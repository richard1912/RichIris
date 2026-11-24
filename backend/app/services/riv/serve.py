"""Serve RIV footage as one continuous fragmented MP4.

``stream_fmp4`` starts at the keyframe at or before the requested wall time
and writes the stored init segment followed by the stored fragments, with
only two fields rewritten in each (``mfhd`` sequence number and ``tfdt``), so
position 0 of the output is exactly that keyframe. It runs across file
boundaries and go2rtc reconnects (each a new "epoch" whose timestamps restart
at zero) by re-anchoring on the index's wall clock, and when it reaches the
file being written it keeps following it, i.e. it turns into live.

It ends (and the client asks again from ``run end``) at a gap in the footage
longer than ``GAP_MS`` or when the codec parameters change, because neither
can be expressed inside one fMP4 stream without the player stalling.
No ffmpeg anywhere.
"""

from __future__ import annotations

import asyncio
import bisect
import os
from dataclasses import dataclass
from typing import AsyncIterator

from . import format as rf
from .fmp4 import parse_fragment, parse_init, rewrite_fragment

GAP_MS = 30_000
READ_BATCH = 1 << 20
FOLLOW_POLL = 0.05
FOLLOW_IDLE_LIMIT = 20.0    # live file not growing for this long: stop


@dataclass
class Source:
    path: str
    start_ms: int
    end_ms: int | None
    live: bool


_index_cache: dict[str, tuple[float, int, rf.Index]] = {}


def index_for(src: Source) -> rf.Index:
    if src.live:
        from .manager import get_riv_manager
        for w in get_riv_manager().writers.values():
            cur = w.current
            if cur is not None and cur.path == src.path:
                return cur.index
    st = os.stat(src.path)
    hit = _index_cache.get(src.path)
    if hit and hit[0] == st.st_mtime and hit[1] == st.st_size:
        return hit[2]
    idx, _clean = rf.load_index(src.path)
    if len(_index_cache) > 256:
        _index_cache.clear()
    _index_cache[src.path] = (st.st_mtime, st.st_size, idx)
    return idx


async def sources_from(camera_id: int, stream: str, start_ms: int) -> list[Source]:
    """Files that can contain footage at or after start_ms, oldest first."""
    from sqlalchemy import select

    from app.database import get_session_factory
    from app.models import RivFile
    from .manager import get_riv_manager
    live_path = None
    cur = get_riv_manager().current_file(camera_id, stream)
    if cur is not None:
        live_path = cur.path
    async with get_session_factory()() as s:
        rows = (await s.execute(
            select(RivFile).where(
                RivFile.camera_id == camera_id, RivFile.stream == stream,
                (RivFile.end_ms >= start_ms) | (RivFile.end_ms.is_(None))
                | (RivFile.in_progress == True),  # noqa: E712
            ).order_by(RivFile.start_ms.asc())
        )).scalars().all()
    out = []
    for r in rows:
        if not os.path.exists(r.file_path):
            continue
        out.append(Source(r.file_path, r.start_ms, r.end_ms, r.file_path == live_path))
    return out


@dataclass
class Run:
    start_ms: int          # wall time of the keyframe playback will start on
    end_ms: int            # last wall time of continuous footage
    live: bool             # the run reaches the file being written
    sources: list[Source]


async def find_run(camera_id: int, stream: str, start_ms: int,
                   backward: bool = False) -> Run | None:
    """The continuous stretch of footage that playback from start_ms plays.

    start_ms inside a gap starts at the next footage (forward)."""
    srcs = await sources_from(camera_id, stream, start_ms - 15 * 60_000)
    pairs = await asyncio.to_thread(_with_indexes, srcs)
    used: list[Source] = []
    begin: int | None = None
    prev: int | None = None
    live = False
    for src, idx in pairs:
        kfs = idx.keyframes
        if not kfs:
            continue
        if begin is None:
            if idx.last_wall_ms < start_ms and not src.live:
                continue
            i = idx.keyframe_at(start_ms)
            begin = kfs[i][0]
            walk = kfs[i:]
        else:
            walk = kfs
            if prev is not None and walk[0][0] - prev > GAP_MS:
                break           # gap at the file boundary
        stop = False
        for k in walk:
            if prev is not None and k[0] - prev > GAP_MS:
                stop = True     # gap inside the file (camera reconnect)
                break
            prev = k[0]
        used.append(src)
        if stop:
            live = False
            break
        prev = max(prev or 0, idx.last_wall_ms)
        live = src.live
    if begin is None or not used:
        return None
    return Run(start_ms=begin, end_ms=prev or begin, live=live and prev is not None, sources=used)


