"""System status endpoints."""

import logging
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi import APIRouter, Depends, Query
from fastapi.responses import PlainTextResponse
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_tz
from app.database import get_db
from app.models import Camera
from app.schemas import RetentionResult, StorageStats, StreamStatus, SystemStatus
from app.services.retention import enforce_retention, get_storage_stats
from app.services.stream_manager import get_stream_manager

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/system", tags=["system"])


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

    stream_statuses = []
    for cam in cameras:
        status = mgr.get_status(cam.id)
        stream_statuses.append(
            StreamStatus(
                camera_id=cam.id,
                camera_name=cam.name,
                running=status["running"],
                pid=status.get("pid"),
                uptime_seconds=status.get("uptime_seconds"),
                error=status.get("error"),
            )
        )

    active = sum(1 for s in stream_statuses if s.running)
    logger.debug("System status queried", extra={"total": len(cameras), "active": active})

    return SystemStatus(
        streams=stream_statuses,
        total_cameras=len(cameras),
        active_streams=active,
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
    log_file = Path(__file__).resolve().parent.parent.parent.parent / "data" / "logs" / "richiris.log"
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

    # Strip ANSI codes from output for clean display
    ansi_escape = re.compile(r"\x1b\[[0-9;]*m")
    cleaned = [ansi_escape.sub("", line) for line in lines]

    return PlainTextResponse("".join(cleaned) if cleaned else f"No logs in the last {minutes} minutes.")
