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

from app.config import get_config, local_now
from app.database import get_db
from app.models import Camera, Recording, RivFile
from app.schemas import RecordingResponse, ThumbnailInfo
from app.services.benchmark import BenchmarkTrace
from app.services.playback import get_playback_manager, probe_duration
from app.services.thumbnail_capture import get_thumbnail_capture

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/recordings", tags=["recordings"])

# Newest playback request per camera, by arrival order. There is ONE playback
# session per camera, and requests are handled concurrently, so a slow older
# request (segment lookup/probe) used to start late and evict the session of a
# NEWER one, which then failed with a 500 the client showed as an error
# (pinch-zooming the Android timeline, 2026-11-14). The newest request now
# always wins and a superseded one gets 409, which clients drop or retry.
_playback_seq = 0
_latest_playback_req: dict[int, int] = {}


def _superseded() -> HTTPException:
    return HTTPException(status_code=409, detail="Superseded by a newer playback request")


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
    dates = {str(row[0]) for row in result.all()}
    riv_col = func.date(RivFile.start_time).label("riv_date")
    riv = await db.execute(select(distinct(riv_col)).where(
        RivFile.camera_id == camera_id, RivFile.stream == "s1"))
    dates |= {str(row[0]) for row in riv.all()}
    dates = sorted(dates, reverse=True)
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
    segments: list = list(result.scalars().all())
    # RIV footage (main stream) as segments too, so the timeline shows it.
    # Negative ids keep them apart from MPEG-TS rows; clients only merge by time.
    riv = await db.execute(
        select(RivFile).where(RivFile.camera_id == camera_id, RivFile.stream == "s1",
                              RivFile.start_time >= start, RivFile.start_time <= end)
        .order_by(RivFile.start_time))
    now = local_now()
    for r in riv.scalars().all():
        end_time = r.end_time or (now if r.in_progress else None)
        segments.append(RecordingResponse(
            id=-r.id, camera_id=r.camera_id, file_path=r.file_path, start_time=r.start_time,
            end_time=end_time, file_size=r.file_size,
            duration=r.duration if r.duration is not None else (
                (end_time - r.start_time).total_seconds() if end_time else None),
            in_progress=r.in_progress))
    segments.sort(key=lambda x: x.start_time)
    logger.debug(
        "Listed segments",
        extra={"camera_id": camera_id, "date": str(date), "count": len(segments)},
    )
    return segments


# Forward requests closer than this to the end of a closed segment start at
# the next segment instead (see start_playback_session).
SEGMENT_TAIL_SECONDS = 1.5


async def _riv_playback(camera: Camera, start: datetime, quality: str,
                        direction: str, stream: str, speed: int = 1) -> dict | None:
    """Serve from RIV when it holds the footage (services/riv/serve.py).

    Same JSON contract as a session, so clients need no change: the URL is a
    continuous fMP4 whose position 0 is the keyframe at ``segment_start``
    (exact), ``seek_seconds`` is 0, and it runs across file boundaries and
    into live. No ffmpeg, no session, nothing to evict. ``segment_end`` is
    where the continuous footage stops (a gap), which is where a client asks
    again; ``has_more`` is false while the stream reaches live.

    Direct quality, forward only for now; everything else uses the MPEG-TS
    path while the camera still records it ("shadow").
    """
    fmt = camera.rec_format or "ts"
    if fmt == "ts":
        return None
    from app.routers.riv import to_iso, to_ms
    from app.services.riv.serve import find_run
    start_ms = to_ms(start.isoformat())
    if direction == "backward":
        return await _riv_reverse(camera, start_ms, stream, fmt)
    if speed >= 8:
        # Keyframe fast forward (any quality: it is one keyframe per GOP).
        run = await find_run(camera.id, stream, start_ms)
        if run is None or (fmt == "shadow" and run.start_ms > start_ms + 5_000):
            return None
        seg_start = to_iso(run.start_ms)
        return {
            "segment_url": f"/api/riv/{camera.id}/stream.mp4?start={seg_start}&stream={stream}&speed={speed}",
            "seek_seconds": 0.0, "segment_start": seg_start,
            "segment_end": to_iso(run.end_ms), "has_more": False,
            "direction": "forward", "format": "riv", "speed": speed,
        }
    if quality != "direct":
        return await _riv_transcoded(camera, start_ms, stream, quality, fmt)
    run = await find_run(camera.id, stream, start_ms)
    if run is None:
        return None
    if fmt == "shadow" and run.start_ms > start_ms + 5_000:
        return None     # RIV starts later than asked: the .ts may have it
    seg_start = to_iso(run.start_ms)
    return {
        "segment_url": f"/api/riv/{camera.id}/stream.mp4?start={seg_start}&stream={stream}",
        "seek_seconds": 0.0,
        "segment_start": seg_start,
        "segment_end": to_iso(run.end_ms),
        "has_more": not run.live,
        "direction": "forward",
        "format": "riv",
    }


