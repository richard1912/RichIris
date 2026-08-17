"""Storage migration REST API — validate, migrate, and finalize recordings directory changes."""

import asyncio
import logging
import shutil
import uuid
from datetime import datetime, timedelta
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_config
from app.database import get_db, get_session_factory
from app.models import Recording
from app.schemas import DriveInfo, StorageConfigValidation, TierUsage, TwoTierStatus
from app.services.disk_info import list_drives, mount_root_for_path
from app.services.retention import get_tier_byte_totals
from app.services.storage_flush import get_storage_flusher
from app.services.storage_migration import get_migration_manager, validate_target

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/storage", tags=["storage"])


class ValidateRequest(BaseModel):
    path: str


class MigrateRequest(BaseModel):
    target_path: str
    mode: str = "copy"  # "move" or "copy"


class StorageConfigRequest(BaseModel):
    archive_dir: str
    two_tier_enabled: bool = True


@router.post("/validate")
async def validate_storage_path(body: ValidateRequest):
    """Validate a target path for recordings storage."""
    return validate_target(body.path)


# ---------------------------------------------------------------------------
# Two-tier storage: drive picker, config validation, live status
# ---------------------------------------------------------------------------

def _find_drive(drives: list[dict], path: str) -> DriveInfo | None:
    root = mount_root_for_path(path)
    if not root:
        return None
    for d in drives:
        if d["letter"].upper() == root.upper():
            return DriveInfo(**d)
    return None


@router.get("/drives", response_model=list[DriveInfo])
async def get_drives():
    """List fixed drives with media type (SSD/HDD), SMART health, and capacity."""
    drives = await asyncio.to_thread(list_drives)
    return [DriveInfo(**d) for d in drives]


@router.post("/config/validate", response_model=StorageConfigValidation)
async def validate_storage_config(body: StorageConfigRequest):
    """Validate a proposed archive-tier configuration before it's applied."""
    cfg = get_config().storage
    res = StorageConfigValidation(valid=False)

    archive_dir = (body.archive_dir or "").strip()
    if body.two_tier_enabled and not archive_dir:
        res.error = "Choose an archive drive or folder for two-tier storage."
        return res
    if not body.two_tier_enabled:
        res.valid = True
        return res

    target = Path(archive_dir)
    # Must differ from the hot tier (data_dir).
    try:
        hot_root = Path(cfg.recordings_dir).resolve().parent  # data_dir
        if target.resolve() == hot_root.resolve() or mount_root_for_path(archive_dir) == mount_root_for_path(cfg.recordings_dir):
            res.error = "Archive must be on a different drive from the hot tier."
            return res
    except Exception:
        pass

    # Parent exists / creatable
    if not target.exists() and not target.parent.exists():
        res.error = f"Parent directory does not exist: {target.parent}"
        return res
    try:
        target.mkdir(parents=True, exist_ok=True)
    except Exception as e:
        res.error = f"Cannot create directory: {e}"
        return res

    # Write probe
    try:
        probe = target / f".richiris_write_test_{uuid.uuid4().hex[:8]}"
        probe.write_bytes(b"write_test")
        probe.unlink()
    except Exception as e:
        res.error = f"Directory is not writable: {e}"
        return res

    # Free space
    try:
        res.free_space_gb = round(shutil.disk_usage(str(target)).free / (1024 ** 3), 2)
    except Exception:
        pass

    # Drive health/media advisories (non-blocking)
    drives = await asyncio.to_thread(list_drives)
    drive = _find_drive(drives, str(target))
    res.drive = drive
    warnings = []
    if drive:
        if drive.health in ("Warning", "Unhealthy"):
            warnings.append(
                f"Drive {drive.letter} reports SMART status '{drive.health}'. "
                "Storing footage here risks data loss — replace the drive."
            )
        if drive.media_type == "SSD":
            warnings.append("Archive is an SSD; an HDD is usually the cheaper choice for bulk storage.")
    res.warning = " ".join(warnings)
    res.valid = True
    return res


@router.get("/status", response_model=TwoTierStatus)
async def get_two_tier_status(db: AsyncSession = Depends(get_db)):
    """Live two-tier status for the Storage settings panel."""
    cfg = get_config().storage
    flusher = get_storage_flusher()

    hot_path = cfg.recordings_dir
    archive_path = cfg.archive_recordings_dir
    enabled = bool(cfg.two_tier_enabled and archive_path and archive_path != hot_path)

    drives = await asyncio.to_thread(list_drives)
    totals = await get_tier_byte_totals(db)

    def _tier_usage(path: str) -> TierUsage:
        online = False
        total = free = 0
        try:
            if Path(path).exists():
                usage = shutil.disk_usage(path)
                total, free, online = usage.total, usage.free, True
        except OSError:
            online = False
        return TierUsage(path=path, online=online, disk_total_bytes=total, disk_free_bytes=free)

    hot = _tier_usage(hot_path)
    hot.recorded_bytes = totals["hot"]["bytes"]
    hot.segment_count = totals["hot"]["count"]
    hot.drive = _find_drive(drives, hot_path)

    archive = _tier_usage(archive_path)
    archive.recorded_bytes = totals["archive"]["bytes"]
    archive.segment_count = totals["archive"]["count"]
    archive.drive = _find_drive(drives, archive_path)

    # Flush backlog: finalized HOT segments past the retention window.
    backlog = 0
    if enabled:
        cutoff = datetime.now() - timedelta(minutes=cfg.hot_retention_minutes)
        backlog = (await db.execute(
            select(func.count(Recording.id)).where(
                Recording.in_progress == False,  # noqa: E712
                Recording.tier == "hot",
                Recording.end_time < cutoff,
            )
        )).scalar() or 0

    return TwoTierStatus(
        enabled=enabled,
        hot=hot,
        archive=archive,
        flush_backlog=int(backlog),
        last_flush_at=flusher.state.last_flush_at,
        last_flushed_count=flusher.state.last_flushed_count,
        degraded=flusher.state.degraded if enabled else False,
        last_error=flusher.state.last_error,
        hot_retention_minutes=cfg.hot_retention_minutes,
        hot_max_gb=cfg.hot_max_gb,
    )


