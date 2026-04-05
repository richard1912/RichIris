"""Backup and restore REST API."""

import logging

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db, init_db
from app.services.backup import get_backup_manager

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/backup", tags=["backup"])


class CreateBackupRequest(BaseModel):
    components: list[str]
    target_path: str


class InspectRequest(BaseModel):
    file_path: str


class RestoreRequest(BaseModel):
    file_path: str
    components: list[str]


VALID_COMPONENTS = {"settings", "cameras", "database", "recordings", "thumbnails"}


def _validate_components(components: list[str]) -> None:
    invalid = set(components) - VALID_COMPONENTS
    if invalid:
        raise HTTPException(400, f"Invalid components: {invalid}")
    if not components:
        raise HTTPException(400, "At least one component must be selected.")


@router.get("/preview")
async def preview_sizes(db: AsyncSession = Depends(get_db)):
    """Return size estimates for each backup component."""
    mgr = get_backup_manager()
    return await mgr.preview_sizes(db)


@router.post("/create")
async def create_backup(body: CreateBackupRequest, db: AsyncSession = Depends(get_db)):
    """Start creating a backup archive."""
    _validate_components(body.components)

    mgr = get_backup_manager()
    if mgr.is_running:
        raise HTTPException(409, "A backup or restore is already in progress.")

    progress = await mgr.start_backup(body.components, body.target_path, db)
    return {"backup_id": progress.backup_id}


@router.get("/{backup_id}/progress")
async def get_backup_progress(backup_id: str):
    """Poll backup progress."""
    mgr = get_backup_manager()
    progress = mgr.get_progress(backup_id)
    if not progress:
        raise HTTPException(404, "Backup not found.")
    return {
        "backup_id": progress.backup_id,
        "operation": progress.operation,
        "status": progress.status,
        "files_total": progress.files_total,
        "files_done": progress.files_done,
        "bytes_total": progress.bytes_total,
        "bytes_done": progress.bytes_done,
        "current_file": progress.current_file,
        "error": progress.error,
    }


@router.post("/{backup_id}/cancel")
async def cancel_backup(backup_id: str):
    """Cancel an in-progress backup or restore."""
    mgr = get_backup_manager()
    if mgr.cancel(backup_id):
        return {"cancelled": True}
    raise HTTPException(404, "Operation not found or not in progress.")


@router.post("/inspect")
async def inspect_backup(body: InspectRequest):
    """Inspect a .richiris backup file and return its manifest."""
    mgr = get_backup_manager()
    try:
        manifest = await mgr.inspect_backup(body.file_path)
        return manifest
    except FileNotFoundError as e:
        raise HTTPException(404, str(e))
    except (ValueError, Exception) as e:
        raise HTTPException(400, str(e))


@router.post("/restore")
async def start_restore(body: RestoreRequest):
    """Start restoring from a .richiris backup file.

    Stops all camera services if database/settings/cameras are being restored.
    """
    _validate_components(body.components)

    mgr = get_backup_manager()
    if mgr.is_running:
        raise HTTPException(409, "A backup or restore is already in progress.")

    needs_service_stop = bool(
        {"settings", "cameras", "database"} & set(body.components)
    )

    if needs_service_stop:
        await _stop_camera_services()

    progress = await mgr.start_restore(
        body.file_path, body.components,
    )
    return {
        "backup_id": progress.backup_id,
        "needs_restart": needs_service_stop,
    }


@router.get("/restore/{backup_id}/progress")
async def get_restore_progress(backup_id: str):
    """Poll restore progress."""
    mgr = get_backup_manager()
    progress = mgr.get_progress(backup_id)
    if not progress:
        raise HTTPException(404, "Restore not found.")

    result = {
        "backup_id": progress.backup_id,
        "operation": progress.operation,
        "status": progress.status,
        "files_total": progress.files_total,
        "files_done": progress.files_done,
        "bytes_total": progress.bytes_total,
        "bytes_done": progress.bytes_done,
        "current_file": progress.current_file,
        "error": progress.error,
    }

    # If restore completed or failed, reinit DB and restart services
    if progress.status in ("completed", "failed", "cancelled"):
        if not hasattr(progress, "_finalized"):
            progress._finalized = True  # type: ignore[attr-defined]
            try:
                await init_db()
                logger.info("Reinitialized database after restore")
            except Exception:
                logger.exception("Failed to reinitialize database after restore")

            try:
                from app.config import reload_from_db, get_config, validate_paths
                from app.database import get_session_factory
                factory = get_session_factory()
                async with factory() as session:
                    await reload_from_db(session)
                validate_paths(get_config())
            except Exception:
                logger.exception("Failed to reload config after restore")

            try:
                await _restart_camera_services()
            except Exception:
                logger.exception("Failed to restart services after restore")

            # Re-register go2rtc streams so live view works without
            # a full service restart
            try:
                await _reregister_go2rtc_streams()
            except Exception:
                logger.exception("Failed to re-register go2rtc streams after restore")

    return result


@router.post("/restore/{backup_id}/cancel")
async def cancel_restore(backup_id: str):
    """Cancel an in-progress restore."""
    mgr = get_backup_manager()
    if mgr.cancel(backup_id):
        return {"cancelled": True}
    raise HTTPException(404, "Restore not found or not in progress.")


async def _stop_camera_services() -> None:
    """Stop all camera-related services."""
    from app.services.stream_manager import get_stream_manager
    from app.services.thumbnail_capture import get_thumbnail_capture
    from app.services.motion_detector import get_motion_detector

    stream_mgr = get_stream_manager()
    await stream_mgr.stop_all()
    logger.info("Stopped all streams for restore")

    thumb = get_thumbnail_capture()
    await thumb.stop()
    logger.info("Stopped thumbnail capture for restore")

    motion = get_motion_detector()
    await motion.stop()
    logger.info("Stopped motion detector for restore")


async def _restart_camera_services() -> None:
    """Restart recording streams, thumbnail capture, and motion detection."""
    import asyncio
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

    stream_mgr = get_stream_manager()
    await asyncio.gather(*[
        stream_mgr.start_stream(cam.id, cam.name, cam.rtsp_url, cam.sub_stream_url)
        for cam in cameras_list
    ])
    logger.info("Restarted camera streams", extra={"count": len(cameras_list)})

    thumb = get_thumbnail_capture()
    thumb.start(cameras_list)

    motion = get_motion_detector()
    await motion.start(cameras_list)


async def _reregister_go2rtc_streams() -> None:
    """Re-register all camera streams with go2rtc so live view works."""
    from sqlalchemy import select
    from app.database import get_session_factory
    from app.models import Camera
    from app.services.go2rtc_client import build_streams_config, get_go2rtc_client

    factory = get_session_factory()
    async with factory() as session:
        result = await session.execute(
            select(Camera).where(Camera.enabled == True)
        )
        cameras_list = list(result.scalars().all())

    cameras_tuples = [
        (cam.name, cam.rtsp_url, cam.sub_stream_url)
        for cam in cameras_list
    ]
    streams_config = build_streams_config(cameras_tuples)

    client = get_go2rtc_client()
    await client.register_streams_from_config(streams_config)
    logger.info("Re-registered go2rtc streams after restore", extra={"count": len(streams_config)})
