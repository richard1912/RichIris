"""Recording playback API endpoints."""

import asyncio
import hashlib
import logging
from datetime import date, datetime, timedelta
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import FileResponse
from sqlalchemy import distinct, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_config
from app.database import get_db
from app.models import Camera, Recording
from app.schemas import RecordingResponse, ThumbnailSpriteInfo
from app.services.playback import get_playback_manager
from app.services.thumbnail import get_thumbnail_generator

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/recordings", tags=["recordings"])


@router.get("/{camera_id}/dates", response_model=list[str])
async def list_recording_dates(camera_id: int, db: AsyncSession = Depends(get_db)):
    """List all dates that have recordings for a camera."""
    camera = await db.get(Camera, camera_id)
    if not camera:
        raise HTTPException(status_code=404, detail="Camera not found")

    date_col = func.date(Recording.start_time).label("rec_date")
    result = await db.execute(
        select(distinct(date_col))
        .where(Recording.camera_id == camera_id)
        .order_by(date_col.desc())
    )
    dates = [str(row[0]) for row in result.all()]
    logger.debug("Listed recording dates", extra={"camera_id": camera_id, "count": len(dates)})
    return dates


@router.get("/{camera_id}/segments", response_model=list[RecordingResponse])
async def list_segments(
    camera_id: int,
    date: date = Query(..., description="Date in YYYY-MM-DD format"),
    db: AsyncSession = Depends(get_db),
):
    """List all recording segments for a camera on a given date."""
    camera = await db.get(Camera, camera_id)
    if not camera:
        raise HTTPException(status_code=404, detail="Camera not found")

    start = datetime.combine(date, datetime.min.time())
    end = datetime.combine(date, datetime.max.time())

    result = await db.execute(
        select(Recording)
        .where(
            Recording.camera_id == camera_id,
            Recording.start_time >= start,
            Recording.start_time <= end,
        )
        .order_by(Recording.start_time)
    )
    segments = result.scalars().all()
    logger.debug(
        "Listed segments",
        extra={"camera_id": camera_id, "date": str(date), "count": len(segments)},
    )
    return segments


PLAYBACK_WINDOW = 1800  # 30 minutes


@router.post("/{camera_id}/playback")
async def start_playback_session(
    camera_id: int,
    start: datetime = Query(..., description="Start time ISO format"),
    db: AsyncSession = Depends(get_db),
):
    """Start a playback session (instant MP4 remux, no transcode)."""
    camera = await db.get(Camera, camera_id)
    if not camera:
        raise HTTPException(status_code=404, detail="Camera not found")

    window_end = start + timedelta(seconds=PLAYBACK_WINDOW)

    # Segments overlapping [start, window_end]
    result = await db.execute(
        select(Recording)
        .where(
            Recording.camera_id == camera_id,
            Recording.start_time < window_end,
            Recording.end_time > start,
        )
        .order_by(Recording.start_time)
    )
    segments = result.scalars().all()

    if not segments:
        raise HTTPException(status_code=404, detail="No recordings in range")

    segment_paths = [str(Path(s.file_path)) for s in segments if Path(s.file_path).exists()]
    if not segment_paths:
        raise HTTPException(status_code=404, detail="Recording files missing")

    # Calculate seek offset if start is mid-segment
    seek_seconds = 0.0
    first_seg = segments[0]
    if first_seg.start_time < start:
        seek_seconds = (start - first_seg.start_time).total_seconds()

    # Calculate actual duration to remux (may be less than full window)
    last_seg = segments[-1]
    last_end = last_seg.end_time or (last_seg.start_time + timedelta(seconds=last_seg.duration or 900))
    actual_end = min(window_end, last_end)
    duration_limit = (actual_end - start).total_seconds()

    # Check if more recordings exist beyond this window
    has_more_result = await db.execute(
        select(Recording.id)
        .where(
            Recording.camera_id == camera_id,
            Recording.start_time >= window_end,
        )
        .limit(1)
    )
    has_more = has_more_result.first() is not None

    key = f"{camera_id}:{start.isoformat()}"
    session_id = hashlib.md5(key.encode()).hexdigest()[:12]

    mgr = get_playback_manager()
    session = await mgr.start_session(
        session_id, segment_paths,
        seek_seconds=seek_seconds, duration_limit=duration_limit,
    )

    # Wait for MP4 to be ready (remux is near-instant, typically < 1 second)
    for _ in range(20):
        await asyncio.sleep(0.25)
        if session.ready:
            return {
                "session_id": session_id,
                "playback_url": f"/api/recordings/playback/{session_id}/playback.mp4",
                "window_end": actual_end.isoformat(),
                "has_more": has_more,
            }

    raise HTTPException(status_code=503, detail="Remux in progress, retry shortly")


