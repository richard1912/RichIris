"""Recording playback API endpoints."""

import asyncio
import hashlib
import logging
from datetime import date, datetime
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import FileResponse, Response
from sqlalchemy import distinct, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models import Camera, Recording
from app.schemas import RecordingResponse
from app.services.playback import get_playback_manager

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
    end: datetime = Query(..., description="End time ISO format"),
    db: AsyncSession = Depends(get_db),
):
    """Start a transcoded playback session. Returns the session playlist URL."""
    camera = await db.get(Camera, camera_id)
    if not camera:
        raise HTTPException(status_code=404, detail="Camera not found")

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

    if not segments:
        raise HTTPException(status_code=404, detail="No recordings in range")

    segment_paths = [str(Path(s.file_path)) for s in segments if Path(s.file_path).exists()]
    if not segment_paths:
        raise HTTPException(status_code=404, detail="Recording files missing")

    key = f"{camera_id}:{start.isoformat()}:{end.isoformat()}"
    session_id = hashlib.md5(key.encode()).hexdigest()[:12]

    mgr = get_playback_manager()
    session = await mgr.start_session(session_id, segment_paths)

    # Wait for playlist to become available
    for _ in range(30):
        await asyncio.sleep(0.5)
        playlist = session.output_dir / "playback.m3u8"
        if playlist.exists() and playlist.stat().st_size > 0:
            return {
                "session_id": session_id,
                "playlist_url": f"/api/recordings/playback/{session_id}/playback.m3u8",
            }

    raise HTTPException(status_code=503, detail="Transcoding in progress, retry shortly")


@router.get("/playback/{session_id}/playback.m3u8")
async def get_playback_session_playlist(session_id: str):
    """Serve the evolving playback HLS playlist (for HLS.js polling)."""
    mgr = get_playback_manager()
    session = mgr.get_session(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Playback session not found")

    playlist = session.output_dir / "playback.m3u8"
    if not playlist.exists():
        raise HTTPException(status_code=404, detail="Playlist not ready")

    content = playlist.read_text()
    lines = []
    for line in content.split("\n"):
        if line.startswith("seg_"):
            lines.append(f"/api/recordings/playback/{session_id}/{line}")
        else:
            lines.append(line)

    return Response(
        content="\n".join(lines),
        media_type="application/vnd.apple.mpegurl",
        headers={"Cache-Control": "no-cache, no-store"},
    )


@router.get("/playback/{session_id}/{filename}")
async def get_playback_segment(session_id: str, filename: str):
    """Serve a transcoded playback HLS segment."""
    mgr = get_playback_manager()
    session = mgr.get_session(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Playback session not found")

    file_path = session.output_dir / filename
    if not file_path.exists():
        raise HTTPException(status_code=404, detail="Segment not found")

    return FileResponse(file_path, media_type="video/mp2t")


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
