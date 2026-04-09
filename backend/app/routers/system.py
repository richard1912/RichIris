"""System status endpoints."""

import ctypes
import logging
import os
import re
import shutil
import subprocess
import sys
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import yaml
from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_bootstrap, get_config, get_tz, get_app_dir
from app.database import get_db
from app.models import Camera
from app.schemas import RetentionResult, StorageStats, StreamStatus, SystemStatus
from app.services.go2rtc_client import get_go2rtc_client, get_stream_name
from app.services.retention import enforce_retention, get_storage_stats
from app.services.stream_manager import get_stream_manager

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/system", tags=["system"])

_startup_time: float = time.time()


@router.get("/time")
async def get_server_time():
    """Return server local time and UTC offset for client timezone alignment."""
    tz = get_tz()
    now = datetime.now(tz)
    utc_offset_min = int(now.utcoffset().total_seconds() / 60)
    return {
        "iso": now.isoformat(),
        "epoch_ms": int(now.timestamp() * 1000),
        "utc_offset_min": utc_offset_min,
        "timezone": str(tz),
    }


@router.get("/status", response_model=SystemStatus)
async def get_system_status(db: AsyncSession = Depends(get_db)):
    """Get overall system status including stream health."""
    mgr = get_stream_manager()

    result = await db.execute(select(Camera))
    cameras = result.scalars().all()

    # Query go2rtc stream health in parallel with building statuses
    go2rtc = get_go2rtc_client()
    go2rtc_health = await go2rtc.get_streams_health()

    stream_statuses = []
    for cam in cameras:
        status = mgr.get_status(cam.id)
        # Check go2rtc producer status for this camera's direct streams
        base_name = get_stream_name(cam.name)
        s1_info = go2rtc_health.get(f"{base_name}_s1_direct", {})
        s2_info = go2rtc_health.get(f"{base_name}_s2_direct", {})
        s1_prods = s1_info.get("producers") or []
        s2_prods = s2_info.get("producers") or []
        s1_cons = s1_info.get("consumers") or []
        s2_cons = s2_info.get("consumers") or []
        # Connected if either main or sub stream has active bytes
        s1_active = any(p.get("bytes_recv", 0) > 0 for p in s1_prods)
        s2_active = any(p.get("bytes_recv", 0) > 0 for p in s2_prods)
        go2rtc_connected = (s1_active or s2_active) if (s1_info or s2_info) else None
        go2rtc_consumer_count = (len(s1_cons) + len(s2_cons)) if (s1_info or s2_info) else None

        stream_statuses.append(
            StreamStatus(
                camera_id=cam.id,
                camera_name=cam.name,
                running=status["running"],
                pid=status.get("pid"),
                uptime_seconds=status.get("uptime_seconds"),
                error=status.get("error"),
                go2rtc_connected=go2rtc_connected,
                go2rtc_consumers=go2rtc_consumer_count,
            )
        )

    active = sum(1 for s in stream_statuses if s.running)
    logger.debug("System status queried", extra={"total": len(cameras), "active": active})

    from app.services.go2rtc_manager import get_rtsp_port
    return SystemStatus(
        streams=stream_statuses,
        total_cameras=len(cameras),
        active_streams=active,
        go2rtc_rtsp_port=get_rtsp_port(),
    )


@router.get("/storage", response_model=StorageStats)
async def get_storage_status(db: AsyncSession = Depends(get_db)):
    """Get storage usage and per-camera recording stats."""
    stats = await get_storage_stats(db)
    return StorageStats(**stats)


@router.post("/retention/run", response_model=RetentionResult)
async def run_retention(db: AsyncSession = Depends(get_db)):
    """Manually trigger retention cleanup."""
    result = await enforce_retention(db)
    return RetentionResult(**result)


