"""Where a camera's footage for a time window is, as ffmpeg input args.

One answer for every reader that decodes recordings: RIV first (the camera
records RIV, or records both and RIV covers the window), else the MPEG-TS
segment with an input-side seek. ``offset`` is the seconds between the first
frame the input yields and ``start`` (RIV begins on the keyframe at or before
``start``; the .ts path seeks, so its offset is 0).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path


@dataclass
class FootageInput:
    args: list[str]          # ffmpeg input args, ending with "-i <src>"
    offset: float            # seconds from the first frame to `start`
    source: str              # path or URL, for logging
    riv: bool


async def footage_input(camera_id: int, start: datetime, end: datetime) -> FootageInput | None:
    from sqlalchemy import select

    from app.database import get_session_factory
    from app.models import Camera, Recording
    from app.services.ffmpeg import TS_INPUT_ARGS

    async with get_session_factory()() as s:
        cam = await s.get(Camera, camera_id)
        fmt = (cam.rec_format if cam else "ts") or "ts"
        if fmt != "ts":
            from app.config import get_bootstrap
            from app.routers.riv import to_iso, to_ms
            from app.services.riv.serve import find_run
            start_ms, end_ms = to_ms(start.isoformat()), to_ms(end.isoformat())
            run = await find_run(camera_id, "s1", start_ms)
            if run is not None and run.start_ms < end_ms and (
                    fmt == "riv" or run.start_ms <= start_ms + 5_000):
                url = (f"http://127.0.0.1:{get_bootstrap().port}/api/riv/{camera_id}/stream.mp4"
                       f"?start={to_iso(run.start_ms)}&end={to_iso(end_ms)}&stream=s1")
                return FootageInput(["-i", url], max(0.0, (start_ms - run.start_ms) / 1000),
                                    url, True)
            if fmt == "riv":
                return None
        rec = (await s.execute(
            select(Recording).where(Recording.camera_id == camera_id,
                                    Recording.start_time <= start)
            .order_by(Recording.start_time.desc()).limit(1))).scalar_one_or_none()
    if rec is None or not Path(rec.file_path).exists():
        return None
    if rec.end_time is not None and rec.end_time < end:
        return None
    off = max((start - rec.start_time).total_seconds(), 0.0)
    return FootageInput([*TS_INPUT_ARGS, "-ss", f"{off:.2f}", "-i", rec.file_path], 0.0,
                        rec.file_path, False)
