"""GOP cache and fanout for go2rtc's fMP4 live output.

A client joining a live stream cannot decode anything until a keyframe
arrives, and on these cameras that is most of a GOP away. Measured
2026-09-20 against already-connected producers (so none of this is the
camera reconnecting):

    front_south_47 s1   GOP 2.14s   first decodable frame 2,536 ms
    front_north_42 s1   GOP 2.14s                          2,496 ms
    front_door     s1   GOP 2.02s                          1,768 ms
    front_door     s2   GOP 4.01s                          3,896 ms

Six repeats on one stream came in at 2,427-2,615 ms, so it is deterministic
rather than a lucky/unlucky join point. It is not contention either: eight
simultaneous opens all finished within 2,724 ms. No demuxer option helps -
``analyzeduration=100ms`` made it *worse* (4,127 ms), and the plain fMP4
proxy is worse again (4,606 ms) because go2rtc's MP4 consumer waits for a
keyframe exactly like an RTSP one does.

So keep the keyframe on the server. StreamManager's keepalive already holds a
persistent fMP4 connection to go2rtc for every camera and discards every byte
(it exists only to stop the camera session going idle). This module parses
that same feed into its init segment plus the fragments since the most recent
keyframe, and writes that to a joining client before attaching it to the live
feed. Time to first frame becomes the time to push ~1 MB down a socket, and
the client then runs between zero and one GOP behind - about 1.1 s on average
here - instead of showing nothing for that long.

Two things fall out of it for free:

* **N viewers cost ONE go2rtc consumer.** Every subscriber is served from the
  shared keepalive, so the tenth client on a camera adds no upstream load.
* **Live view stops needing a direct route to the RTSP port.** Clients on the
  cached path speak HTTP to the backend, which Caddy can proxy, unlike RTSP.

Buffer cost is small: the main streams run ~2.8 Mbit/s, so one GOP is ~1 MB
per camera and under 10 MB for the whole house.

Structure of go2rtc's ``/api/stream.mp4`` output, confirmed by parsing real
captures (2026-09-20): ``ftyp`` + ``moov``, then one ``moof``+``mdat`` pair
per sample. Keyframes are flagged in ``tfhd.default_sample_flags``:
0x02000000 on a sync sample against 0x01010000 otherwise, i.e. bit
0x00010000 is ``sample_is_non_sync_sample``. On Front South 47 the
flag-detected keyframes landed on fragments 0, 15, 30 and 45, exactly the
measured 2.143 s GOP at 7 fps.

**Track count varies by camera and it matters.** Front South 47 is video
only (709-byte moov, one trak), but Front Door is video + audio (1090-byte
moov, ``vide`` and ``soun``). Audio samples are ALL sync samples, so
treating any sync fragment as a GOP boundary makes every 222-byte audio
fragment reset the buffer: Front Door cached exactly one 346-byte audio
fragment and served it as the backlog, which is why it measured 1,655 ms
against ~116 ms on the video-only cameras. The video track id is read out
of the ``moov`` and only that track's sync samples start a new GOP;
everything else is carried along after it.
"""

import asyncio
import logging
import struct
import time
from collections import deque

logger = logging.getLogger(__name__)

# Per-subscriber queue depth, in fragments. One fragment is one frame, so at
# 7-15 fps this is tens of seconds of slack before a stalled client is cut
# loose. A client that falls this far behind is not coming back.
SUBSCRIBER_QUEUE_FRAGMENTS = 512

# How many video keyframes the buffer holds. TWO are kept, but a client chooses
# how much of that it is handed (``attach(short=...)``):
#
# * **short** - from the NEWEST keyframe, so the client runs 0-1 GOP behind
#   live. Only for clients that set `vd-lavc-o=flags=+low_delay` (the native
#   app). Without that flag libavcodec will not emit a frame until it has
#   roughly a full GOP in hand, so a partial-GOP backlog leaves it waiting out
#   the rest in real time: measured 2026-09-20 on a burst-then-realtime server,
#   2,379 ms at a 1-fragment backlog, 1,595 ms at 8, 589 ms at 15 (one GOP),
#   455 ms at 20 - against ~390 ms at ANY depth with the flag.
# * **full** (default) - from the OLDER keyframe, always one complete GOP plus
#   part of the next, so a client that cannot set the flag (browsers, ffmpeg)
#   never lands in the slow part of that curve. Costs one more GOP of latency.
#   Confirmed 2026-09-22 with the buffer briefly at one keyframe: plain ffmpeg
#   took up to 2,604 ms to first frame, as the curve predicts.
#
# low_delay was wrongly blamed for Android corruption on 2026-09-20; that was
# go2rtc's canned hvcC (see _repair_hevc_init below). Re-tested on the Fold7
# with the hvcC repaired: clean on all 8 tiles over repeated cold launches.
KEEP_KEYFRAMES = 2