async def reverse_chunks(camera_id: int, stream: str, start_ms: int,
                         max_seconds: float = 900, chunk_seconds: float = 8.0
                         ) -> tuple[list[tuple[int, int]], bool] | None:
    """Keyframe-aligned chunks for a reverse render ending at start_ms.

    Returns ([(chunk_start_ms, chunk_end_ms), ...] latest first, has_more) or
    None. Walks back over keyframes (across files) until max_seconds or a gap
    longer than GAP_MS; each chunk starts ON a keyframe and the first one ends
    at start_ms (clamped to the footage). has_more = footage exists before.
    """
    srcs = await sources_from(camera_id, stream, start_ms - int(max_seconds * 1000) - 900_000)
    pairs = await asyncio.to_thread(_with_indexes, srcs)
    kfs: list[int] = []
    last_wall = None
    for _src, idx in pairs:
        for k in idx.keyframes:
            if k[0] <= start_ms:
                kfs.append(k[0])
        if idx.keyframes and idx.keyframes[0][0] <= start_ms:
            last_wall = max(last_wall or 0, min(idx.last_wall_ms, start_ms))
    if not kfs or last_wall is None:
        return None
    kfs.sort(reverse=True)
    end = last_wall
    floor = start_ms - int(max_seconds * 1000)
    chunks: list[tuple[int, int]] = []
    cur_end = end
    prev = end
    for k in kfs:
        if prev - k > GAP_MS:
            break
        if k < floor and chunks:
            break
        prev = k
        if cur_end - k >= chunk_seconds * 1000:
            chunks.append((k, cur_end))
            cur_end = k
    if cur_end > prev:
        chunks.append((prev, cur_end))
    has_more = len(kfs) > 0 and kfs[-1] < (chunks[-1][0] if chunks else end)
    return chunks, has_more


def video_codec(path: str) -> str:
    """Sample-entry fourcc of the video track in a RIV file's first init."""
    with open(path, "rb") as f:
        off = 0
        for _ in range(4):
            rec = rf.read_record(f, off)
            if rec is None:
                return ""
            if rec.rtype == rf.INIT:
                v = next((t for t in parse_init(rec.payload).values() if t.handler == "vide"), None)
                return v.codec if v else ""
            off += rec.size
    return ""


def _with_indexes(srcs: list[Source]) -> list[tuple[Source, rf.Index]]:
    out = []
    for s in srcs:
        try:
            out.append((s, index_for(s)))
        except OSError:
            continue
    return out


class _Reader:
    """Sequential record reader over one file, safe on a growing file."""

    def __init__(self, path: str, offset: int) -> None:
        self.f = open(path, "rb")
        self.offset = offset

    def read_batch(self, limit: int) -> list[rf.Record]:
        out: list[rf.Record] = []
        got = 0
        while got < READ_BATCH:
            rec = rf.read_record(self.f, self.offset, limit)
            if rec is None:
                break
            out.append(rec)
            self.offset += rec.size
            got += rec.size
            if rec.rtype == rf.INDEX:
                break
        return out

    def close(self) -> None:
        self.f.close()


