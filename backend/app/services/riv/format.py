"""RIV container: records, index trailer, recovery scan.

A RIV file is a sequence of self-describing records::

    "RIVR" type:u8 flags:u8 track:u8 rsv:u8 len:u32 arrival_ms:i64 media_ts:i64
    crc32:u32 payload[len]

``crc32`` covers the first 28 header bytes plus the payload. Record types:

* ``HEAD``  JSON: format version, camera, stream.
* ``INIT``  an fMP4 init segment (``ftyp``+``moov``). Starts a new *epoch*
  (one go2rtc connection; fragment timestamps restart at zero with each).
* ``FRAG``  one ``moof``+``mdat`` exactly as received. ``flags`` bit 0 =
  video keyframe, bit 1 = carries the video track. ``media_ts`` = the video
  (or first) track's baseMediaDecodeTime; ``track`` = that track id.
* ``INDEX`` JSON trailer written when the file is closed cleanly, followed by
  a 16-byte footer ``"RIVX" offset:u64 "END!"`` pointing at it.

A file without a valid footer (crash, power loss) is recovered by
:func:`scan`: records are walked from the start and the scan stops at the
first record that is truncated or fails its CRC, so at most the record being
written at the moment of the crash is lost. ``INIT + FRAG...`` of one epoch,
concatenated, is a valid fMP4 file, which is how RIV is exported.
"""

from __future__ import annotations

import json
import os
import struct
import zlib
from collections import deque
from dataclasses import dataclass, field

MAGIC = b"RIVR"
HEADER = struct.Struct(">4sBBBBIqqI")      # 32 bytes
FOOTER = struct.Struct(">4sQ4s")           # 16 bytes
FOOTER_MAGIC, FOOTER_END = b"RIVX", b"END!"
VERSION = 1

HEAD, INIT, FRAG, INDEX = 1, 2, 3, 4
F_KEY, F_VIDEO = 0x01, 0x02

MAX_RECORD = 64 * 1024 * 1024
# Window of the wall-clock estimator, see WallClock.
WALL_WINDOW_MS = 30_000


def encode_record(rtype: int, payload: bytes, *, flags: int = 0, track: int = 0,
                  arrival_ms: int = 0, media_ts: int = 0) -> bytes:
    head = struct.pack(">4sBBBBIqq", MAGIC, rtype, flags, track, 0,
                       len(payload), arrival_ms, media_ts)
    crc = zlib.crc32(payload, zlib.crc32(head)) & 0xFFFFFFFF
    return head + struct.pack(">I", crc) + payload


@dataclass
class Record:
    offset: int
    rtype: int
    flags: int
    track: int
    arrival_ms: int
    media_ts: int
    payload: bytes

    @property
    def size(self) -> int:
        return HEADER.size + len(self.payload)


def read_record(f, offset: int, limit: int | None = None) -> Record | None:
    """Record at ``offset``, or None if it is incomplete or corrupt."""
    f.seek(offset)
    head = f.read(HEADER.size)
    if len(head) < HEADER.size:
        return None
    magic, rtype, flags, track, _rsv, length, arrival, media, crc = HEADER.unpack(head)
    if magic != MAGIC or length > MAX_RECORD:
        return None
    if limit is not None and offset + HEADER.size + length > limit:
        return None
    payload = f.read(length)
    if len(payload) < length:
        return None
    if zlib.crc32(payload, zlib.crc32(head[:28])) & 0xFFFFFFFF != crc:
        return None
    return Record(offset, rtype, flags, track, arrival, media, payload)


class WallClock:
    """Smooth wall-clock time for media timestamps of one epoch.

    ``arrival - media`` is the camera-to-server offset plus a non-negative
    network/queueing delay, so its minimum over a recent window is the best
    estimate of the true offset: jitter only ever adds. A sliding window
    (rather than the minimum since the epoch began) follows slow clock drift
    between the camera and the server over a connection that can last days.
    Deterministic: the same (arrival, media) sequence gives the same walls,
    so a crash-recovery scan reproduces the writer's index exactly.
    """

    def __init__(self, window_ms: int = WALL_WINDOW_MS) -> None:
        self.window_ms = window_ms
        self._q: deque[tuple[int, int]] = deque()   # (arrival_ms, offset), offsets increasing

    def update(self, arrival_ms: int, media_ms: int) -> int:
        off = arrival_ms - media_ms
        while self._q and self._q[-1][1] >= off:
            self._q.pop()
        self._q.append((arrival_ms, off))
        while self._q[0][0] < arrival_ms - self.window_ms and len(self._q) > 1:
            self._q.popleft()
        return media_ms + self._q[0][1]