async def _riv_reverse(camera: Camera, start_ms: int, stream: str, fmt: str) -> dict | None:
    """Reverse from RIV: keyframe-aligned chunks, each read from the RIV
    stream endpoint between two keyframes (no -ss, no mid-GOP decoder starts)."""
    from app.config import get_bootstrap
    from app.routers.riv import to_iso
    from app.services.riv.serve import reverse_chunks
    found = await reverse_chunks(camera.id, stream, start_ms)
    if found is None:
        return None
    chunks, has_more = found
    if fmt == "shadow" and chunks[0][1] < start_ms - 5_000:
        return None
    base = f"http://127.0.0.1:{get_bootstrap().port}/api/riv/{camera.id}/stream.mp4"
    plan = [(float(s), (e - s) / 1000) for s, e in chunks]

    def input_for(seek: float, duration: float) -> list[str]:
        s_ms = int(seek)
        e_ms = s_ms + int(round(duration * 1000))
        return ["-i", f"{base}?start={to_iso(s_ms)}&end={to_iso(e_ms)}&stream={stream}"]

    earliest = chunks[-1][0]
    newest = chunks[0][1]
    key = f"riv-{camera.id}-{stream}-{newest}-backward"
    session_id = hashlib.md5(key.encode()).hexdigest()[:12]
    mgr = get_playback_manager()
    session = await mgr.start_session(
        session_id=session_id, segment_paths=[], seek_seconds=(newest - earliest) / 1000,
        quality="direct", camera_id=camera.id, direction="backward",
        reverse_plan=plan, reverse_input=input_for,
    )
    await session._ready_event.wait()
    if not session.ready:
        raise HTTPException(status_code=500, detail="Reverse render failed")
    return {
        "segment_url": f"/api/recordings/playback/{session_id}/playback.mp4",
        "seek_seconds": (newest - earliest) / 1000,
        "segment_start": to_iso(earliest),
        "segment_end": to_iso(newest),
        "has_more": has_more,
        "direction": "backward",
        "format": "riv",
    }


async def _riv_transcoded(camera: Camera, start_ms: int, stream: str, quality: str,
                          fmt: str) -> dict | None:
    """Transcoded qualities from RIV: the same ffmpeg presets, reading the
    continuous RIV stream (local HTTP) instead of -ss into a .ts file."""
    from app.config import get_bootstrap
    from app.routers.riv import to_iso
    from app.services.riv.serve import find_run
    run = await find_run(camera.id, stream, start_ms)
    if run is None or (fmt == "shadow" and run.start_ms > start_ms + 5_000):
        return None
    src = (f"http://127.0.0.1:{get_bootstrap().port}/api/riv/{camera.id}/stream.mp4"
           f"?start={to_iso(run.start_ms)}&stream={stream}")
    key = f"riv-{camera.id}-{stream}-{run.start_ms}-{quality}"
    session_id = hashlib.md5(key.encode()).hexdigest()[:12]
    mgr = get_playback_manager()
    session = await mgr.start_session(
        session_id=session_id, segment_paths=[src], seek_seconds=0.0,
        quality=quality, camera_id=camera.id, direction="forward",
    )
    await session._ready_event.wait()
    if not session.ready:
        raise HTTPException(status_code=500, detail="Playback transcode failed")
    return {
        "segment_url": f"/api/recordings/playback/{session_id}/playback.mp4",
        "seek_seconds": 0.0,
        "segment_start": to_iso(run.start_ms),
        "segment_end": to_iso(run.end_ms),
        "has_more": not run.live,
        "direction": "forward",
        "format": "riv",
    }


