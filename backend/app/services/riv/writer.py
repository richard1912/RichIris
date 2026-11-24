"""Write one camera stream (s1 or s2) to RIV files.

Fed from the live cache's sink on the event loop; everything that touches the
disk or parses boxes runs on this writer's own thread, so a slow disk can
never stall live view. The queue is bounded: if the thread falls more than
``MAX_QUEUE`` fragments behind (minutes of a stalled disk) fragments are
dropped and counted rather than growing memory without limit, and the next
keyframe starts cleanly again.
"""

from __future__ import annotations

import logging
import os
import queue
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Callable
from zoneinfo import ZoneInfo

from . import format as rf
from .fmp4 import parse_fragment, parse_init

logger = logging.getLogger(__name__)

ROLL_SECONDS = 900
MAX_QUEUE = 4000
FLUSH_SECONDS = 1.0


@dataclass
class _Item:
    epoch_key: object
    init: bytes
    fragment: bytes
    is_key: bool
    has_video: bool
    arrival_ms: int


class OpenFile:
    """State of the file currently being written (shared with readers)."""

    def __init__(self, path: str, builder: rf.IndexBuilder) -> None:
        self.path = path
        self.builder = builder
        self.window_end_ms = 0

    @property
    def index(self) -> rf.Index:
        return self.builder.index