@router.post("/migrate")
async def start_migration(body: MigrateRequest):
    """Start migrating recordings to a new directory.

    Stops all recording streams first. Returns migration_id for progress polling.
    """
    if body.mode not in ("move", "copy"):
        raise HTTPException(400, "mode must be 'move' or 'copy'")

    mgr = get_migration_manager()
    if mgr.is_running:
        raise HTTPException(409, "A migration is already in progress.")

    # Stop all recording streams
    from app.services.stream_manager import get_stream_manager
    stream_mgr = get_stream_manager()
    await stream_mgr.stop_all()
    logger.info("Stopped all streams for storage migration")

    # Stop thumbnail capture
    from app.services.thumbnail_capture import get_thumbnail_capture
    thumb = get_thumbnail_capture()
    await thumb.stop()
    logger.info("Stopped thumbnail capture for storage migration")

    # Stop motion detector
    from app.services.motion_detector import get_motion_detector
    motion = get_motion_detector()
    await motion.stop()
    logger.info("Stopped motion detector for storage migration")

    progress = await mgr.start_migration(body.target_path, body.mode)
    return {"migration_id": progress.migration_id}


@router.get("/migrate/{migration_id}/progress")
async def get_migration_progress(migration_id: str):
    """Poll migration progress."""
    mgr = get_migration_manager()
    progress = mgr.get_progress(migration_id)
    if not progress:
        raise HTTPException(404, "Migration not found.")
    return {
        "migration_id": progress.migration_id,
        "status": progress.status,
        "files_total": progress.files_total,
        "files_done": progress.files_done,
        "bytes_total": progress.bytes_total,
        "bytes_done": progress.bytes_done,
        "current_file": progress.current_file,
        "error": progress.error,
    }


@router.post("/migrate/{migration_id}/cancel")
async def cancel_migration(migration_id: str):
    """Cancel an in-progress migration."""
    mgr = get_migration_manager()
    if mgr.cancel(migration_id):
        return {"cancelled": True}
    raise HTTPException(404, "Migration not found or not in progress.")


@router.post("/migrate/{migration_id}/finalize")
async def finalize_migration(migration_id: str, db: AsyncSession = Depends(get_db)):
    """Finalize a completed migration: update settings and restart streams."""
    mgr = get_migration_manager()
    progress = mgr.get_progress(migration_id)
    if not progress:
        raise HTTPException(404, "Migration not found.")
    if progress.status not in ("completed", "cancelled"):
        raise HTTPException(400, f"Cannot finalize migration in status: {progress.status}")

    new_path = progress.target

    # Update the recordings_dir setting in DB
    if progress.status == "completed":
        from app.services.settings import update_settings
        await update_settings(db, {"storage.recordings_dir": new_path})
        logger.info("Updated recordings_dir setting", extra={"new_path": new_path})

        # Reload config
        from app.config import reload_from_db, validate_paths, get_config
        factory = get_session_factory()
        async with factory() as session:
            await reload_from_db(session)
        validate_paths(get_config())

    # Restart all services
    await _restart_camera_services()

    return {"finalized": True, "recordings_dir": new_path}


@router.post("/update-path")
async def update_path_only(body: ValidateRequest, db: AsyncSession = Depends(get_db)):
    """Change recordings directory without migrating files.

    Validates the path, updates settings, and restarts streams.
    """
    validation = validate_target(body.path)
    if not validation["valid"]:
        raise HTTPException(400, validation["error"])

    # Stop all services
    from app.services.stream_manager import get_stream_manager
    stream_mgr = get_stream_manager()
    await stream_mgr.stop_all()

    from app.services.thumbnail_capture import get_thumbnail_capture
    thumb = get_thumbnail_capture()
    await thumb.stop()

    from app.services.motion_detector import get_motion_detector
    motion = get_motion_detector()
    await motion.stop()

    # Update setting
    from app.services.settings import update_settings
    await update_settings(db, {"storage.recordings_dir": body.path})

    # Reload config
    from app.config import reload_from_db, validate_paths, get_config
    factory = get_session_factory()
    async with factory() as session:
        await reload_from_db(session)
    validate_paths(get_config())

    # Restart services
    await _restart_camera_services()

    return {"updated": True, "recordings_dir": body.path}


async def _restart_camera_services() -> None:
    """Restart recording streams, thumbnail capture, and motion detection."""
    from sqlalchemy import select
    from app.database import get_session_factory
    from app.models import Camera
    from app.services.stream_manager import get_stream_manager
    from app.services.thumbnail_capture import get_thumbnail_capture
    from app.services.motion_detector import get_motion_detector

    factory = get_session_factory()
    async with factory() as session:
        result = await session.execute(
            select(Camera).where(Camera.enabled == True)
        )
        cameras_list = list(result.scalars().all())

    # Restart recording streams
    stream_mgr = get_stream_manager()
    import asyncio
    await asyncio.gather(*[
        stream_mgr.start_stream(cam.id, cam.name, cam.rtsp_url, cam.sub_stream_url)
        for cam in cameras_list
    ])
    logger.info("Restarted camera streams", extra={"count": len(cameras_list)})

    # Restart thumbnail capture
    thumb = get_thumbnail_capture()
    thumb.start(cameras_list)

    # Restart motion detector
    motion = get_motion_detector()
    await motion.start(cameras_list)