# Hard caps on the cache itself, in case a stream arrives with a pathological
# GOP (or no keyframes at all, which would otherwise never trim).
MAX_BUFFER_FRAGMENTS = 900
MAX_BUFFER_BYTES = 24 * 1024 * 1024

# sample_flags bit 16 (ISO 14496-12 8.8.3.1): set means NOT a sync sample.
_NON_SYNC = 0x00010000

# tf_flags bits in tfhd
_TFHD_BASE_DATA_OFFSET = 0x000001
_TFHD_SAMPLE_DESC_INDEX = 0x000002
_TFHD_DEFAULT_SAMPLE_DURATION = 0x000008
_TFHD_DEFAULT_SAMPLE_SIZE = 0x000010
_TFHD_DEFAULT_SAMPLE_FLAGS = 0x000020

# tr_flags bits in trun
_TRUN_DATA_OFFSET = 0x000001
_TRUN_FIRST_SAMPLE_FLAGS = 0x000004


def _iter_boxes(buf: bytes, start: int, end: int):
    """Yield (type, payload_start, box_end) for each box in [start, end)."""
    i = start
    while i + 8 <= end:
        size = struct.unpack_from(">I", buf, i)[0]
        btype = buf[i + 4:i + 8]
        header = 8
        if size == 1:
            if i + 16 > end:
                return
            size = struct.unpack_from(">Q", buf, i + 8)[0]
            header = 16
        if size < header or i + size > end:
            return
        yield btype, i + header, i + size
        i += size


def _parse_video_track_id(moov: bytes) -> int | None:
    """The ``track_ID`` of the ``vide`` track, or None if it cannot be read.

    None means "treat every track as video", which is the correct fallback
    for the single-track streams most of these cameras produce.
    """
    try:
        for btype, s, e in _iter_boxes(moov, 8, len(moov)):
            if btype != b"trak":
                continue
            track_id: int | None = None
            is_video = False
            for b2, s2, e2 in _iter_boxes(moov, s, e):
                if b2 == b"tkhd":
                    version = moov[s2]
                    off = s2 + 4 + (16 if version == 1 else 8)
                    if off + 4 <= e2:
                        track_id = struct.unpack_from(">I", moov, off)[0]
                elif b2 == b"mdia":
                    for b3, s3, e3 in _iter_boxes(moov, s2, e2):
                        if b3 == b"hdlr" and moov[s3 + 8:s3 + 12] == b"vide":
                            is_video = True
            if is_video and track_id is not None:
                return track_id
    except Exception:  # pragma: no cover - malformed box
        pass
    return None


def _moof_tracks(moof: bytes) -> list[tuple[int | None, bool]]:
    """Per-traf ``(track_id, is_sync_sample)`` for one fragment.

    Reads ``trun.first_sample_flags`` when present and falls back to
    ``tfhd.default_sample_flags``, which is what go2rtc actually writes.
    An unreadable traf is reported as a sync sample: starting a client on a
    non-keyframe costs a moment of green macroblocks, whereas never
    recognising one would leave the buffer growing until the cap trims it.
    """
    out: list[tuple[int | None, bool]] = []
    try:
        for btype, s, e in _iter_boxes(moof, 8, len(moof)):
            if btype != b"traf":
                continue
            track_id: int | None = None
            default_flags: int | None = None
            first_flags: int | None = None
            for btype2, s2, e2 in _iter_boxes(moof, s, e):
                if btype2 == b"tfhd":
                    flags = struct.unpack_from(">I", moof, s2)[0] & 0xFFFFFF
                    track_id = struct.unpack_from(">I", moof, s2 + 4)[0]
                    p = s2 + 8  # version/flags + track_ID
                    if flags & _TFHD_BASE_DATA_OFFSET:
                        p += 8
                    if flags & _TFHD_SAMPLE_DESC_INDEX:
                        p += 4
                    if flags & _TFHD_DEFAULT_SAMPLE_DURATION:
                        p += 4
                    if flags & _TFHD_DEFAULT_SAMPLE_SIZE:
                        p += 4
                    if flags & _TFHD_DEFAULT_SAMPLE_FLAGS and p + 4 <= e2:
                        default_flags = struct.unpack_from(">I", moof, p)[0]
                elif btype2 == b"trun":
                    flags = struct.unpack_from(">I", moof, s2)[0] & 0xFFFFFF
                    p = s2 + 8  # version/flags + sample_count
                    if flags & _TRUN_DATA_OFFSET:
                        p += 4
                    if flags & _TRUN_FIRST_SAMPLE_FLAGS and p + 4 <= e2:
                        first_flags = struct.unpack_from(">I", moof, p)[0]
            effective = first_flags if first_flags is not None else default_flags
            out.append((track_id, True if effective is None
                        else (effective & _NON_SYNC) == 0))
    except Exception:  # pragma: no cover - malformed box
        pass
    return out or [(None, True)]