class RivWriter:
    def __init__(self, *, camera_id: int, camera_name: str, safe_name: str,
                 stream: str, root: str, tz: ZoneInfo,
                 on_open: Callable[[str, int], None] | None = None,
                 on_close: Callable[[str, rf.Index, int], None] | None = None,
                 roll_seconds: int = ROLL_SECONDS) -> None:
        self.camera_id = camera_id
        self.camera_name = camera_name
        self.safe_name = safe_name
        self.stream = stream
        self.root = root
        self.tz = tz
        self.on_open = on_open
        self.on_close = on_close
        self.roll_ms = roll_seconds * 1000
        self._q: queue.Queue[_Item | None] = queue.Queue(MAX_QUEUE)
        self._thread = threading.Thread(target=self._run, daemon=True,
                                        name=f"riv-{safe_name}-{stream}")
        self.current: OpenFile | None = None
        self._f = None
        self._epoch_key: object = None
        self._init: bytes | None = None
        self._need_key = True
        self._last_flush = 0.0
        self._tracks_for: bytes | None = None
        self._tracks: dict = {}
        # stats
        self.dropped = 0
        self.written_frags = 0
        self.last_write_monotonic = 0.0
        self.last_video_monotonic = 0.0
        self.errors = 0

    # -- event-loop side -------------------------------------------------

    def start(self) -> None:
        self._thread.start()

    def submit(self, epoch_key: object, init: bytes, fragment: bytes,
               is_key: bool, has_video: bool, arrival_ms: int | None = None) -> None:
        try:
            self._q.put_nowait(_Item(epoch_key, init, fragment, is_key, has_video,
                                     arrival_ms if arrival_ms is not None
                                     else int(time.time() * 1000)))
        except queue.Full:
            self.dropped += 1
            self._need_key = True   # read on the thread; a benign race

    def stop(self, timeout: float = 10.0) -> None:
        try:
            self._q.put(None, timeout=timeout)
        except queue.Full:
            pass
        self._thread.join(timeout)

    # -- writer thread ---------------------------------------------------

    def _run(self) -> None:
        while True:
            item = self._q.get()
            if item is None:
                break
            try:
                self._handle(item)
            except Exception:
                self.errors += 1
                logger.exception("RIV write failed - closing file",
                                 extra={"camera_name": self.camera_name, "stream": self.stream})
                self._close_file()
                self._need_key = True
            if self._f is not None and (self._q.empty()
                                        or time.monotonic() - self._last_flush > FLUSH_SECONDS):
                self._f.flush()
                self._last_flush = time.monotonic()
        self._close_file()

    def _handle(self, item: _Item) -> None:
        if not item.init:
            return
        new_epoch = item.epoch_key is not self._epoch_key
        if new_epoch:
            self._need_key = True
        if self._need_key and not (item.is_key and item.has_video):
            return
        info = parse_fragment(item.fragment)
        if info is None or not info.trafs:
            return
        if item.init is not self._tracks_for:
            self._tracks_for = item.init
            self._tracks = parse_init(item.init)
        video = next((t for t in self._tracks.values() if t.handler == "vide"), None)
        vt = next((t for t in info.trafs if video and t.track_id == video.track_id),
                  info.trafs[0])
        if new_epoch:
            self._epoch_key = item.epoch_key
            self._init = item.init
            if self.current is not None:
                self._write(rf.INIT, item.init)
        elif item.init != self._init:
            # Same connection, repaired init (hvcC rebuilt on the first
            # keyframe): treat as a new epoch of the same timeline.
            self._init = item.init
            if self.current is not None:
                self._write(rf.INIT, item.init)
        self._need_key = False

        flags = (rf.F_KEY if item.is_key else 0) | (rf.F_VIDEO if item.has_video else 0)
        if item.is_key and item.has_video:
            wall = self.current.builder.estimate(item.arrival_ms, vt.base_time) \
                if self.current is not None else None
            if self.current is None or (wall is not None and wall >= self.current.window_end_ms):
                self._roll(item, vt.base_time)
        if self.current is None:
            return
        self._write(rf.FRAG, item.fragment, flags=flags, track=vt.track_id,
                    arrival_ms=item.arrival_ms, media_ts=vt.base_time)
        self.written_frags += 1
        self.last_write_monotonic = time.monotonic()
        if item.has_video:
            self.last_video_monotonic = self.last_write_monotonic

    def _write(self, rtype: int, payload: bytes, **kw) -> None:
        f = self._f
        off = f.tell()
        data = rf.encode_record(rtype, payload, **kw)
        f.write(data)
        h = rf.HEADER.unpack(data[:rf.HEADER.size])
        self.current.builder.add(rf.Record(off, rtype, h[2], h[3], h[6], h[7], payload))

    def _roll(self, item: _Item, media_ts: int) -> None:
        self._close_file()
        # Name and window come from the arrival time of the first keyframe;
        # the index's smoothed wall time differs by network delay only.
        start_ms = item.arrival_ms
        local = datetime.fromtimestamp(start_ms / 1000, self.tz)
        day = local.strftime("%Y-%m-%d")
        d = os.path.join(self.root, self.safe_name, day)
        os.makedirs(d, exist_ok=True)
        name = f"{self.camera_name} {day} {local.strftime('%H.%M.%S')} {self.stream}.riv"
        path = os.path.join(d, name)
        n = 1
        while os.path.exists(path):
            path = os.path.join(d, name[:-4] + f" ({n}).riv")
            n += 1
        self._f = open(path, "wb")
        self.current = OpenFile(path, rf.IndexBuilder())
        self.current.window_end_ms = (start_ms // self.roll_ms + 1) * self.roll_ms
        import json
        self._write(rf.HEAD, json.dumps({
            "v": rf.VERSION, "camera_id": self.camera_id, "camera": self.camera_name,
            "stream": self.stream, "created_ms": start_ms}).encode())
        self._write(rf.INIT, item.init)
        self._f.flush()
        if self.on_open:
            try:
                self.on_open(path, start_ms)
            except Exception:
                logger.exception("RIV on_open callback failed")

    def _close_file(self) -> None:
        if self._f is None:
            return
        cur, f = self.current, self._f
        self._f = None
        try:
            f.seek(0, os.SEEK_END)
            rf.finish(f, cur.index)
            f.flush()
            os.fsync(f.fileno())
        except OSError:
            logger.exception("RIV close failed", extra={"path": cur.path})
        finally:
            f.close()
        size = os.path.getsize(cur.path)
        self.current = None
        if self.on_close:
            try:
                self.on_close(cur.path, cur.index, size)
            except Exception:
                logger.exception("RIV on_close callback failed")
