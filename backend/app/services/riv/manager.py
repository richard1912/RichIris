"""Glue between the live cache, the RIV writers and the database.

One RivWriter per (camera, stream) for every enabled camera whose
``rec_format`` is ``shadow`` or ``riv``. The live cache hands every fragment
it receives to :meth:`RivManager.sink`; the writer thread does the rest.

RIV files are written straight into the ARCHIVE tree (``tier='archive'``).
The hot/archive split exists to move ffmpeg's output off a fast disk; on the
box both tiers are the same F: filesystem, and a RIV file is written once and
never copied.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from datetime import datetime

from sqlalchemy import select

from app.config import get_config, get_tz
from app.database import get_session_factory
from app.models import Camera, RivFile

from . import format as rf
from .writer import OpenFile, RivWriter

logger = logging.getLogger(__name__)

STREAMS = ("s1", "s2")
SYNC_SECONDS = 30


def local_dt(ms: int) -> datetime:
    return datetime.fromtimestamp(ms / 1000, get_tz()).replace(tzinfo=None)


def riv_root() -> tuple[str, str]:
    cfg = get_config().storage
    if cfg.two_tier_enabled and cfg.archive_recordings_dir \
            and cfg.archive_recordings_dir != cfg.recordings_dir:
        return cfg.archive_recordings_dir, "archive"
    return cfg.recordings_dir, "hot"


class RivManager:
    def __init__(self) -> None:
        self.writers: dict[str, RivWriter] = {}         # go2rtc stream name -> writer
        self.by_camera: dict[tuple[int, str], RivWriter] = {}
        self.formats: dict[int, str] = {}
        self._loop: asyncio.AbstractEventLoop | None = None
        self._db_lock = asyncio.Lock()
        self._task: asyncio.Task | None = None

    # -- lifecycle -------------------------------------------------------

    async def start(self) -> None:
        self._loop = asyncio.get_running_loop()
        try:
            await self.recover()
        except Exception:
            logger.exception("RIV recovery failed")
        await self.sync()
        from app.services.live_cache import get_live_cache
        get_live_cache().set_sink(self.sink)
        self._task = asyncio.create_task(self._sync_loop())

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
        from app.services.live_cache import get_live_cache
        get_live_cache().set_sink(None)
        writers = list(self.writers.values())
        self.writers.clear()
        self.by_camera.clear()
        await asyncio.gather(*(asyncio.to_thread(w.stop) for w in writers))

    async def _sync_loop(self) -> None:
        while True:
            await asyncio.sleep(SYNC_SECONDS)
            try:
                await self.sync()
            except Exception:
                logger.exception("RIV sync failed")

    async def sync(self) -> None:
        """Start/stop writers to match each camera's rec_format."""
        from app.services.ffmpeg import sanitize_camera_name
        from app.services.go2rtc_client import get_stream_name
        factory = get_session_factory()
        async with factory() as s:
            cams = (await s.execute(select(Camera))).scalars().all()
        want: dict[str, tuple[Camera, str]] = {}
        self.formats = {}
        for c in cams:
            fmt = (c.rec_format or "ts") if c.enabled else "ts"
            self.formats[c.id] = fmt
            if fmt in ("shadow", "riv"):
                for st in STREAMS:
                    want[f"{get_stream_name(c.name)}_{st}_direct"] = (c, st)
        for name in list(self.writers):
            w = self.writers[name]
            cam = want.get(name)
            if cam is None or cam[0].name != w.camera_name:
                self.writers.pop(name)
                self.by_camera.pop((w.camera_id, w.stream), None)
                await asyncio.to_thread(w.stop)
                logger.info("RIV writer stopped", extra={"camera_name": w.camera_name,
                                                         "stream": w.stream})
        root, _tier = riv_root()
        for name, (c, st) in want.items():
            if name in self.writers:
                continue
            w = RivWriter(camera_id=c.id, camera_name=c.name,
                          safe_name=sanitize_camera_name(c.name), stream=st, root=root,
                          tz=get_tz(), on_open=self._on_open, on_close=self._on_close)
            w.start()
            self.writers[name] = w
            self.by_camera[(c.id, st)] = w
            logger.info("RIV writer started", extra={"camera_name": c.name, "stream": st,
                                                     "root": root})

    # -- live cache sink (event loop) -----------------------------------

    def sink(self, stream_name: str, epoch_key: object, init: bytes, fragment: bytes,
             is_key: bool, has_video: bool) -> None:
        w = self.writers.get(stream_name)
        if w is not None:
            w.submit(epoch_key, init, fragment, is_key, has_video)

    # -- queries ---------------------------------------------------------

    def video_idle_seconds(self, camera_id: int, stream: str = "s1") -> float | None:
        """Seconds since a video frame was written (None: no writer / never)."""
        w = self.by_camera.get((camera_id, stream))
        if w is None or not w.last_video_monotonic:
            return None
        return time.monotonic() - w.last_video_monotonic

    def current_file(self, camera_id: int, stream: str) -> OpenFile | None:
        w = self.by_camera.get((camera_id, stream))
        return w.current if w else None

    def stats(self) -> dict:
        now = time.monotonic()
        return {
            f"{w.camera_name} {w.stream}": {
                "file": os.path.basename(w.current.path) if w.current else None,
                "frames": w.current.index.frames if w.current else 0,
                "written": w.written_frags,
                "dropped": w.dropped,
                "errors": w.errors,
                "queue": w._q.qsize(),
                "idle_s": round(now - w.last_write_monotonic, 1) if w.last_write_monotonic else None,
            }
            for w in self.writers.values()
        }

    # -- DB bookkeeping (called on writer threads) -----------------------

    def _on_open(self, path: str, start_ms: int) -> None:
        w = next((x for x in self.writers.values()
                  if x.current is not None and x.current.path == path), None)
        if w is None or self._loop is None:
            return
        asyncio.run_coroutine_threadsafe(
            self._db_open(w.camera_id, w.stream, path, start_ms), self._loop)

    def _on_close(self, path: str, index: rf.Index, size: int) -> None:
        if self._loop is None:
            return
        asyncio.run_coroutine_threadsafe(self._db_close(path, index, size), self._loop)

    async def _db_open(self, camera_id: int, stream: str, path: str, start_ms: int) -> None:
        _root, tier = riv_root()
        async with self._db_lock:
            async with get_session_factory()() as s:
                s.add(RivFile(camera_id=camera_id, stream=stream, file_path=path,
                              start_ms=start_ms, start_time=local_dt(start_ms),
                              in_progress=True, tier=tier))
                await s.commit()

    async def _db_close(self, path: str, index: rf.Index, size: int) -> None:
        async with self._db_lock:
            async with get_session_factory()() as s:
                row = (await s.execute(select(RivFile).where(RivFile.file_path == path))
                       ).scalars().first()
                if row is None:
                    return
                _apply_index(row, index, size)
                await s.commit()

    async def recover(self) -> None:
        """Close files left open by a crash: rebuild their index from the
        records, cut the torn tail, append the trailer, finalise the row."""
        async with get_session_factory()() as s:
            rows = (await s.execute(select(RivFile).where(RivFile.in_progress == True))  # noqa: E712
                    ).scalars().all()
            for row in rows:
                if not os.path.exists(row.file_path):
                    await s.delete(row)
                    continue
                idx, size = await asyncio.to_thread(_recover_file, row.file_path)
                _apply_index(row, idx, size)
                logger.info("RIV file recovered", extra={"path": row.file_path,
                                                         "frames": idx.frames})
            await s.commit()


def _recover_file(path: str) -> tuple[rf.Index, int]:
    return rf.recover_file(path)


def _apply_index(row: RivFile, index: rf.Index, size: int) -> None:
    row.in_progress = False
    row.file_size = size
    row.frames = index.frames
    row.keyframes = len(index.keyframes)
    if index.first_wall_ms:
        row.start_ms = index.first_wall_ms
        row.start_time = local_dt(index.first_wall_ms)
    if index.last_wall_ms:
        row.end_ms = index.last_wall_ms
        row.end_time = local_dt(index.last_wall_ms)
        row.duration = max(0.0, (index.last_wall_ms - row.start_ms) / 1000)


_manager: RivManager | None = None


def get_riv_manager() -> RivManager:
    global _manager
    if _manager is None:
        _manager = RivManager()
    return _manager