# -- HEVC init-segment repair ---------------------------------------------
#
# go2rtc builds the fMP4 ``hvcC`` from the RTSP SDP. These cameras send no
# ``sprop-vps/sps/pps`` there, so go2rtc falls back to a canned parameter set
# describing 2560x1440 at level 5.1 - while the cameras actually send
# 3840x2160, with the real VPS/SPS/PPS in-band on every keyframe (found
# 2026-09-22 by parsing captures: the hvcC SPS was byte-identical across two
# different cameras and 33 bytes against the in-band 44).
#
# Software decoders and browsers shrug: they adopt the in-band SPS. Android's
# MediaCodec is configured from the hvcC (csd-0) and is not so forgiving - it
# rendered a band of colour noise over black on the Fold. That, not
# ``low_delay`` and not anything about the cache, is why native was reverted
# off this endpoint on 2026-09-20. It also explains why the app reported
# "2560x1440" for cameras that are not.
#
# The cache sees every keyframe, so it rewrites the init segment with the
# parameter sets the stream really uses before any client is served.

_NAL_VPS, _NAL_SPS, _NAL_PPS = 32, 33, 34


def _rbsp(nal: bytes) -> bytes:
    """Strip emulation-prevention bytes (00 00 03 -> 00 00)."""
    out = bytearray()
    zeros = 0
    for c in nal:
        if zeros >= 2 and c == 3:
            zeros = 0
            continue
        out.append(c)
        zeros = zeros + 1 if c == 0 else 0
    return bytes(out)


class _Bits:
    def __init__(self, data: bytes) -> None:
        self.d = data
        self.p = 0

    def u(self, n: int) -> int:
        v = 0
        for _ in range(n):
            v = (v << 1) | ((self.d[self.p >> 3] >> (7 - (self.p & 7))) & 1)
            self.p += 1
        return v

    def ue(self) -> int:
        z = 0
        while self.u(1) == 0:
            z += 1
        return (1 << z) - 1 + self.u(z)


def _hevc_sps_dims(sps: bytes) -> tuple[int, int]:
    """Cropped (width, height) from an HEVC SPS NAL (2-byte header included)."""
    r = _Bits(_rbsp(sps[2:]))
    r.u(4)
    max_sub = r.u(3)
    r.u(1)
    r.u(96)  # general profile_tier_level
    present = [(r.u(1), r.u(1)) for _ in range(max_sub)]
    if max_sub:
        r.u(2 * (8 - max_sub))
    for prof, lvl in present:
        if prof:
            r.u(88)
        if lvl:
            r.u(8)
    r.ue()
    chroma = r.ue()
    if chroma == 3:
        r.u(1)
    w, h = r.ue(), r.ue()
    if r.u(1):
        sub_w = 2 if chroma in (1, 2) else 1
        sub_h = 2 if chroma == 1 else 1
        left, right, top, bottom = r.ue(), r.ue(), r.ue(), r.ue()
        w -= sub_w * (left + right)
        h -= sub_h * (top + bottom)
    return w, h


