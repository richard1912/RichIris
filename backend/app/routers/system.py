"""System status endpoints."""

import logging
from datetime import datetime, timezone
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

    cutoff = datetime.now(get_tz()) - __import__("datetime").timedelta(minutes=minutes)
    lines: list[str] = []

    with open(log_file, "r", encoding="utf-8", errors="replace") as f:
        for line in f:
            # Parse timestamp from structured log lines (ISO format at start)
            try:
                # structlog console format: "2026-04-03T10:30:00+11:00 [info     ] ..."
                ts_str = line.split(" ", 1)[0]
                ts = datetime.fromisoformat(ts_str)
                if ts >= cutoff:
                    lines.append(line)
            except (ValueError, IndexError):
                # Continuation line or unparseable — include if we're already collecting
                if lines:
                    lines.append(line)

    return PlainTextResponse("".join(lines) if lines else f"No logs in the last {minutes} minutes.")
