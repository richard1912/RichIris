"""Camera CRUD API endpoints."""

import logging

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models import Camera
from app.schemas import CameraCreate, CameraResponse, CameraUpdate
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
    camera = Camera(name=data.name, rtsp_url=data.rtsp_url, enabled=data.enabled)
    db.add(camera)
    await db.commit()
    await db.refresh(camera)
    logger.info("Camera created", extra={"camera_id": camera.id, "name": camera.name})

    if camera.enabled:
        mgr = get_stream_manager()
        await mgr.start_stream(camera.id, camera.name, camera.rtsp_url)

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

    if data.name is not None:
        camera.name = data.name
        needs_restart = True
    if data.rtsp_url is not None:
        camera.rtsp_url = data.rtsp_url
        needs_restart = True
    if data.enabled is not None:
        camera.enabled = data.enabled

    await db.commit()
    await db.refresh(camera)
    logger.info("Camera updated", extra={"camera_id": camera.id})

    if not camera.enabled:
        await mgr.stop_stream(camera.id)
    elif needs_restart:
        await mgr.stop_stream(camera.id)
        await mgr.start_stream(camera.id, camera.name, camera.rtsp_url)

    return camera


@router.delete("/{camera_id}", status_code=204)
async def delete_camera(camera_id: int, db: AsyncSession = Depends(get_db)):
    """Delete a camera and stop its stream."""
    camera = await db.get(Camera, camera_id)
    if not camera:
        raise HTTPException(status_code=404, detail="Camera not found")

    mgr = get_stream_manager()
    await mgr.stop_stream(camera.id)

    await db.delete(camera)
    await db.commit()
    logger.info("Camera deleted", extra={"camera_id": camera_id})