@router.post("/{camera_id}/playback")
async def start_playback_session(
    camera_id: int,
    start: datetime = Query(..., description="Start time ISO format"),
    quality: str = Query("direct", description="Quality tier: direct, high, low, ultralow"),
    direction: str = Query("forward", description="forward, or backward for a server-rendered reverse stream"),
    stream: str = Query("s1", pattern="^s[12]$", description="RIV only: s1 main, s2 sub stream"),
    speed: int = Query(1, ge=1, le=64, description="RIV only: >= 8 = keyframe fast forward"),
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

    global _playback_seq
    _playback_seq += 1
    req_seq = _playback_seq
    _latest_playback_req[camera_id] = req_seq

    camera = await db.get(Camera, camera_id)
    if not camera:
        raise HTTPException(status_code=404, detail="Camera not found")

    riv = await _riv_playback(camera, start, quality, direction, stream, speed)
    if riv is not None:
        bench.mark("riv_resolved")
        bench.summary()
        return riv

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
            # For forward playback: a growing segment's end_time is only
            # refreshed every ~15s, so a request a few seconds behind "now"
            # (the 30s skip from live, or a forward skip near the tail) can
            # fall past the recorded end_time while the file itself already
            # holds those frames. Prefer that segment over the "next one".
            result = await db.execute(
                select(Recording)
                .where(
                    Recording.camera_id == camera_id,
                    Recording.in_progress == True,  # noqa: E712
                    Recording.start_time <= start,
                )
                .order_by(Recording.start_time.desc())
                .limit(1)
            )
            seg = result.scalars().first()
            if not seg:
                # Otherwise the next segment after start
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

    # The segment after this one, if any. Its existence also tells us this
    # segment is no longer being written, whatever its in_progress flag says:
    # the scanner only clears that flag a minute or two after the rollover.
    result = await db.execute(
        select(Recording)
        .where(
            Recording.camera_id == camera_id,
            Recording.start_time > seg.start_time,
        )
        .order_by(Recording.start_time)
        .limit(1)
    )
    nxt = result.scalars().first()
    growing = bool(seg.in_progress) and nxt is None

    # A forward request that lands in the last moments of a FINISHED segment
    # must roll into the next one. The grid continues on its master clock,
    # which can sit a few ms before this camera's segment end. Two freezes
    # came from serving that tail (both PTZ 49, 2026-11-18): a closed segment
    # gave an empty fMP4, so the player never reported "completed" (06:06);
    # and a just-rolled segment still flagged in_progress was clamped 3 s
    # back, so the last seconds replayed in a loop (09:46).
    if direction != "backward" and not growing and nxt is not None:
        end = seg.end_time
        if seg.in_progress:
            actual = await probe_duration(seg.file_path)
            if actual is not None:
                end = seg.start_time + timedelta(seconds=actual)
        if end and (end - start).total_seconds() < SEGMENT_TAIL_SECONDS:
            if Path(nxt.file_path).exists():
                seg = nxt
                growing = bool(seg.in_progress)

    if not Path(seg.file_path).exists():
        raise HTTPException(status_code=404, detail="Recording file missing")

    seek_seconds = max(0.0, (start - seg.start_time).total_seconds())
    seg_end = seg.end_time or (seg.start_time + timedelta(seconds=seg.duration or 900))
    if direction != "backward" and growing:
        # Never seek past what is on disk: ffmpeg would read to EOF and emit
        # nothing, and the session would fail as "transcode failed". Stay a
        # couple of seconds inside so there is a keyframe to land on.
        actual = await probe_duration(seg.file_path)
        if actual is not None:
            seek_seconds = min(seek_seconds, max(0.0, actual - 3.0))
    if direction == "backward":
        # seek_seconds becomes the point the reverse render starts FROM. A
        # request past the segment's end (a gap, or "now" on a growing file)
        # clamps to what is actually on disk, so playback begins at the last
        # recorded frame.
        actual = await probe_duration(seg.file_path)
        if actual is not None:
            seek_seconds = min(seek_seconds, max(0.0, actual - 0.2))
    bench.mark("segment_resolved", segment_id=seg.id, seek_seconds=round(seek_seconds, 2),
               in_progress=seg.in_progress)

    # Whether playback can continue past this segment in the travel direction
    if direction == "backward":
        more_cond = Recording.start_time < seg.start_time
    else:
        # Not ">= seg_end": a segment's probed end can overlap the next
        # one's start by up to a second, which hid the only later segment.
        more_cond = Recording.start_time > seg.start_time
    has_more_result = await db.execute(
        select(Recording.id)
        .where(Recording.camera_id == camera_id, more_cond)
        .limit(1)
    )
    has_more = has_more_result.first() is not None

    # All qualities go through PlaybackManager for clean fMP4 output.
    # Direct = ffmpeg -c copy remux with -ss pre-seek (server-side seek shifts
    # the work off libmpv so first frame is faster than letting libmpv binary-
    # search a raw .ts for the target PTS). Transcoded = HEVC NVENC encode.
    session_key = f"{seg.id}-{quality}-{seek_seconds:.0f}-{direction}"
    session_id = hashlib.md5(session_key.encode()).hexdigest()[:12]

    if _latest_playback_req.get(camera_id) != req_seq:
        # A newer request arrived while we were resolving the segment: do not
        # start (and so do not evict the newer request's session).
        raise _superseded()

    mgr = get_playback_manager()
    session = await mgr.start_session(
        session_id=session_id,
        segment_paths=[seg.file_path],
        seek_seconds=seek_seconds,
        quality=quality,
        camera_id=camera_id,
        bench=bench,
        direction="backward" if direction == "backward" else "forward",
    )
    bench.mark("session_started", session_id=session_id, streaming=session.streaming)

    # Wait for transcode to be ready
    await session._ready_event.wait()
    bench.mark("ready_event_set", ready=session.ready)
    if not session.ready:
        bench.summary()
        if _latest_playback_req.get(camera_id) != req_seq or mgr.get_session(session_id) is not session:
            # Evicted before it produced output: not a failure, just replaced.
            raise _superseded()
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
        "direction": session.direction,
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
        # A reverse render writes in ~8s chunks and can pause for several
        # seconds between them on a busy box; a plain transcode streams
        # continuously, so 5s of silence there really does mean it died.
        max_stalls = 1200 if session.renderer else 100  # x50ms

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
                        # A client that is still consuming keeps the session
                        # alive; without this the idle sweep (30s) tears down
                        # a long transcode or reverse render mid-stream.
                        mgr.touch(session_id)
                        if session.renderer:
                            # Lets the reverse renderer pace itself to us.
                            session.renderer.bytes_consumed = max(
                                session.renderer.bytes_consumed, sent)
                        yield chunk
                    else:
                        if session.process and session.process.returncode is None:
                            stalls += 1
                            if stalls > max_stalls:
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
                mgr.touch(session_id)
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