class _Timeline:
    """Maps stored tfdt values onto one continuous output timeline."""

    def __init__(self) -> None:
        self.init: bytes | None = None
        self.tracks = {}
        self.video_tid = 0
        self.vts = 90000
        self.base: dict[int, int] = {}
        self.start_wall: int | None = None
        self.last_out_v = -1
        self.seq = 1
        self.need_anchor = True

    def set_init(self, payload: bytes) -> bool:
        """False if this init cannot continue the current output stream."""
        if self.init is not None and payload != self.init:
            return False
        if self.init is None:
            self.init = payload
            self.tracks = parse_init(payload)
            v = next((t for t in self.tracks.values() if t.handler == "vide"), None)
            self.video_tid = v.track_id if v else next(iter(self.tracks), 0)
            self.vts = (v.timescale if v else 0) or 90000
        self.need_anchor = True
        return True

    def anchor(self, kf_wall: int, kf_media: int) -> None:
        media_s = kf_media / self.vts
        if self.start_wall is None:
            self.start_wall = kf_wall
            new_out_v = 0
        else:
            expected = (kf_wall - self.start_wall) * self.vts // 1000
            cont = kf_media - self.base.get(self.video_tid, kf_media)
            if self.base and abs(cont - expected) < 2 * self.vts and cont > self.last_out_v:
                self.need_anchor = False
                return
            new_out_v = max(expected, self.last_out_v + self.vts // 25)
        for tid, t in self.tracks.items():
            ts = t.timescale or self.vts
            self.base[tid] = round(media_s * ts) - round(new_out_v / self.vts * ts)
        self.need_anchor = False

    def emit(self, frag: bytes) -> bytes | None:
        info = parse_fragment(frag)
        if info is None:
            return None
        times = {}
        for t in info.trafs:
            out = t.base_time - self.base.get(t.track_id, 0)
            if out < 0:
                return None
            times[t.track_id] = out
            if t.track_id == self.video_tid:
                self.last_out_v = out
        data = rewrite_fragment(frag, info, self.seq, times)
        self.seq += 1
        return data


async def stream_fmp4(run: Run, camera_id: int, stream: str,
                      end_ms: int | None = None, more=None) -> AsyncIterator[bytes]:
    """``more(camera_id, stream, after_ms)`` finds newer files (default: the DB)."""
    more = more or sources_from
    tl = _Timeline()
    first = True
    srcs = list(run.sources)
    seen: set[str] = set()
    while srcs:
        src = srcs.pop(0)
        seen.add(src.path)
        idx = await asyncio.to_thread(index_for, src)
        if not idx.keyframes and not src.live:
            continue
        if first:
            k = idx.keyframes[idx.keyframe_at(run.start_ms)]
            epoch = k[3]
            init_off = idx.epochs[epoch][0]
            with open(src.path, "rb") as f:
                init_rec = rf.read_record(f, init_off)
            if init_rec is None or not tl.set_init(init_rec.payload):
                return
            yield init_rec.payload
            offset = k[2]
            first = False
        else:
            offset = min((e[0] for e in idx.epochs.values()), default=0)
        kf_offsets = [k[2] for k in idx.keyframes]
        reader = _Reader(src.path, offset)
        idle = 0.0
        try:
            while True:
                limit = idx.data_end if src.live else None
                batch = await asyncio.to_thread(reader.read_batch, limit or (1 << 62))
                if not batch:
                    if not src.live:
                        break
                    from .manager import get_riv_manager
                    cur = get_riv_manager().current_file(camera_id, stream)
                    if cur is None or cur.path != src.path:
                        # writer rolled to a new file (or stopped): drain, move on
                        idx = await asyncio.to_thread(index_for, Source(src.path, 0, None, False))
                        tail = await asyncio.to_thread(reader.read_batch, 1 << 62)
                        if not tail:
                            nxt = await more(camera_id, stream, idx.last_wall_ms)
                            srcs = [s for s in nxt if s.path not in seen]
                            break
                        batch = tail
                    else:
                        idle += FOLLOW_POLL
                        if idle > FOLLOW_IDLE_LIMIT:
                            return
                        await asyncio.sleep(FOLLOW_POLL)
                        continue
                idle = 0.0
                for rec in batch:
                    if rec.rtype == rf.INIT:
                        if not tl.set_init(rec.payload):
                            return
                    elif rec.rtype == rf.FRAG:
                        is_key = rec.flags & rf.F_KEY
                        if tl.need_anchor and not is_key:
                            continue
                        if is_key:
                            if len(kf_offsets) != len(idx.keyframes):
                                kf_offsets = [k[2] for k in idx.keyframes]
                            i = bisect.bisect_left(kf_offsets, rec.offset)
                            if i >= len(kf_offsets) or kf_offsets[i] != rec.offset:
                                if tl.need_anchor:
                                    continue
                                kf_wall = None
                            else:
                                kf_wall = idx.keyframes[i][0]
                            if end_ms is not None and kf_wall is not None                                     and kf_wall >= end_ms and tl.start_wall is not None:
                                return
                            if tl.need_anchor:
                                if tl.start_wall is not None and kf_wall - _out_wall(tl) > GAP_MS:
                                    return      # gap: the client continues from run end
                                tl.anchor(kf_wall, rec.media_ts)
                        data = tl.emit(rec.payload)
                        if data:
                            yield data
                    elif rec.rtype == rf.INDEX:
                        break
        finally:
            reader.close()
        if not srcs and not first:
            # Closed file was the last known one: pick up anything newer.
            nxt = await more(camera_id, stream, idx.last_wall_ms)
            srcs = [s for s in nxt if s.path not in seen and s.start_ms >= idx.first_wall_ms]
            if srcs and srcs[0].start_ms - idx.last_wall_ms > GAP_MS:
                return


async def stream_keyframes(run: Run, speed: int, end_ms: int | None = None
                           ) -> AsyncIterator[bytes]:
    """Fast forward: keyframes only, timed at wall offset / speed.

    Output position p (seconds) is wall time ``run.start_ms + p * speed``.
    Read straight from the index (one record per GOP), so 32x costs about as
    much disk as 1x of a sub stream and needs no decoding or transcoding.
    Ends at a codec change; gaps simply pass by faster.
    """
    speed = max(2, int(speed))
    init: bytes | None = None
    vtid, vts = 0, 90000
    seq = 1
    last_out = -1
    for src in run.sources:
        idx = await asyncio.to_thread(index_for, src)
        kfs = list(idx.keyframes)
        if not kfs:
            continue
        i = idx.keyframe_at(run.start_ms) if seq == 1 else 0
        init_off = None
        with open(src.path, "rb") as f:
            for wall, _media, off, epoch in kfs[i:]:
                if wall < run.start_ms:
                    continue
                if end_ms is not None and wall >= end_ms:
                    return
                if init is None or idx.epochs[epoch][0] != init_off:
                    rec = await asyncio.to_thread(rf.read_record, f, idx.epochs[epoch][0])
                    if rec is None:
                        return
                    init_off = idx.epochs[epoch][0]
                    if init is None:
                        init = rec.payload
                        tr = parse_init(init)
                        v = next((t for t in tr.values() if t.handler == "vide"), None)
                        vtid = v.track_id if v else 0
                        vts = (v.timescale if v else 0) or 90000
                        yield init
                    elif rec.payload != init:
                        return
                rec = await asyncio.to_thread(rf.read_record, f, off)
                if rec is None or rec.rtype != rf.FRAG:
                    continue
                info = parse_fragment(rec.payload)
                if info is None:
                    continue
                out = (wall - run.start_ms) * vts // (1000 * speed)
                if out <= last_out:
                    out = last_out + 1
                last_out = out
                yield rewrite_fragment(rec.payload, info, seq,
                                       {t.track_id: out for t in info.trafs})
                seq += 1


def keyframe_at(src: Source, at_ms: int) -> tuple[int, bytes] | None:
    """(wall_ms, init + keyframe fragment) for the keyframe at or before at_ms
    in one file: a complete one-frame fMP4 that decodes on its own."""
    idx = index_for(src)
    if not idx.keyframes:
        return None
    wall, _media, off, epoch = idx.keyframes[idx.keyframe_at(at_ms)]
    if wall > at_ms + 5_000:
        return None
    with open(src.path, "rb") as f:
        init = rf.read_record(f, idx.epochs[epoch][0])
        frag = rf.read_record(f, off)
    if init is None or frag is None:
        return None
    info = parse_fragment(frag.payload)
    if info is None:
        return None
    return wall, init.payload + rewrite_fragment(frag.payload, info, 1,
                                                 {t.track_id: 0 for t in info.trafs})


def _out_wall(tl: _Timeline) -> int:
    return (tl.start_wall or 0) + max(0, tl.last_out_v) * 1000 // tl.vts
