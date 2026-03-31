"""Camera CRUD API endpoints."""

import logging
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_config
from app.database import get_db
from app.models import Camera, ClipExport, MotionEvent, Recording
from app.schemas import CameraCreate, CameraResponse, CameraUpdate
from app.services.ffmpeg import sanitize_camera_name
from app.services.motion_detector import get_motion_detector
from app.services.stream_manager import get_stream_manager

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/cameras", tags=["cameras"])


@router.get("", response_model=list[CameraResponse])
async def list_cameras(db: AsyncSession = Depends(get_db)):
    """List all cameras."""
    result = await db.execute(select(Camera).order_by(Camera.id))
    cameras = result.scalars().all()
    logger.debug("Listed cameras", extra={"count": len(cameras)})
    return cameras


@router.get("/{camera_id}", response_model=CameraResponse)
async def get_camera(camera_id: int, db: AsyncSession = Depends(get_db)):
    """Get a single camera by ID."""
    camera = await db.get(Camera, camera_id)
    if not camera:
        raise HTTPException(status_code=404, detail="Camera not found")
    return camera


@router.post("", response_model=CameraResponse, status_code=201)
async def create_camera(data: CameraCreate, db: AsyncSession = Depends(get_db)):
    """Create a new camera."""
    camera = Camera(
        name=data.name, rtsp_url=data.rtsp_url,
        sub_stream_url=data.sub_stream_url or None,
        enabled=data.enabled, rotation=data.rotation,
        motion_sensitivity=data.motion_sensitivity,
        motion_script=data.motion_script,
        motion_script_off=data.motion_script_off,
        ai_detection=data.ai_detection,
        ai_confidence_threshold=data.ai_confidence_threshold,
    )
    db.add(camera)
    await db.commit()
    await db.refresh(camera)
    logger.info("Camera created", extra={"camera_id": camera.id, "camera_name": camera.name})

    if camera.enabled:
        try:
            mgr = get_stream_manager()
            await mgr.start_stream(camera.id, camera.name, camera.rtsp_url, camera.sub_stream_url)
        except Exception:
            logger.exception("Failed to start stream for new camera", extra={"camera_id": camera.id})

    return camera


@router.put("/{camera_id}", response_model=CameraResponse)
async def update_camera(
    camera_id: int, data: CameraUpdate, db: AsyncSession = Depends(get_db)
):
    """Update an existing camera."""
    camera = await db.get(Camera, camera_id)
    if not camera:
        raise HTTPException(status_code=404, detail="Camera not found")

    mgr = get_stream_manager()
    needs_restart = False

    old_name = camera.name

    if data.name is not None:
        camera.name = data.name
        needs_restart = True
    if data.rtsp_url is not None:
        camera.rtsp_url = data.rtsp_url
        needs_restart = True
    if "sub_stream_url" in (data.model_fields_set or set()):
        camera.sub_stream_url = data.sub_stream_url or None
        needs_restart = True
    if data.enabled is not None:
        camera.enabled = data.enabled
    if data.rotation is not None:
        camera.rotation = data.rotation
    if data.motion_sensitivity is not None:
        camera.motion_sensitivity = data.motion_sensitivity
    if "motion_script" in (data.model_fields_set or set()):
        camera.motion_script = data.motion_script
    if "motion_script_off" in (data.model_fields_set or set()):
        camera.motion_script_off = data.motion_script_off
    if data.ai_detection is not None:
        camera.ai_detection = data.ai_detection
    if data.ai_confidence_threshold is not None:
        camera.ai_confidence_threshold = data.ai_confidence_threshold

    await db.commit()
    await db.refresh(camera)
    logger.info("Camera updated", extra={"camera_id": camera.id})

    # Rename recording folder and update DB paths if name changed
    if data.name is not None and data.name != old_name:
        await _rename_camera_folder(db, camera.id, old_name, data.name)

    if not camera.enabled:
        await mgr.stop_stream(camera.id)
    elif needs_restart:
        await mgr.stop_stream(camera.id)
        await mgr.start_stream(camera.id, camera.name, camera.rtsp_url, camera.sub_stream_url)

    # Update motion detection if settings changed
    detector = get_motion_detector()
    await detector.update_camera(camera)

    return camera


@router.delete("/{camera_id}", status_code=204)
async def delete_camera(camera_id: int, db: AsyncSession = Depends(get_db)):
    """Delete a camera and stop its stream. Video files on disk are preserved."""
    camera = await db.get(Camera, camera_id)
    if not camera:
        raise HTTPException(status_code=404, detail="Camera not found")

    mgr = get_stream_manager()
    await mgr.stop_stream(camera.id)

    # Remove DB metadata (recordings + clip exports) so FK constraints don't block delete.
    # Actual video files on disk are NOT deleted.
    for model in (ClipExport, MotionEvent, Recording):
        result = await db.execute(select(model).where(model.camera_id == camera_id))
        for row in result.scalars().all():
            await db.delete(row)

    await db.delete(camera)
    await db.commit()
    logger.info("Camera deleted", extra={"camera_id": camera_id})


async def _rename_camera_folder(
    db: AsyncSession, camera_id: int, old_name: str, new_name: str
) -> None:
    """Rename the recording folder on disk and update all DB file paths."""
    config = get_config()
    rec_root = Path(config.storage.recordings_dir)
    old_safe = sanitize_camera_name(old_name)
    new_safe = sanitize_camera_name(new_name)

    old_dir = rec_root / old_safe
    new_dir = rec_root / new_safe

    if old_dir.exists() and not new_dir.exists():
        old_dir.rename(new_dir)
        logger.info("Renamed camera folder", extra={"old": str(old_dir), "new": str(new_dir)})

        # Update all recording paths in DB
        result = await db.execute(
            select(Recording).where(Recording.camera_id == camera_id)
        )
        for rec in result.scalars().all():
            if rec.file_path:
                rec.file_path = rec.file_path.replace(
                    str(old_dir), str(new_dir)
                )
        await db.commit()
        logger.info("Updated recording paths", extra={"camera_id": camera_id})
