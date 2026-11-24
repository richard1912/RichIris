"""RIV recordings: writer stats, file listing and the continuous playback stream."""

from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import StreamingResponse
from sqlalchemy import select

from app.config import get_tz
from app.database import get_session_factory
from app.models import RivFile
from app.services.riv.manager import get_riv_manager, local_dt
from app.services.riv.serve import find_run, stream_fmp4, stream_keyframes

router = APIRouter(prefix="/api/riv", tags=["riv"])


def to_ms(iso: str) -> int:
    """Naive local ISO (what every client sends) -> unix ms."""
    try:
        dt = datetime.fromisoformat(iso.replace("Z", ""))
    except ValueError:
        raise HTTPException(400, f"bad time: {iso}")
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=get_tz())
    return int(dt.timestamp() * 1000)


def to_iso(ms: int) -> str:
    return local_dt(ms).isoformat(timespec="milliseconds")


@router.get("/stats")
async def stats():
    return get_riv_manager().stats()


@router.get("/{camera_id}/files")
async def files(camera_id: int, date: str, stream: str = "s1"):
    start = datetime.fromisoformat(date)
    async with get_session_factory()() as s:
        rows = (await s.execute(
            select(RivFile).where(RivFile.camera_id == camera_id, RivFile.stream == stream,
                                  RivFile.start_time >= start,
                                  RivFile.start_time < start.replace(hour=23, minute=59, second=59))
            .order_by(RivFile.start_ms))).scalars().all()
    return [{"id": r.id, "path": r.file_path, "start": r.start_time.isoformat(),
             "end": r.end_time.isoformat() if r.end_time else None, "frames": r.frames,
             "keyframes": r.keyframes, "size": r.file_size, "in_progress": r.in_progress}
            for r in rows]


@router.get("/{camera_id}/run")
async def run_info(camera_id: int, start: str, stream: str = "s1"):
    run = await find_run(camera_id, stream, to_ms(start))
    if run is None:
        raise HTTPException(404, "no RIV footage at or after that time")
    return {"start": to_iso(run.start_ms), "end": to_iso(run.end_ms), "live": run.live,
            "files": [s.path for s in run.sources]}


@router.get("/{camera_id}/frame.jpg")
async def frame_jpg(camera_id: int, at: str, stream: str = Query("s2", pattern="^s[12]$"),
                    width: int = Query(480, ge=64, le=1920)):
    """The keyframe at or before ``at`` as a JPEG (timeline hover preview).

    Exact to the GOP, unlike the 15-second trickplay snapshots: the header
    ``X-Riv-Frame`` gives the frame's wall time. Cached by the client per URL.
    """
    import asyncio

    from app.config import get_config
    from app.services.riv.serve import keyframe_at, sources_from
    at_ms = to_ms(at)
    srcs = [s for s in await sources_from(camera_id, stream, at_ms - 60_000)
            if s.start_ms <= at_ms + 1000]
    got = None
    for src in reversed(srcs):
        got = await asyncio.to_thread(keyframe_at, src, at_ms)
        if got:
            break
    if not got:
        raise HTTPException(404, "no RIV frame there")
    wall, data = got
    proc = await asyncio.create_subprocess_exec(
        get_config().ffmpeg.path, "-v", "error", "-f", "mp4", "-i", "pipe:0",
        "-frames:v", "1", "-vf", f"scale={width}:-2", "-q:v", "4",
        "-f", "image2", "-c:v", "mjpeg", "pipe:1",
        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE)
    out, err = await asyncio.wait_for(proc.communicate(data), 15)
    if not out:
        raise HTTPException(500, err.decode("utf-8", "replace")[-300:])
    from fastapi import Response
    return Response(out, media_type="image/jpeg",
                    headers={"X-Riv-Frame": to_iso(wall), "Cache-Control": "max-age=86400"})


@router.get("/{camera_id}/stream.mp4")
async def stream_mp4(camera_id: int, start: str, stream: str = Query("s1", pattern="^s[12]$"),
                     end: str | None = None, speed: int = Query(1, ge=1, le=64)):
    """Continuous fMP4 from ``start`` (see services/riv/serve.py). ``speed`` >= 2
    serves keyframes only, timed so output second p = start + p * speed."""
    run = await find_run(camera_id, stream, to_ms(start))
    if run is None:
        raise HTTPException(404, "no RIV footage at or after that time")
    end_ms = to_ms(end) if end else None
    if speed >= 2:
        # keyframe mode does not follow the live file: give it every file
        from app.services.riv.serve import sources_from
        run.sources = await sources_from(camera_id, stream, run.start_ms)
        body = stream_keyframes(run, speed, end_ms=end_ms)
    else:
        body = stream_fmp4(run, camera_id, stream, end_ms=end_ms)
    return StreamingResponse(
        body,
        media_type="video/mp4",
        headers={"Cache-Control": "no-store", "Accept-Ranges": "none",
                 "X-Riv-Start": to_iso(run.start_ms), "X-Riv-End": to_iso(run.end_ms),
                 "X-Riv-Live": "1" if run.live else "0"},
    )
