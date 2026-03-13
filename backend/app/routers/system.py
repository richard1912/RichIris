"""System status endpoints."""

import logging
from datetime import datetime

from fastapi import APIRouter, Depends
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

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
    now = datetime.now()
    utc_now = datetime.utcnow()
    # Server's UTC offset in minutes (e.g. +660 for UTC+11)
    utc_offset_min = round((now - utc_now).total_seconds() / 60)
    return {
        "iso": now.isoformat(),
        "epoch_ms": int(now.timestamp() * 1000),
        "utc_offset_min": utc_offset_min,
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