@dataclass
class Index:
    """Where things are in one RIV file."""
    stream: str = ""
    camera_id: int = 0
    # (wall_ms, media_ts, offset, epoch) per video keyframe, in file order
    keyframes: list[tuple[int, int, int, int]] = field(default_factory=list)
    # epoch -> (init record offset, video track id, video timescale)
    epochs: dict[int, tuple[int, int, int]] = field(default_factory=dict)
    first_wall_ms: int = 0
    last_wall_ms: int = 0      # wall time of the end of the last video sample
    frames: int = 0
    data_end: int = 0          # offset just past the last complete record

    def to_json(self) -> bytes:
        return json.dumps({
            "v": VERSION, "stream": self.stream, "camera_id": self.camera_id,
            "keyframes": self.keyframes,
            "epochs": {str(k): v for k, v in self.epochs.items()},
            "first_wall_ms": self.first_wall_ms, "last_wall_ms": self.last_wall_ms,
            "frames": self.frames, "data_end": self.data_end,
        }, separators=(",", ":")).encode()

    @classmethod
    def from_json(cls, raw: bytes) -> "Index":
        d = json.loads(raw)
        return cls(
            stream=d.get("stream", ""), camera_id=d.get("camera_id", 0),
            keyframes=[tuple(k) for k in d["keyframes"]],
            epochs={int(k): tuple(v) for k, v in d["epochs"].items()},
            first_wall_ms=d["first_wall_ms"], last_wall_ms=d["last_wall_ms"],
            frames=d["frames"], data_end=d["data_end"],
        )

    def keyframe_at(self, wall_ms: int) -> int:
        """Index into ``keyframes`` of the last keyframe at or before wall_ms
        (the first keyframe when wall_ms is earlier than all of them)."""
        lo, hi = 0, len(self.keyframes)
        while lo < hi:
            mid = (lo + hi) // 2
            if self.keyframes[mid][0] <= wall_ms:
                lo = mid + 1
            else:
                hi = mid
        return max(0, lo - 1)

    def keyframe_after(self, wall_ms: int) -> int | None:
        """Index of the first keyframe strictly after wall_ms, if any."""
        i = self.keyframe_at(wall_ms)
        if self.keyframes and self.keyframes[i][0] > wall_ms:
            return i
        return i + 1 if i + 1 < len(self.keyframes) else None


class IndexBuilder:
    """Builds an Index from records in file order. Used by the writer as it
    writes and by :func:`scan` on recovery, so both produce the same index."""

    def __init__(self) -> None:
        self.index = Index()
        self._epoch = -1
        self._clock: WallClock | None = None
        self._video_track = 0
        self._video_ts = 90000

    def add(self, rec: Record) -> None:
        if rec.rtype == HEAD:
            try:
                h = json.loads(rec.payload)
                self.index.stream = h.get("stream", "")
                self.index.camera_id = h.get("camera_id", 0)
            except ValueError:
                pass
        elif rec.rtype == INIT:
            from .fmp4 import parse_init
            tracks = parse_init(rec.payload)
            video = next((t for t in tracks.values() if t.handler == "vide"), None)
            self._video_track = video.track_id if video else 0
            self._video_ts = video.timescale if video and video.timescale else 90000
            self._epoch += 1
            self._clock = WallClock()
            self.index.epochs[self._epoch] = (rec.offset, self._video_track, self._video_ts)
        elif rec.rtype == FRAG and self._clock is not None and rec.flags & F_VIDEO:
            media_ms = rec.media_ts * 1000 // self._video_ts
            wall = self._clock.update(rec.arrival_ms, media_ms)
            if rec.flags & F_KEY:
                self.index.keyframes.append((wall, rec.media_ts, rec.offset, self._epoch))
                if not self.index.first_wall_ms:
                    self.index.first_wall_ms = wall
            self.index.frames += 1
            # Sample end: start + duration is not in the record; the next
            # frame's start replaces this, so a closed file is at most one
            # frame short of its true end.
            self.index.last_wall_ms = max(self.index.last_wall_ms, wall)
        self.index.data_end = rec.offset + rec.size

    def estimate(self, arrival_ms: int, media_ts: int) -> int | None:
        """Wall time the next video FRAG with these values will be given.

        WallClock.update is idempotent for a repeated sample, so calling this
        before :meth:`add` for the same frame does not change the result."""
        if self._clock is None:
            return None
        return self._clock.update(arrival_ms, media_ts * 1000 // self._video_ts)

    @property
    def epoch(self) -> int:
        return self._epoch


def read_footer_index(f, size: int) -> Index | None:
    if size < FOOTER.size:
        return None
    f.seek(size - FOOTER.size)
    magic, off, end = FOOTER.unpack(f.read(FOOTER.size))
    if magic != FOOTER_MAGIC or end != FOOTER_END or off >= size:
        return None
    rec = read_record(f, off)
    if rec is None or rec.rtype != INDEX:
        return None
    try:
        return Index.from_json(rec.payload)
    except (ValueError, KeyError):
        return None


def scan(f, size: int | None = None) -> Index:
    """Rebuild the index by walking every record (crash recovery)."""
    if size is None:
        f.seek(0, os.SEEK_END)
        size = f.tell()
    b = IndexBuilder()
    off = 0
    while off < size:
        rec = read_record(f, off, size)
        if rec is None or rec.rtype == INDEX:
            break
        b.add(rec)
        off += rec.size
    return b.index


def load_index(path: str) -> tuple[Index, bool]:
    """(index, was_clean) for a file on disk."""
    with open(path, "rb") as f:
        f.seek(0, os.SEEK_END)
        size = f.tell()
        idx = read_footer_index(f, size)
        if idx is not None:
            return idx, True
        return scan(f, size), False


def finish(f, index: Index) -> None:
    """Append the INDEX record and footer to an open file positioned at its end."""
    off = f.tell()
    f.write(encode_record(INDEX, index.to_json()))
    f.write(FOOTER.pack(FOOTER_MAGIC, off, FOOTER_END))


def recover_file(path: str) -> tuple[Index, int]:
    """Make a file left open by a crash readable: rebuild its index from the
    records, cut the torn tail and append the trailer. (index, final size)."""
    idx, clean = load_index(path)
    if not clean:
        truncate_to(path, idx.data_end)
        with open(path, "r+b") as f:
            f.seek(0, os.SEEK_END)
            finish(f, idx)
    return idx, os.path.getsize(path)


def truncate_to(path: str, data_end: int) -> None:
    """Cut a torn tail off a recovered file so new appends stay readable."""
    if os.path.getsize(path) > data_end:
        with open(path, "r+b") as f:
            f.truncate(data_end)
