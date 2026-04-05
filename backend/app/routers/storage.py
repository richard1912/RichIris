"""Storage migration REST API — validate, migrate, and finalize recordings directory changes."""

import logging

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db, get_session_factory
from app.services.storage_migration import get_migration_manager, validate_target

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/storage", tags=["storage"])


class ValidateRequest(BaseModel):
    path: str


class MigrateRequest(BaseModel):
    target_path: str
    mode: str = "copy"  # "move" or "copy"


@router.post("/validate")
async def validate_storage_path(body: ValidateRequest):
    """Validate a target path for recordings storage."""
    return validate_target(body.path)


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
