"""Recording playback API endpoints."""

import asyncio
import hashlib
import logging
from datetime import date, datetime, timedelta
from pathlib import Path

from fastapi import APIRouter, Depends, Header, HTTPException, Query
from fastapi.responses import FileResponse, StreamingResponse
from sqlalchemy import distinct, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_config
from app.database import get_db
from app.models import Camera, Recording
from app.schemas import RecordingResponse, ThumbnailInfo
from app.services.benchmark import BenchmarkTrace
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
    x_bench_id: str | None = Header(default=None),
    db: AsyncSession = Depends(get_db),
):
    """Start a playback session for the recording segment at the requested time.

    Direct = raw .ts file (instant). High/Low/Ultra-low = HEVC NVENC transcode
    via fragmented MP4 streaming.
    """
    if quality not in ("direct", "high", "low", "ultralow"):
        quality = "direct"

    bench = BenchmarkTrace(x_bench_id, camera_id=camera_id, quality=quality)
    bench.mark("request_received", start=start.isoformat())

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
    bench.mark("segment_resolved", segment_id=seg.id, seek_seconds=round(seek_seconds, 2),
               in_progress=seg.in_progress)

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
    # Direct = ffmpeg -c copy remux with -ss pre-seek (server-side seek shifts
    # the work off libmpv so first frame is faster than letting libmpv binary-
    # search a raw .ts for the target PTS). Transcoded = HEVC NVENC encode.
    session_key = f"{seg.id}-{quality}-{seek_seconds:.0f}"
    session_id = hashlib.md5(session_key.encode()).hexdigest()[:12]

    mgr = get_playback_manager()
    session = await mgr.start_session(
        session_id=session_id,
        segment_paths=[seg.file_path],
        seek_seconds=seek_seconds,
        quality=quality,
        camera_id=camera_id,
        bench=bench,
    )
    bench.mark("session_started", session_id=session_id, streaming=session.streaming)

    # Wait for transcode to be ready
    await session._ready_event.wait()
    bench.mark("ready_event_set", ready=session.ready)
    if not session.ready:
        bench.summary()
        raise HTTPException(status_code=500, detail="Playback transcode failed")

    logger.info(
        "Playback session ready",
        extra={"camera_id": camera_id, "recording_id": seg.id, "quality": quality, "session_id": session_id},
    )
    bench.summary()

    return {
        "segment_url": f"/api/recordings/playback/{session_id}/playback.mp4",
        "seek_seconds": seek_seconds,  # offset for playhead positioning (seek applied by ffmpeg, player starts at 0)
        "segment_start": seg.start_time.isoformat(),
        "segment_end": seg_end.isoformat(),
        "has_more": has_more,
    }


@router.get("/segment/{recording_id}")
async def get_segment_file(
    recording_id: int,
    range: str | None = Header(default=None),
    x_bench_id: str | None = Header(default=None),
    db: AsyncSession = Depends(get_db),
):
    """Serve a raw .ts recording segment. FileResponse handles Range natively."""
    recording = await db.get(Recording, recording_id)
    if not recording:
        raise HTTPException(status_code=404, detail="Recording not found")

    path = Path(recording.file_path)
    if not path.exists():
        raise HTTPException(status_code=404, detail="Segment file missing")

    if x_bench_id:
        logger.info(
            "[BENCH:%s] ts_request_received recording=%d range=%s size=%d",
            x_bench_id, recording_id, range or "-", path.stat().st_size,
        )

    return FileResponse(path, media_type="video/mp2t")


def _parse_range(range_header: str | None) -> tuple[int, int | None] | None:
    """Parse a Range header like 'bytes=N-M' or 'bytes=N-'. Returns (start, end_inclusive)."""
    if not range_header:
        return None
    try:
        unit, _, spec = range_header.partition("=")
        if unit.strip().lower() != "bytes":
            return None
        first = spec.split(",")[0].strip()
        start_s, _, end_s = first.partition("-")
        start = int(start_s) if start_s else 0
        end = int(end_s) if end_s else None
        return start, end
    except (ValueError, AttributeError):
        return None