@router.get("/logs", response_class=PlainTextResponse)
async def get_recent_logs(minutes: int = Query(default=10, ge=1, le=60)):
    """Return log lines from the last N minutes."""
    from app.config import get_bootstrap
    log_file = Path(get_bootstrap().data_dir) / "logs" / "richiris.log"
    if not log_file.exists():
        return PlainTextResponse("No log file found.", status_code=404)

    cutoff = datetime.now(get_tz()) - timedelta(minutes=minutes)
    lines: list[str] = []
    # Match ISO timestamp, possibly wrapped in ANSI escape codes
    ts_pattern = re.compile(r"(?:\x1b\[\d+m)*(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}[.\d]*[+-]\d{2}:\d{2})")

    with open(log_file, "r", encoding="utf-8", errors="replace") as f:
        for line in f:
            m = ts_pattern.match(line)
            if m:
                try:
                    ts = datetime.fromisoformat(m.group(1))
                    if ts >= cutoff:
                        lines.append(line)
                    elif lines:
                        # Past cutoff window — stop if we already started collecting
                        # (shouldn't happen with chronological logs, but be safe)
                        pass
                except ValueError:
                    if lines:
                        lines.append(line)
            elif lines:
                lines.append(line)

    # Strip ANSI codes from output for clean display, newest first
    ansi_escape = re.compile(r"\x1b\[[0-9;]*m")
    cleaned = [ansi_escape.sub("", line) for line in reversed(lines)]

    return PlainTextResponse("".join(cleaned) if cleaned else f"No logs in the last {minutes} minutes.")


# ---------------------------------------------------------------------------
# Service diagnostics
# ---------------------------------------------------------------------------


def _get_process_memory_mb() -> float:
    """Get current process memory usage (RSS) in MB using Windows API."""
    if sys.platform != "win32":
        return 0.0
    try:
        # PROCESS_MEMORY_COUNTERS_EX
        class PROCESS_MEMORY_COUNTERS(ctypes.Structure):
            _fields_ = [
                ("cb", ctypes.c_ulong),
                ("PageFaultCount", ctypes.c_ulong),
                ("PeakWorkingSetSize", ctypes.c_size_t),
                ("WorkingSetSize", ctypes.c_size_t),
                ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                ("PagefileUsage", ctypes.c_size_t),
                ("PeakPagefileUsage", ctypes.c_size_t),
            ]

        pmc = PROCESS_MEMORY_COUNTERS()
        pmc.cb = ctypes.sizeof(pmc)
        handle = ctypes.windll.kernel32.GetCurrentProcess()
        if ctypes.windll.psapi.GetProcessMemoryInfo(handle, ctypes.byref(pmc), pmc.cb):
            return round(pmc.WorkingSetSize / (1024 * 1024), 1)
    except Exception:
        pass
    return 0.0


def _count_child_processes(name_filter: str) -> tuple[int, list[int]]:
    """Count child processes matching a name filter. Returns (count, pids)."""
    if sys.platform != "win32":
        return 0, []
    try:
        result = subprocess.run(
            ["tasklist", "/fi", f"imagename eq {name_filter}", "/fo", "csv", "/nh"],
            capture_output=True, text=True, timeout=5,
        )
        pids = []
        for line in result.stdout.strip().splitlines():
            parts = line.strip().strip('"').split('","')
            if len(parts) >= 2:
                try:
                    pids.append(int(parts[1]))
                except ValueError:
                    pass
        return len(pids), pids
    except Exception:
        return 0, []


