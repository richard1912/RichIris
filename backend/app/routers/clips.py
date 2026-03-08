"""Clip export API endpoints."""

import asyncio
import logging
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db, get_session_factory
from app.models import Camera, ClipExport
from app.schemas import ClipExportCreate, ClipExportResponse
from app.services.clip_exporter import export_clip

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/clips", tags=["clips"])


@router.post("", response_model=ClipExportResponse, status_code=201)
async def create_clip(body: ClipExportCreate, db: AsyncSession = Depends(get_db)):
    """Create a new clip export job."""
    camera = await db.get(Camera, body.camera_id)
    if not camera:
        raise HTTPException(status_code=404, detail="Camera not found")

    if body.end_time <= body.start_time:
        raise HTTPException(status_code=400, detail="end_time must be after start_time")

    clip = ClipExport(
        camera_id=body.camera_id,
        start_time=body.start_time,
        end_time=body.end_time,
        status="pending",
    )
    db.add(clip)
    await db.commit()
    await db.refresh(clip)

    logger.info("Clip export created", extra={"clip_id": clip.id, "camera_id": body.camera_id})

    # Launch background export
    factory = get_session_factory()
    asyncio.create_task(export_clip(clip.id, factory))

    return clip


@router.get("", response_model=list[ClipExportResponse])
async def list_clips(
    camera_id: int | None = None,
    db: AsyncSession = Depends(get_db),
):
    """List clip exports, optionally filtered by camera."""
    query = select(ClipExport).order_by(ClipExport.created_at.desc())
    if camera_id is not None:
        query = query.where(ClipExport.camera_id == camera_id)
    result = await db.execute(query)
    return result.scalars().all()


@router.get("/{clip_id}", response_model=ClipExportResponse)
async def get_clip(clip_id: int, db: AsyncSession = Depends(get_db)):
    """Get status of a clip export."""
    clip = await db.get(ClipExport, clip_id)
    if not clip:
        raise HTTPException(status_code=404, detail="Clip not found")
    return clip


@router.get("/{clip_id}/download")
async def download_clip(clip_id: int, db: AsyncSession = Depends(get_db)):
    """Download a completed clip export."""
    clip = await db.get(ClipExport, clip_id)
    if not clip:
        raise HTTPException(status_code=404, detail="Clip not found")

    if clip.status != "done":
        raise HTTPException(status_code=409, detail=f"Clip is {clip.status}, not ready for download")

    if not clip.file_path:
        raise HTTPException(status_code=404, detail="Clip file path not set")

    path = Path(clip.file_path)
    if not path.exists():
        raise HTTPException(status_code=404, detail="Clip file missing from disk")

    return FileResponse(
        path,
        media_type="video/mp4",
        filename=path.name,
    )


@router.delete("/{clip_id}", status_code=204)
async def delete_clip(clip_id: int, db: AsyncSession = Depends(get_db)):
    """Delete a clip export and its file."""
    clip = await db.get(ClipExport, clip_id)
    if not clip:
        raise HTTPException(status_code=404, detail="Clip not found")

    if clip.file_path:
        path = Path(clip.file_path)
        path.unlink(missing_ok=True)
        logger.info("Deleted clip file", extra={"clip_id": clip_id, "path": str(path)})

    await db.delete(clip)
    await db.commit()
