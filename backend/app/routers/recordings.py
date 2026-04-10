"""Recording playback API endpoints."""

import hashlib
import logging
from datetime import date, datetime, timedelta
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import FileResponse, StreamingResponse
from sqlalchemy import distinct, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_config
from app.database import get_db
from app.models import Camera, Recording
from app.schemas import RecordingResponse, ThumbnailInfo
from app.services.playback import get_playback_manager
from app.services.thumbnail_capture import get_thumbnail_capture

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


@router.post("/{camera_id}/playback")
async def start_playback_session(
    camera_id: int,
    start: datetime = Query(..., description="Start time ISO format"),
    quality: str = Query("direct", description="Quality tier: direct, high, low, ultralow"),
    direction: str = Query("forward", description="Fallback direction: forward or backward"),
    db: AsyncSession = Depends(get_db),
):
    """Start a playback session for the recording segment at the requested time.

    Direct = raw .ts file (instant). High/Low/Ultra-low = HEVC NVENC transcode
    via fragmented MP4 streaming.
    """
    if quality not in ("direct", "high", "low", "ultralow"):
        quality = "direct"

    camera = await db.get(Camera, camera_id)
    if not camera:
        raise HTTPException(status_code=404, detail="Camera not found")

    # Find the segment that contains 'start'
    result = await db.execute(
        select(Recording)
        .where(
            Recording.camera_id == camera_id,
            Recording.start_time <= start,
            Recording.end_time > start,
        )
        .order_by(Recording.start_time.desc())
        .limit(1)
    )
    seg = result.scalars().first()

    # If no segment contains start, find the nearest one
    if not seg:
        if direction == "backward":
            # For reverse playback: find the latest segment ending at or before start
            result = await db.execute(
                select(Recording)
                .where(
                    Recording.camera_id == camera_id,
                    Recording.end_time <= start,
                )
                .order_by(Recording.end_time.desc())
                .limit(1)
            )
            seg = result.scalars().first()
        else:
            # For forward playback: find the next segment after start
            result = await db.execute(
                select(Recording)
                .where(
                    Recording.camera_id == camera_id,
                    Recording.start_time > start,
                )
                .order_by(Recording.start_time)
                .limit(1)
            )
            seg = result.scalars().first()

    if not seg:
        raise HTTPException(status_code=404, detail="No recordings in range")

    if not Path(seg.file_path).exists():
        raise HTTPException(status_code=404, detail="Recording file missing")

    seek_seconds = max(0.0, (start - seg.start_time).total_seconds())
    seg_end = seg.end_time or (seg.start_time + timedelta(seconds=seg.duration or 900))

    # Check if more segments exist after this one
    has_more_result = await db.execute(
        select(Recording.id)
        .where(
            Recording.camera_id == camera_id,
            Recording.start_time >= seg_end,
        )
        .limit(1)
    )
    has_more = has_more_result.first() is not None

    # All qualities go through PlaybackManager for clean fMP4 output.
    # Direct = ffmpeg -c copy remux (fixes .ts timestamp offset that causes
    # green frames). Transcoded = HEVC NVENC encode.
    session_key = f"{seg.id}-{quality}-{seek_seconds:.0f}"
    session_id = hashlib.md5(session_key.encode()).hexdigest()[:12]

    mgr = get_playback_manager()
    session = await mgr.start_session(
        session_id=session_id,
        segment_paths=[seg.file_path],
        seek_seconds=seek_seconds,
        quality=quality,
        camera_id=camera_id,
    )

    # Wait for transcode to be ready
    await session._ready_event.wait()
    if not session.ready:
        raise HTTPException(status_code=500, detail="Playback transcode failed")

    logger.info(
        "Playback session ready",
        extra={"camera_id": camera_id, "recording_id": seg.id, "quality": quality, "session_id": session_id},
    )

    return {
        "segment_url": f"/api/recordings/playback/{session_id}/playback.mp4",
        "seek_seconds": seek_seconds,  # offset for playhead positioning (seek applied by ffmpeg, player starts at 0)
        "segment_start": seg.start_time.isoformat(),
        "segment_end": seg_end.isoformat(),
        "has_more": has_more,
    }


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


@router.get("/playback/{session_id}/playback.mp4")
async def get_playback_file(session_id: str):
    """Stream a transcoded playback MP4 (fragmented MP4 from PlaybackManager)."""
    mgr = get_playback_manager()
    session = mgr.get_session(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Playback session not found")

    output_path = session.output_dir / "playback.mp4"
    if not output_path.exists():
        raise HTTPException(status_code=404, detail="Playback file not ready")

    if session.streaming and session.process and session.process.returncode is None:
        # Stream the file as it's being written (fragmented MP4)
        import asyncio

        async def stream_fmp4():
            with open(output_path, "rb") as f:
                while True:
                    chunk = f.read(65536)
                    if chunk:
                        yield chunk
                    else:
                        # Check if ffmpeg is still writing
                        if session.process and session.process.returncode is None:
                            await asyncio.sleep(0.1)
                        else:
                            # Read any remaining data
                            remaining = f.read()
                            if remaining:
                                yield remaining
                            break

        mgr.touch(session_id)
        return StreamingResponse(stream_fmp4(), media_type="video/mp4")

    # Non-streaming or completed: serve the full file
    mgr.touch(session_id)
    return FileResponse(output_path, media_type="video/mp4")


@router.get("/{camera_id}/thumbnails", response_model=list[ThumbnailInfo])
async def list_thumbnails(
    camera_id: int,
    date: date = Query(..., description="Date in YYYY-MM-DD format"),
    db: AsyncSession = Depends(get_db),
):
    """List thumbnail metadata for a camera on a date."""
    config = get_config()
    if not config.trickplay.enabled:
        return []

    camera = await db.get(Camera, camera_id)
    if not camera:
        raise HTTPException(status_code=404, detail="Camera not found")

    tp = config.trickplay
    capture = get_thumbnail_capture()
    thumbs = capture.get_thumbnails_for_date(camera.name, str(date))

    return [
        ThumbnailInfo(
            timestamp=t["timestamp"],
            url=f"/api/recordings/{camera_id}/thumb/{date}/{t['filename']}",
            thumb_width=tp.thumb_width,
            thumb_height=tp.thumb_height,
            interval=tp.interval,
        )
        for t in thumbs
    ]


@router.get("/{camera_id}/thumb/{date}/{filename}")
async def get_thumbnail(
    camera_id: int,
    date: str,
    filename: str,
    db: AsyncSession = Depends(get_db),
):
    """Serve an individual thumbnail JPEG."""
    camera = await db.get(Camera, camera_id)
    if not camera:
        raise HTTPException(status_code=404, detail="Camera not found")

    from app.services.ffmpeg import sanitize_camera_name
    config = get_config()
    safe_name = sanitize_camera_name(camera.name)
    path = Path(config.storage.thumbnails_dir) / safe_name / date / "thumbs" / filename

    if not path.exists():
        raise HTTPException(status_code=404, detail="Thumbnail not found")

    return FileResponse(
        path,
        media_type="image/jpeg",
        headers={"Cache-Control": "public, max-age=86400"},
    )