def _keyframe_param_sets(fragment: bytes) -> dict[int, bytes]:
    """VPS/SPS/PPS carried in-band in a moof+mdat fragment, keyed by NAL type."""
    found: dict[int, bytes] = {}
    for btype, s, e in _iter_boxes(fragment, 0, len(fragment)):
        if btype != b"mdat":
            continue
        p = s
        while p + 4 <= e:
            n = struct.unpack_from(">I", fragment, p)[0]
            if n < 2 or p + 4 + n > e:
                break
            ntype = (fragment[p + 4] >> 1) & 0x3F
            if ntype in (_NAL_VPS, _NAL_SPS, _NAL_PPS):
                found.setdefault(ntype, fragment[p + 4:p + 4 + n])
            elif ntype < 32:
                break  # reached slice data
            p += 4 + n
    return found


def _find_child(buf: bytes, start: int, end: int, want: bytes):
    for btype, s, e in _iter_boxes(buf, start, end):
        if btype == want:
            return s, e
    return None


def _repair_hevc_init(init: bytes, ps: dict[int, bytes]) -> bytes | None:
    """``init`` with its hvcC + dimensions rebuilt from ``ps``; None = leave it.

    Returns None when the init is not HEVC, the parameter sets are incomplete,
    or the hvcC already matches - every failure mode falls back to serving
    go2rtc's init untouched, which is what happened before this existed.
    """
    if not all(t in ps for t in (_NAL_VPS, _NAL_SPS, _NAL_PPS)):
        return None
    try:
        # offset of the size field of every box that contains the hvcC
        ancestors: list[int] = []
        lo, hi = 0, len(init)
        tkhd = None
        for name in (b"moov", b"trak", b"mdia", b"minf", b"stbl", b"stsd"):
            if name == b"trak":
                hit = None
                for btype, s, e in _iter_boxes(init, lo, hi):
                    if btype == b"trak" and init.find(b"hvcC", s, e) > 0:
                        hit = (s, e)
                        tkhd = _find_child(init, s, e, b"tkhd")
                        break
            else:
                hit = _find_child(init, lo, hi, name)
            if hit is None:
                return None
            ancestors.append(hit[0] - 8)
            lo, hi = hit
        lo += 8  # stsd: version/flags + entry_count
        entry = None
        for btype, s, e in _iter_boxes(init, lo, hi):
            if btype in (b"hvc1", b"hev1"):
                entry = (s, e)
                break
        if entry is None:
            return None
        ancestors.append(entry[0] - 8)
        hvcc = _find_child(init, entry[0] + 78, entry[1], b"hvcC")
        if hvcc is None:
            return None
        old = init[hvcc[0]:hvcc[1]]
        body = bytearray(old[:23])
        body[1:13] = _rbsp(ps[_NAL_SPS][2:])[1:13]  # general profile/tier/level
        body[22] = 3
        for ntype in (_NAL_VPS, _NAL_SPS, _NAL_PPS):
            nal = ps[ntype]
            body += bytes([0x80 | ntype]) + struct.pack(">HH", 1, len(nal)) + nal
        if bytes(body) == old:
            return None
        width, height = _hevc_sps_dims(ps[_NAL_SPS])
        out = bytearray(init)
        struct.pack_into(">HH", out, entry[0] + 24, width, height)
        if tkhd is not None:
            struct.pack_into(">II", out, tkhd[1] - 8, width << 16, height << 16)
        delta = len(body) - len(old)
        struct.pack_into(">I", out, hvcc[0] - 8, len(body) + 8)
        for off in ancestors:
            size = struct.unpack_from(">I", out, off)[0]
            struct.pack_into(">I", out, off, size + delta)
        out[hvcc[0]:hvcc[1]] = body
        return bytes(out)
    except Exception:  # malformed SPS/box: serve go2rtc's init as-is
        logger.warning("Live cache: hvcC repair failed", exc_info=True)
        return None


class _Subscriber:
    """One live client attached to a stream's fanout."""

    __slots__ = ("queue", "dropped")

    def __init__(self) -> None:
        self.queue: asyncio.Queue[bytes | None] = asyncio.Queue(
            maxsize=SUBSCRIBER_QUEUE_FRAGMENTS
        )
        self.dropped = False