@router.get("/playback/{session_id}/playback.mp4")
async def get_playback_file(
    session_id: str,
    range: str | None = Header(default=None),
    x_bench_id: str | None = Header(default=None),
):
    """Stream a transcoded playback MP4 (fragmented MP4 from PlaybackManager).

    Supports HTTP Range requests so libmpv can issue ranged GETs for individual
    fMP4 fragments instead of re-downloading the whole growing file on every
    reconnect.
    """
    mgr = get_playback_manager()
    session = mgr.get_session(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Playback session not found")

    output_path = session.output_dir / "playback.mp4"
    if not output_path.exists():
        raise HTTPException(status_code=404, detail="Playback file not ready")

    bench_id = x_bench_id or (session._bench.bench_id if session._bench else None)
    is_growing = bool(
        session.streaming and session.process and session.process.returncode is None
    )
    file_size = (await asyncio.to_thread(output_path.stat)).st_size
    parsed = _parse_range(range)

    if bench_id:
        logger.info(
            "[BENCH:%s] mp4_request_received session=%s range=%s file_size=%d growing=%s",
            bench_id, session_id, range or "-", file_size, is_growing,
        )

    mgr.touch(session_id)

    # Growing file: ignore Range and stream from byte 0 (live-stream semantics).
    # We can't honor Range because we can't promise an end byte for a file whose
    # final size is unknown, and libmpv strictly enforces 206 Content-Range bounds.
    # Once ffmpeg finishes the segment becomes seekable via the path below.
    #
    # File reads are offloaded to a thread via asyncio.to_thread so concurrent
    # streams (grid view, multiple clients) can't starve the event loop — a
    # single slow disk read used to block all other handlers including
    # /api/health, which caused the watchdog to restart the service.
    if is_growing:
        async def stream_growing():
            sent = 0
            f = await asyncio.to_thread(open, output_path, "rb")
            try:
                stalls = 0
                while True:
                    chunk = await asyncio.to_thread(f.read, 65536)
                    if chunk:
                        stalls = 0
                        if sent == 0 and bench_id:
                            logger.info(
                                "[BENCH:%s] mp4_first_chunk_sent bytes=%d offset=0 mode=growing",
                                bench_id, len(chunk),
                            )
                        sent += len(chunk)
                        yield chunk
                    else:
                        if session.process and session.process.returncode is None:
                            stalls += 1
                            if stalls > 100:
                                break
                            await asyncio.sleep(0.05)
                        else:
                            break
            finally:
                await asyncio.to_thread(f.close)

        return StreamingResponse(
            stream_growing(), media_type="video/mp4",
            headers={"Accept-Ranges": "none", "Cache-Control": "no-cache"},
        )

    # Completed file path (file_size is final and accurate).
    if parsed is None:
        return FileResponse(
            output_path, media_type="video/mp4",
            headers={"Accept-Ranges": "bytes"},
        )

    start, end = parsed
    # Clamp range to actual file bounds.
    if start >= file_size:
        raise HTTPException(
            status_code=416, detail="Range not satisfiable",
            headers={"Content-Range": f"bytes */{file_size}"},
        )
    last = file_size - 1 if end is None else min(end, file_size - 1)
    length = last - start + 1

    async def stream_range():
        sent = 0
        f = await asyncio.to_thread(open, output_path, "rb")
        try:
            await asyncio.to_thread(f.seek, start)
            remaining = length
            while remaining > 0:
                chunk = await asyncio.to_thread(f.read, min(65536, remaining))
                if not chunk:
                    break
                if sent == 0 and bench_id:
                    logger.info(
                        "[BENCH:%s] mp4_first_chunk_sent bytes=%d offset=%d mode=range",
                        bench_id, len(chunk), start,
                    )
                sent += len(chunk)
                remaining -= len(chunk)
                yield chunk
        finally:
            await asyncio.to_thread(f.close)

    return StreamingResponse(
        stream_range(), status_code=206, media_type="video/mp4",
        headers={
            "Accept-Ranges": "bytes",
            "Cache-Control": "no-cache",
            "Content-Range": f"bytes {start}-{last}/{file_size}",
            "Content-Length": str(length),
        },
    )


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