@router.get("/playback/{session_id}/playback.mp4")
async def get_playback_mp4(session_id: str):
    """Serve the remuxed playback MP4 file."""
    mgr = get_playback_manager()
    session = mgr.get_session(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Playback session not found")

    mp4_path = session.output_dir / "playback.mp4"
    if not mp4_path.exists():
        raise HTTPException(status_code=404, detail="Playback not ready")

    return FileResponse(mp4_path, media_type="video/mp4")


@router.get("/segment/{recording_id}")
async def get_segment_file(recording_id: int, db: AsyncSession = Depends(get_db)):
    """Serve a specific recording segment file (raw)."""
    recording = await db.get(Recording, recording_id)
    if not recording:
        raise HTTPException(status_code=404, detail="Recording not found")

    path = Path(recording.file_path)
    if not path.exists():
        raise HTTPException(status_code=404, detail="Segment file missing")

    return FileResponse(path, media_type="video/mp2t")


@router.get("/{camera_id}/thumbnails", response_model=list[ThumbnailSpriteInfo])
async def list_thumbnails(
    camera_id: int,
    date: date = Query(..., description="Date in YYYY-MM-DD format"),
    db: AsyncSession = Depends(get_db),
):
    """List thumbnail sprite metadata for all segments on a date."""
    config = get_config()
    if not config.trickplay.enabled:
        return []

    camera = await db.get(Camera, camera_id)
    if not camera:
        raise HTTPException(status_code=404, detail="Camera not found")

    start = datetime.combine(date, datetime.min.time())
    end = datetime.combine(date, datetime.max.time())

    result = await db.execute(
        select(Recording)
        .where(
            Recording.camera_id == camera_id,
            Recording.start_time >= start,
            Recording.start_time <= end,
            Recording.has_thumbnail == True,
        )
        .order_by(Recording.start_time)
    )
    recordings = result.scalars().all()

    tp = config.trickplay
    sprites = []
    for rec in recordings:
        duration = rec.duration or 900.0
        frame_count = max(1, int(duration / tp.interval))
        cols = min(frame_count, 10)
        import math
        rows = math.ceil(frame_count / cols)
        sprites.append(ThumbnailSpriteInfo(
            recording_id=rec.id,
            start_time=rec.start_time,
            end_time=rec.end_time or rec.start_time,
            duration=duration,
            sprite_url=f"/api/recordings/thumbnail/{rec.id}",
            interval=tp.interval,
            cols=cols,
            rows=rows,
            thumb_width=tp.thumb_width,
            thumb_height=tp.thumb_height,
        ))
    return sprites


@router.get("/thumbnail/{recording_id}")
async def get_thumbnail_sprite(recording_id: int, db: AsyncSession = Depends(get_db)):
    """Serve a thumbnail sprite JPEG."""
    recording = await db.get(Recording, recording_id)
    if not recording:
        raise HTTPException(status_code=404, detail="Recording not found")

    gen = get_thumbnail_generator()
    path = gen.get_sprite_path(recording.camera_id, recording_id)
    if not path:
        raise HTTPException(status_code=404, detail="Thumbnail not found")

    return FileResponse(
        path,
        media_type="image/jpeg",
        headers={"Cache-Control": "public, max-age=86400"},
    )