class _StreamBuffer:
    """Init segment + fragments since the last keyframe, plus subscribers."""

    def __init__(self, name: str) -> None:
        self.name = name
        self.init: bytes | None = None
        # (fragment bytes, starts a video GOP)
        self.fragments: deque[tuple[bytes, bool]] = deque()
        self.buffered_bytes = 0
        self.subscribers: set[_Subscriber] = set()
        self.last_keyframe_at: float = 0.0
        self.fed_bytes = 0
        # None = single-track stream, so every fragment counts as video.
        self.video_track: int | None = None
        # Incremental parser state
        self._tail = b""
        self._init_parts: list[bytes] = []
        self._pending_moof: bytes | None = None
        self._pending_is_key = False
        self._pending_has_video = True
        self._init_checked = False

    # -- parsing -------------------------------------------------------

    def feed(self, chunk: bytes) -> None:
        self.fed_bytes += len(chunk)
        buf = self._tail + chunk if self._tail else chunk
        consumed = 0
        for btype, payload_start, box_end in _iter_boxes(buf, 0, len(buf)):
            box = buf[consumed:box_end]
            consumed = box_end
            if btype == b"ftyp":
                # A new init segment: the previous session's fragments can no
                # longer be decoded against it.
                self._init_parts = [box]
                self._pending_moof = None
            elif btype == b"moov":
                self._init_parts.append(box)
                self.init = b"".join(self._init_parts)
                self._init_parts = []
                self.video_track = _parse_video_track_id(box)
                self._init_checked = False
                self._reset_fragments()
            elif btype == b"moof":
                self._pending_moof = box
                self._pending_has_video, self._pending_is_key = self._classify(
                    _moof_tracks(box))
            elif btype == b"mdat":
                if self._pending_moof is not None:
                    self._append(self._pending_moof + box,
                                 self._pending_has_video and self._pending_is_key)
                    self._pending_moof = None
        self._tail = buf[consumed:] if consumed < len(buf) else b""

    def _classify(self, trafs: list[tuple[int | None, bool]]) -> tuple[bool, bool]:
        """(carries the video track, that track's sample is a sync sample).

        On a single-track stream ``video_track`` is None and every traf
        qualifies. On video+audio (Front Door) only the ``vide`` traf may
        start a GOP - audio samples are all sync samples, so counting them
        would reset the buffer several times a second and leave a joining
        client with nothing but a stray audio fragment.
        """
        has_video = False
        is_key = False
        for track_id, sync in trafs:
            if self.video_track is None or track_id == self.video_track:
                has_video = True
                is_key = sync
        return has_video, is_key

    def _reset_fragments(self) -> None:
        self.fragments.clear()
        self.buffered_bytes = 0

    def _append(self, fragment: bytes, is_key: bool) -> None:
        if is_key:
            self.last_keyframe_at = time.monotonic()
            if not self._init_checked and self.init is not None:
                self._init_checked = True
                fixed = _repair_hevc_init(self.init, _keyframe_param_sets(fragment))
                if fixed is not None:
                    self.init = fixed
                    logger.info("Live cache: rebuilt hvcC from in-band parameter sets",
                                extra={"stream": self.name})
        elif self.init is None or not self.fragments:
            # Nothing decodable to hang this off yet.
            return
        self.fragments.append((fragment, is_key))
        self.buffered_bytes += len(fragment)
        if is_key:
            self._trim_to_keyframes()
        # Backstop for a stream whose keyframes we never recognise, which
        # would otherwise grow without bound. Note this CAN leave the buffer
        # starting on a non-keyframe; that is the lesser evil against
        # unbounded memory, and a client joining then sees a moment of
        # macroblocks rather than nothing.
        while self.fragments and (
            len(self.fragments) > MAX_BUFFER_FRAGMENTS
            or self.buffered_bytes > MAX_BUFFER_BYTES
        ):
            self.buffered_bytes -= len(self.fragments.popleft()[0])
        self._broadcast(fragment)

    def _trim_to_keyframes(self) -> None:
        """Drop whole GOPs off the front until KEEP_KEYFRAMES remain.

        Only ever cuts AT a keyframe, so ``fragments[0]`` is always something
        a client can start decoding on.
        """
        keys = [i for i, (_, k) in enumerate(self.fragments) if k]
        if len(keys) <= KEEP_KEYFRAMES:
            return
        cut = keys[len(keys) - KEEP_KEYFRAMES]
        for _ in range(cut):
            self.buffered_bytes -= len(self.fragments.popleft()[0])

    # -- fanout --------------------------------------------------------

    def _broadcast(self, fragment: bytes) -> None:
        if not self.subscribers:
            return
        for sub in list(self.subscribers):
            try:
                sub.queue.put_nowait(fragment)
            except asyncio.QueueFull:
                sub.dropped = True
                self.subscribers.discard(sub)
                _drain_and_close(sub)
                logger.warning("Live fanout subscriber too slow - closing",
                               extra={"stream": self.name})

    def attach(self, short: bool = False) -> tuple[bytes, list[bytes], _Subscriber]:
        """Register a subscriber and snapshot the buffer in one step.

        ``short`` starts the backlog at the newest keyframe instead of the
        oldest - see KEEP_KEYFRAMES for who may ask for that.

        Deliberately synchronous throughout: with no await between the
        snapshot and the registration, the event loop cannot deliver a
        fragment that lands in neither, so a client can never miss one or
        receive it twice.
        """
        sub = _Subscriber()
        self.subscribers.add(sub)
        frags = list(self.fragments)
        if short:
            last_key = max((i for i, (_, k) in enumerate(frags) if k), default=0)
            frags = frags[last_key:]
        return self.init or b"", [f for f, _ in frags], sub

    def detach(self, sub: _Subscriber) -> None:
        self.subscribers.discard(sub)

    def close_all(self) -> None:
        for sub in list(self.subscribers):
            _drain_and_close(sub)
        self.subscribers.clear()