@router.get("/service")
async def get_service_info(db: AsyncSession = Depends(get_db)):
    """Return backend service diagnostic information."""
    from app.services.go2rtc_manager import _process as go2rtc_process, is_managed as go2rtc_is_managed

    config = get_config()
    bootstrap = get_bootstrap()

    # Process info
    pid = os.getpid()
    uptime_seconds = round(time.time() - _startup_time)
    memory_mb = _get_process_memory_mb()

    # go2rtc status
    go2rtc_running = go2rtc_is_managed()
    go2rtc_pid = go2rtc_process.pid if go2rtc_process and go2rtc_process.poll() is None else None

    # Camera/stream info
    mgr = get_stream_manager()
    result = await db.execute(select(Camera))
    cameras = result.scalars().all()
    total_cameras = len(cameras)
    active_streams = sum(1 for cam in cameras if mgr.get_status(cam.id).get("running"))

    # ffmpeg process count
    ffmpeg_count, _ = _count_child_processes("ffmpeg.exe")

    # Log file size
    log_file = Path(bootstrap.data_dir) / "logs" / "richiris.log"
    log_size_mb = 0.0
    if log_file.exists():
        try:
            log_size_mb = round(log_file.stat().st_size / (1024 * 1024), 2)
        except Exception:
            pass

    # Startup time as ISO string
    startup_iso = datetime.fromtimestamp(_startup_time, tz=get_tz()).isoformat()

    return {
        "pid": pid,
        "uptime_seconds": uptime_seconds,
        "memory_mb": memory_mb,
        "python_version": sys.version.split()[0],
        "platform": sys.platform,
        "go2rtc_running": go2rtc_running,
        "go2rtc_pid": go2rtc_pid,
        "total_cameras": total_cameras,
        "active_streams": active_streams,
        "ffmpeg_processes": ffmpeg_count,
        "data_dir": str(bootstrap.data_dir),
        "log_file_size_mb": log_size_mb,
        "startup_time": startup_iso,
        "port": bootstrap.port,
    }


# ---------------------------------------------------------------------------
# Data directory management
# ---------------------------------------------------------------------------

class DataDirUpdateRequest(BaseModel):
    path: str
    mode: str = "path_only"  # "move", "copy", or "path_only"


@router.get("/data-dir")
async def get_data_dir():
    """Return the current data directory path and subdirectory info."""
    bootstrap = get_bootstrap()
    data_dir = Path(bootstrap.data_dir)

    subdirs = {}
    for name in ("database", "logs", "recordings", "thumbnails", "playback"):
        sub = data_dir / name
        subdirs[name] = {
            "path": str(sub),
            "exists": sub.exists(),
        }

    # Disk usage
    free_gb = 0.0
    try:
        usage = shutil.disk_usage(str(data_dir))
        free_gb = round(usage.free / (1024 ** 3), 2)
    except Exception:
        pass

    # Total data size
    total_bytes = 0
    if data_dir.exists():
        try:
            for entry in data_dir.rglob("*"):
                if entry.is_file():
                    total_bytes += entry.stat().st_size
        except Exception:
            pass

    return {
        "data_dir": str(data_dir),
        "free_space_gb": free_gb,
        "total_size_gb": round(total_bytes / (1024 ** 3), 2),
        "subdirs": subdirs,
    }


@router.post("/data-dir/validate")
async def validate_data_dir(body: DataDirUpdateRequest):
    """Validate a target path for data directory migration."""
    bootstrap = get_bootstrap()
    source = Path(bootstrap.data_dir)
    target = Path(body.path)

    result = {
        "valid": False,
        "error": "",
        "free_space_gb": 0.0,
        "source_size_gb": 0.0,
    }

    # Reject same path
    try:
        if source.resolve() == target.resolve():
            result["error"] = "Target is the same as the current data directory."
            return result
    except Exception:
        pass

    # Check parent exists
    if not target.parent.exists():
        result["error"] = f"Parent directory does not exist: {target.parent}"
        return result

    # Create target dir if needed
    try:
        target.mkdir(parents=True, exist_ok=True)
    except Exception as e:
        result["error"] = f"Cannot create directory: {e}"
        return result

    # Write test
    try:
        test_file = target / f".richiris_write_test_{uuid.uuid4().hex[:8]}"
        test_file.write_bytes(b"write_test")
        test_file.unlink()
    except Exception as e:
        result["error"] = f"Directory is not writable: {e}"
        return result

    # Free space
    try:
        usage = shutil.disk_usage(str(target))
        result["free_space_gb"] = round(usage.free / (1024 ** 3), 2)
    except Exception:
        pass

    # Source size
    total_bytes = 0
    if source.exists():
        try:
            for entry in source.rglob("*"):
                if entry.is_file():
                    total_bytes += entry.stat().st_size
        except Exception:
            pass
    result["source_size_gb"] = round(total_bytes / (1024 ** 3), 2)

    result["valid"] = True
    return result


