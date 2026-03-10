"""Retention service: deletes old recordings by age and storage limits."""

import logging
import os
from datetime import datetime, timedelta
from pathlib import Path

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_config
from app.models import Recording

logger = logging.getLogger(__name__)


async def enforce_retention(session: AsyncSession) -> dict:
    """Run retention cleanup: age-based first, then storage-based.

    Returns a summary dict with counts and bytes freed.
    """
    config = get_config()
    total_deleted = 0
    total_bytes_freed = 0

    # Age-based retention
    deleted, freed = await _delete_by_age(session, config.retention.max_age_days)
    total_deleted += deleted
    total_bytes_freed += freed

    # Storage-based retention
    deleted, freed = await _delete_by_storage(session, config.retention.max_storage_gb)
    total_deleted += deleted
    total_bytes_freed += freed

    if total_deleted > 0:
        await session.commit()
        logger.info(
            "Retention cleanup complete",
            extra={"deleted": total_deleted, "freed_mb": round(total_bytes_freed / 1_048_576, 1)},
        )

    return {"deleted": total_deleted, "freed_bytes": total_bytes_freed}


async def _delete_by_age(session: AsyncSession, max_age_days: int) -> tuple[int, int]:
    """Delete recordings older than max_age_days."""
    cutoff = datetime.now() - timedelta(days=max_age_days)

    result = await session.execute(
        select(Recording).where(Recording.start_time < cutoff)
    )
    old_recordings = result.scalars().all()

    if not old_recordings:
        return 0, 0

    deleted, freed = _delete_recordings(old_recordings)

    ids = [r.id for r in old_recordings]
    await session.execute(delete(Recording).where(Recording.id.in_(ids)))

    logger.info(
        "Age-based retention",
        extra={"cutoff": str(cutoff), "deleted": deleted, "freed_mb": round(freed / 1_048_576, 1)},
    )
    return deleted, freed


async def _delete_by_storage(session: AsyncSession, max_storage_gb: int) -> tuple[int, int]:
    """Delete oldest recordings until total storage is under the limit."""
    max_bytes = max_storage_gb * 1_073_741_824  # GB to bytes

    total_result = await session.execute(
        select(func.coalesce(func.sum(Recording.file_size), 0))
    )
    total_size = total_result.scalar()

    if total_size <= max_bytes:
        return 0, 0

    excess = total_size - max_bytes
    logger.info(
        "Storage over limit",
        extra={"total_gb": round(total_size / 1_073_741_824, 2), "max_gb": max_storage_gb},
    )

    # Fetch oldest recordings first
    result = await session.execute(
        select(Recording).order_by(Recording.start_time.asc())
    )
    all_recordings = result.scalars().all()

    to_delete = []
    freed_so_far = 0
    for rec in all_recordings:
        if freed_so_far >= excess:
            break
        to_delete.append(rec)
        freed_so_far += rec.file_size or 0

    if not to_delete:
        return 0, 0

    deleted, freed = _delete_recordings(to_delete)
    ids = [r.id for r in to_delete]
    await session.execute(delete(Recording).where(Recording.id.in_(ids)))

    logger.info(
        "Storage-based retention",
        extra={"deleted": deleted, "freed_mb": round(freed / 1_048_576, 1)},
    )
    return deleted, freed


def _delete_recordings(recordings: list[Recording]) -> tuple[int, int]:
    """Delete recording files from disk. Returns (count_deleted, bytes_freed)."""
    deleted = 0
    freed = 0
    for rec in recordings:
        path = Path(rec.file_path)
        if path.exists():
            size = path.stat().st_size
            try:
                path.unlink()
                freed += size
                deleted += 1
                logger.debug("Deleted segment", extra={"path": str(path), "size": size})
            except OSError:
                logger.exception("Failed to delete segment", extra={"path": str(path)})
        else:
            # File already gone, still count for DB cleanup
            deleted += 1

    # Clean up empty date directories (including empty thumbs/ subdirs)
    _cleanup_empty_dirs()
    return deleted, freed


def _cleanup_empty_dirs() -> None:
    """Remove empty date directories under each camera folder."""
    config = get_config()
    rec_root = Path(config.storage.recordings_dir)
    if not rec_root.exists():
        return

    for camera_dir in rec_root.iterdir():
        if not camera_dir.is_dir():
            continue
        for date_dir in camera_dir.iterdir():
            if date_dir.is_dir() and not any(date_dir.iterdir()):
                try:
                    date_dir.rmdir()
                    logger.debug("Removed empty dir", extra={"path": str(date_dir)})
                except OSError:
                    pass


async def get_storage_stats(session: AsyncSession) -> dict:
    """Get storage statistics for the system status page."""
    config = get_config()
    rec_dir = Path(config.storage.recordings_dir)

    # Disk usage from OS
    disk_total, disk_used, disk_free = 0, 0, 0
    if rec_dir.exists():
        usage = os.statvfs(str(rec_dir)) if hasattr(os, "statvfs") else None
        if usage:
            disk_total = usage.f_frsize * usage.f_blocks
            disk_free = usage.f_frsize * usage.f_bavail
            disk_used = disk_total - disk_free
        else:
            # Windows fallback
            import shutil
            total, used, free = shutil.disk_usage(str(rec_dir))
            disk_total, disk_used, disk_free = total, used, free

    # Per-camera recording stats from DB
    result = await session.execute(
        select(
            Recording.camera_id,
            func.count(Recording.id),
            func.coalesce(func.sum(Recording.file_size), 0),
            func.min(Recording.start_time),
            func.max(Recording.start_time),
        ).group_by(Recording.camera_id)
    )
    camera_stats = []
    total_recordings_size = 0
    for row in result.all():
        cam_size = row[2]
        total_recordings_size += cam_size
        camera_stats.append({
            "camera_id": row[0],
            "segment_count": row[1],
            "total_size_bytes": cam_size,
            "oldest_recording": row[3].isoformat() if row[3] else None,
            "newest_recording": row[4].isoformat() if row[4] else None,
        })

    return {
        "disk_total_bytes": disk_total,
        "disk_used_bytes": disk_used,
        "disk_free_bytes": disk_free,
        "recordings_total_bytes": total_recordings_size,
        "max_storage_bytes": config.retention.max_storage_gb * 1_073_741_824,
        "max_age_days": config.retention.max_age_days,
        "camera_stats": camera_stats,
    }