def _drain_and_close(sub: _Subscriber) -> None:
    """Signal end-of-stream to a subscriber, making room if the queue is full."""
    try:
        sub.queue.put_nowait(None)
    except asyncio.QueueFull:
        try:
            sub.queue.get_nowait()
            sub.queue.put_nowait(None)
        except (asyncio.QueueEmpty, asyncio.QueueFull):
            pass


class LiveCache:
    """Per-stream GOP caches, fed by StreamManager's keepalive consumers."""

    def __init__(self) -> None:
        self._streams: dict[str, _StreamBuffer] = {}

    def feed(self, stream_name: str, chunk: bytes) -> None:
        buf = self._streams.get(stream_name)
        if buf is None:
            buf = self._streams[stream_name] = _StreamBuffer(stream_name)
        buf.feed(chunk)

    def reset(self, stream_name: str) -> None:
        """Drop the cache and disconnect viewers - the upstream restarted.

        A reconnect brings a fresh init segment, which an in-flight client
        cannot adopt mid-stream, so they are closed and left to reconnect
        (which every client already does on EOF).
        """
        buf = self._streams.get(stream_name)
        if buf is None:
            return
        buf.close_all()
        self._streams.pop(stream_name, None)

    def drop(self, stream_name: str) -> None:
        self.reset(stream_name)

    def is_warm(self, stream_name: str) -> bool:
        buf = self._streams.get(stream_name)
        return bool(buf and buf.init and buf.fragments and buf.fragments[0][1])

    def attach(self, stream_name: str,
               short: bool = False) -> tuple[bytes, list[bytes], _Subscriber] | None:
        buf = self._streams.get(stream_name)
        # fragments[0][1] == "the head is a video keyframe". Serving a backlog
        # that starts mid-GOP hands the client undecodable data, which is
        # worse than falling through to the go2rtc proxy.
        if not buf or not buf.init or not buf.fragments or not buf.fragments[0][1]:
            return None
        return buf.attach(short)

    def detach(self, stream_name: str, sub: _Subscriber) -> None:
        buf = self._streams.get(stream_name)
        if buf:
            buf.detach(sub)

    def stream_names(self) -> list[str]:
        return list(self._streams)

    def newest_keyframe(self, stream_name: str) -> tuple[bytes, bytes, float] | None:
        """(init, newest video-keyframe fragment, when it arrived) - poster source."""
        buf = self._streams.get(stream_name)
        if not buf or not buf.init:
            return None
        for fragment, is_key in reversed(buf.fragments):
            if is_key:
                return buf.init, fragment, buf.last_keyframe_at
        return None

    def stats(self) -> dict[str, dict]:
        return {
            name: {
                "warm": bool(buf.init and buf.fragments and buf.fragments[0][1]),
                "fragments": len(buf.fragments),
                "keyframes": sum(1 for _, k in buf.fragments if k),
                "buffered_bytes": buf.buffered_bytes,
                "subscribers": len(buf.subscribers),
                "age_s": round(time.monotonic() - buf.last_keyframe_at, 2)
                if buf.last_keyframe_at else None,
            }
            for name, buf in self._streams.items()
        }


_cache: LiveCache | None = None


def get_live_cache() -> LiveCache:
    global _cache
    if _cache is None:
        _cache = LiveCache()
    return _cache
