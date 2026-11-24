"""Minimal fragmented-MP4 parsing and rewriting for RIV.

Scope is exactly what go2rtc's ``/api/stream.mp4`` produces (confirmed on
captures 2026-11-24): ``ftyp`` + ``moov``, then one ``moof`` + ``mdat`` per
sample. Each ``moof`` has ``mfhd`` and one ``traf`` with ``tfhd``
(flags 0x20038: default-base-is-moof, default duration/size/flags), ``tfdt``
(version 1) and a ``trun`` carrying only a data offset. Timestamps restart at
zero on every new go2rtc connection. The parser is tolerant of other layouts
(tfdt v0, trun per-sample fields) because a camera or go2rtc upgrade must not
break recording, but rewriting only ever touches mfhd and tfdt, in place, so
byte lengths and the trun data offsets never change.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass

_TFHD_BASE_DATA_OFFSET = 0x000001
_TFHD_SAMPLE_DESC_INDEX = 0x000002
_TFHD_DEFAULT_SAMPLE_DURATION = 0x000008
_TFHD_DEFAULT_SAMPLE_SIZE = 0x000010
_TFHD_DEFAULT_SAMPLE_FLAGS = 0x000020
_TRUN_DATA_OFFSET = 0x000001
_TRUN_FIRST_SAMPLE_FLAGS = 0x000004
_TRUN_SAMPLE_DURATION = 0x000100
_TRUN_SAMPLE_SIZE = 0x000200
_TRUN_SAMPLE_FLAGS = 0x000400
_TRUN_SAMPLE_CTS = 0x000800
_NON_SYNC = 0x00010000


def iter_boxes(buf: bytes, start: int, end: int):
    """Yield (type, payload_start, box_end) for complete boxes in [start, end)."""
    i = start
    while i + 8 <= end:
        size, btype = struct.unpack_from(">I4s", buf, i)
        header = 8
        if size == 1:
            if i + 16 > end:
                return
            size = struct.unpack_from(">Q", buf, i + 8)[0]
            header = 16
        elif size == 0:
            size = end - i
        if size < header or i + size > end:
            return
        yield btype, i + header, i + size
        i += size


@dataclass
class Track:
    track_id: int
    handler: str      # "vide" | "soun" | other
    timescale: int
    codec: str        # sample entry fourcc, e.g. "hev1", "avc1", "mp4a"


def parse_init(init: bytes) -> dict[int, Track]:
    """Tracks declared in an init segment (``ftyp`` + ``moov``)."""
    tracks: dict[int, Track] = {}
    for btype, s, e in iter_boxes(init, 0, len(init)):
        if btype != b"moov":
            continue
        for t2, s2, e2 in iter_boxes(init, s, e):
            if t2 != b"trak":
                continue
            tid, handler, ts, codec = 0, "", 0, ""
            for t3, s3, e3 in iter_boxes(init, s2, e2):
                if t3 == b"tkhd":
                    v = init[s3]
                    tid = struct.unpack_from(">I", init, s3 + (20 if v == 1 else 12))[0]
                elif t3 == b"mdia":
                    for t4, s4, e4 in iter_boxes(init, s3, e3):
                        if t4 == b"mdhd":
                            v = init[s4]
                            ts = struct.unpack_from(">I", init, s4 + (20 if v == 1 else 12))[0]
                        elif t4 == b"hdlr":
                            handler = init[s4 + 8:s4 + 12].decode("latin1")
                        elif t4 == b"minf":
                            codec = _sample_entry(init, s4, e4) or codec
            if tid:
                tracks[tid] = Track(tid, handler, ts, codec)
    return tracks


def _sample_entry(buf: bytes, s: int, e: int) -> str:
    for t, s2, e2 in iter_boxes(buf, s, e):
        if t == b"stbl":
            for t2, s3, e3 in iter_boxes(buf, s2, e2):
                if t2 == b"stsd":
                    for t3, _s4, _e4 in iter_boxes(buf, s3 + 8, e3):
                        return t3.decode("latin1")
    return ""


@dataclass
class TrafInfo:
    track_id: int
    base_time: int          # tfdt baseMediaDecodeTime, track timescale
    duration: int           # sum of sample durations, track timescale
    sync: bool              # first sample is a sync sample
    tfdt_pos: int           # byte offset of the tfdt time field in the fragment
    tfdt_v1: bool


@dataclass
class FragInfo:
    seq_pos: int            # byte offset of mfhd.sequence_number
    trafs: list[TrafInfo]


def parse_fragment(frag: bytes) -> FragInfo | None:
    """Parse the ``moof`` at the start of a moof+mdat fragment."""
    try:
        boxes = list(iter_boxes(frag, 0, len(frag)))
        if not boxes or boxes[0][0] != b"moof":
            return None
        _, ms, me = boxes[0]
        seq_pos = -1
        trafs: list[TrafInfo] = []
        for btype, s, e in iter_boxes(frag, ms, me):
            if btype == b"mfhd":
                seq_pos = s + 4
            elif btype == b"traf":
                t = _parse_traf(frag, s, e)
                if t is not None:
                    trafs.append(t)
        return FragInfo(seq_pos, trafs)
    except (struct.error, IndexError):
        return None


def _parse_traf(buf: bytes, s: int, e: int) -> TrafInfo | None:
    tid = 0
    default_dur = 0
    default_flags: int | None = None
    base = 0
    tfdt_pos = -1
    v1 = True
    duration = 0
    first_flags: int | None = None
    for btype, s2, e2 in iter_boxes(buf, s, e):
        if btype == b"tfhd":
            flags = struct.unpack_from(">I", buf, s2)[0] & 0xFFFFFF
            tid = struct.unpack_from(">I", buf, s2 + 4)[0]
            p = s2 + 8
            if flags & _TFHD_BASE_DATA_OFFSET:
                p += 8
            if flags & _TFHD_SAMPLE_DESC_INDEX:
                p += 4
            if flags & _TFHD_DEFAULT_SAMPLE_DURATION:
                default_dur = struct.unpack_from(">I", buf, p)[0]
                p += 4
            if flags & _TFHD_DEFAULT_SAMPLE_SIZE:
                p += 4
            if flags & _TFHD_DEFAULT_SAMPLE_FLAGS:
                default_flags = struct.unpack_from(">I", buf, p)[0]
        elif btype == b"tfdt":
            v1 = buf[s2] == 1
            tfdt_pos = s2 + 4
            base = struct.unpack_from(">Q" if v1 else ">I", buf, tfdt_pos)[0]
        elif btype == b"trun":
            flags = struct.unpack_from(">I", buf, s2)[0] & 0xFFFFFF
            count = struct.unpack_from(">I", buf, s2 + 4)[0]
            p = s2 + 8
            if flags & _TRUN_DATA_OFFSET:
                p += 4
            if flags & _TRUN_FIRST_SAMPLE_FLAGS:
                first_flags = struct.unpack_from(">I", buf, p)[0]
                p += 4
            per = sum(4 for f in (_TRUN_SAMPLE_DURATION, _TRUN_SAMPLE_SIZE,
                                  _TRUN_SAMPLE_FLAGS, _TRUN_SAMPLE_CTS) if flags & f)
            for i in range(count):
                q = p + i * per
                if flags & _TRUN_SAMPLE_DURATION:
                    duration += struct.unpack_from(">I", buf, q)[0]
                    q += 4
                else:
                    duration += default_dur
                if i == 0 and flags & _TRUN_SAMPLE_SIZE:
                    q += 4
                if i == 0 and first_flags is None and flags & _TRUN_SAMPLE_FLAGS:
                    first_flags = struct.unpack_from(">I", buf, q)[0]
    if not tid:
        return None
    eff = first_flags if first_flags is not None else default_flags
    sync = True if eff is None else (eff & _NON_SYNC) == 0
    return TrafInfo(tid, base, duration, sync, tfdt_pos, v1)


def rewrite_fragment(frag: bytes, info: FragInfo, seq: int,
                     new_times: dict[int, int]) -> bytes:
    """Copy of ``frag`` with a new mfhd sequence number and tfdt values.

    ``new_times`` maps track id -> new baseMediaDecodeTime. Done in place on a
    copy, so the fragment length (and every trun data offset) is unchanged. A
    version-0 tfdt that cannot hold the new value is clamped (never happens
    with go2rtc, which writes version 1).
    """
    out = bytearray(frag)
    if info.seq_pos >= 0:
        struct.pack_into(">I", out, info.seq_pos, seq & 0xFFFFFFFF)
    for t in info.trafs:
        if t.tfdt_pos < 0 or t.track_id not in new_times:
            continue
        v = max(0, new_times[t.track_id])
        if t.tfdt_v1:
            struct.pack_into(">Q", out, t.tfdt_pos, v)
        else:
            struct.pack_into(">I", out, t.tfdt_pos, min(v, 0xFFFFFFFF))
    return bytes(out)