@router.post("/data-dir")
async def update_data_dir(body: DataDirUpdateRequest):
    """Change the data directory.

    Modes:
    - path_only: Just update bootstrap.yaml (user moved files manually or starting fresh)
    - move: Copy all data to new location, then delete old
    - copy: Copy all data to new location, keep old

    Returns restart_required=True — the service MUST be restarted for the change to take effect.
    """
    if body.mode not in ("move", "copy", "path_only"):
        raise HTTPException(400, "mode must be 'move', 'copy', or 'path_only'")

    bootstrap = get_bootstrap()
    source = Path(bootstrap.data_dir)
    target = Path(body.path)

    # Validate
    try:
        if source.resolve() == target.resolve():
            raise HTTPException(400, "Target is the same as the current data directory.")
    except HTTPException:
        raise
    except Exception:
        pass

    # Create target subdirectories
    for subdir in ("database", "logs", "recordings", "thumbnails", "playback"):
        (target / subdir).mkdir(parents=True, exist_ok=True)

    # Migrate files if requested
    if body.mode in ("move", "copy"):
        logger.info("Starting data directory migration",
                     extra={"source": str(source), "target": str(target), "mode": body.mode})
        try:
            for subdir in ("database", "logs", "recordings", "thumbnails", "playback"):
                src_sub = source / subdir
                dst_sub = target / subdir
                if not src_sub.exists():
                    continue
                for item in src_sub.rglob("*"):
                    if item.is_file():
                        rel = item.relative_to(src_sub)
                        dest = dst_sub / rel
                        dest.parent.mkdir(parents=True, exist_ok=True)
                        shutil.copy2(str(item), str(dest))

            if body.mode == "move":
                for subdir in ("database", "logs", "recordings", "thumbnails", "playback"):
                    src_sub = source / subdir
                    if src_sub.exists():
                        try:
                            shutil.rmtree(str(src_sub))
                        except Exception:
                            logger.warning("Failed to remove source subdirectory",
                                         extra={"path": str(src_sub)})

            logger.info("Data directory migration completed",
                         extra={"target": str(target), "mode": body.mode})
        except Exception as e:
            logger.exception("Data directory migration failed")
            raise HTTPException(500, f"Migration failed: {e}")

    # Update bootstrap.yaml
    app_dir = get_app_dir()
    bootstrap_path = app_dir / "bootstrap.yaml"
    new_data_dir = str(target).replace("\\", "/")
    try:
        bootstrap_data = {"data_dir": new_data_dir, "port": bootstrap.port}
        with open(bootstrap_path, "w") as f:
            yaml.dump(bootstrap_data, f, default_flow_style=False)
        logger.info("Updated bootstrap.yaml", extra={"data_dir": new_data_dir})
    except Exception as e:
        raise HTTPException(500, f"Failed to update bootstrap.yaml: {e}")

    return {
        "updated": True,
        "data_dir": new_data_dir,
        "restart_required": True,
    }


# ---------------------------------------------------------------------------
# Update checking
# ---------------------------------------------------------------------------

@router.get("/version")
async def get_version():
    """Return the installed backend version."""
    from app.services.update_checker import get_update_checker
    checker = get_update_checker()
    return {"version": checker.current_version}


@router.get("/update")
async def get_update_info():
    """Return cached latest release info (from periodic GitHub check)."""
    from app.services.update_checker import get_update_checker
    checker = get_update_checker()
    return {
        "update_available": checker.latest_release is not None,
        "current_version": checker.current_version,
        "latest": checker.latest_release,
        "last_checked": checker.last_check.isoformat() if checker.last_check else None,
    }


@router.post("/update/check")
async def check_for_update():
    """Force an immediate update check (manual trigger from app)."""
    from app.services.update_checker import get_update_checker
    checker = get_update_checker()
    await checker.check_now()
    return {
        "update_available": checker.latest_release is not None,
        "current_version": checker.current_version,
        "latest": checker.latest_release,
        "last_checked": checker.last_check.isoformat() if checker.last_check else None,
    }
