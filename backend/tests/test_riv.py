"""RIV container tests, on real go2rtc captures (tests/fixtures, 2026-11-24).

Run: cd backend && python -m pytest tests -q
"""

from __future__ import annotations

import asyncio
import json
import os
import random
import shutil
import struct
import subprocess
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from app.services.live_cache import _StreamBuffer, _moof_tracks
from app.services.riv import format as rf
from app.services.riv.fmp4 import parse_fragment, parse_init
from app.services.riv.serve import Run, Source, stream_fmp4
from app.services.riv.writer import RivWriter

FIX = Path(__file__).parent / "fixtures"
FIXTURES = {
    "hevc_s2": FIX / "ptz49_s2_hevc.mp4",
    "h264_aac": FIX / "frontdoor_s2_h264_aac.mp4",
    "hevc_noaudio": FIX / "alfresco_s2_hevc_noaudio.mp4",
}
TZ = ZoneInfo("Australia/Sydney")
T0 = 1_764_000_000_000          # an arbitrary unix ms, 2025-11-24
FFPROBE = shutil.which("ffprobe")


# -- helpers -----------------------------------------------------------------

def capture(name: str) -> list[tuple[bytes, bytes, bool, bool]]:
    """Feed a capture through the real live-cache parser and collect exactly
    what the recording sink receives: (init, fragment, is_key, has_video)."""
    got = []
    buf = _StreamBuffer("t", sink=lambda n, ep, init, frag, k, v: got.append((init, frag, k, v)))
    data = FIXTURES[name].read_bytes()
    for i in range(0, len(data), 7001):          # odd chunking, like the network
        buf.feed(data[i:i + 7001])
    assert got, "capture produced no fragments"
    return got


def video_ms(init: bytes, frag: bytes) -> int:
    tracks = parse_init(init)
    v = next(t for t in tracks.values() if t.handler == "vide")
    info = parse_fragment(frag)
    t = next((x for x in info.trafs if x.track_id == v.track_id), info.trafs[0])
    return t.base_time * 1000 // (tracks[t.track_id].timescale or 90000)


def write(tmp: Path, epochs: list[list], *, jitter=0, gap_ms=0, roll=900, start=T0):
    """Write one or more epochs (connections) of sink items through RivWriter.
    Arrival = start + media time (+ random positive jitter); each new epoch
    starts gap_ms after the previous one ended."""
    rnd = random.Random(1)
    opened, closed = [], []
    w = RivWriter(camera_id=1, camera_name="Cam", safe_name="Cam", stream="s1",
                  root=str(tmp), tz=TZ, roll_seconds=roll,
                  on_open=lambda p, ms: opened.append(p),
                  on_close=lambda p, idx, size: closed.append((p, idx, size)))
    w.start()
    base = start
    for items in epochs:
        key = object()
        last = 0
        for init, frag, k, v in items:
            m = video_ms(init, frag) if v else last
            last = max(last, m)
            w.submit(key, init, frag, k, v, arrival_ms=base + m + rnd.randint(0, jitter))
        base += last + gap_ms
    w.stop()
    assert w.errors == 0 and w.dropped == 0
    return opened, closed


def run_for(closed, start_ms) -> Run:
    srcs = [Source(p, idx.first_wall_ms, idx.last_wall_ms, False) for p, idx, _ in closed]
    return Run(start_ms=start_ms, end_ms=closed[-1][1].last_wall_ms, live=False, sources=srcs)


async def _no_more(*_a):
    return []


def serve(run: Run, end_ms=None) -> bytes:
    async def go():
        out = []
        async for b in stream_fmp4(run, 1, "s1", end_ms=end_ms, more=_no_more):
            out.append(b)
        return b"".join(out)
    return asyncio.run(go())


def boxes(data: bytes):
    i = 0
    while i + 8 <= len(data):
        n, t = struct.unpack_from(">I4s", data, i)
        yield t, data[i:i + n]
        i += n


def frags(data: bytes):
    out, moof = [], None
    for t, b in boxes(data):
        if t == b"moof":
            moof = b
        elif t == b"mdat" and moof:
            out.append(moof + b)
            moof = None
    return out


# -- fmp4 --------------------------------------------------------------------

@pytest.mark.parametrize("name", list(FIXTURES))
def test_parse_matches_live_cache(name):
    items = capture(name)
    init = items[0][0]
    tracks = parse_init(init)
    assert any(t.handler == "vide" for t in tracks.values())
    if name == "h264_aac":
        assert {t.handler for t in tracks.values()} == {"vide", "soun"}
        assert next(t for t in tracks.values() if t.handler == "soun").timescale == 16000
    if name == "hevc_noaudio":
        assert len(tracks) == 1
    assert items[0][2], "the sink must start on a keyframe"
    for _init, frag, _k, _v in items:
        info = parse_fragment(frag)
        lc = _moof_tracks(frag)
        assert [(t.track_id, t.sync) for t in info.trafs] == lc
        assert all(t.duration > 0 for t in info.trafs)


# -- write / index -----------------------------------------------------------

@pytest.mark.parametrize("name", list(FIXTURES))
def test_round_trip_and_clean_index(tmp_path, name):
    items = capture(name)
    _o, closed = write(tmp_path, [items], jitter=40)
    assert len(closed) == 1
    path, idx, size = closed[0]
    assert os.path.getsize(path) == size
    disk, clean = rf.load_index(path)
    assert clean
    assert disk.keyframes == [tuple(k) for k in idx.keyframes]
    # scan without the trailer gives the identical index
    with open(path, "rb") as f:
        scanned = rf.scan(f)
    assert scanned.keyframes == disk.keyframes and scanned.frames == disk.frames
    n_video = sum(1 for i in items if i[3])
    n_key = sum(1 for i in items if i[2])
    assert disk.frames == n_video
    assert len(disk.keyframes) == n_key
    # wall clock: jitter only delays, so the estimate is never late and the
    # first keyframe lands on the start time (0 jitter for the first sample
    # is not guaranteed, so allow the jitter bound)
    assert T0 <= disk.first_wall_ms <= T0 + 40
    walls = [k[0] for k in disk.keyframes]
    assert walls == sorted(walls)


def test_roll_on_keyframe_at_window(tmp_path):
    items = capture("hevc_noaudio")
    # start 1 s before a window boundary with 2 s windows: must roll at the
    # first keyframe at or after the boundary, never mid-GOP
    start = (T0 // 2000 + 1) * 2000 - 1000
    _o, closed = write(tmp_path, [items], roll=2, start=start)
    assert len(closed) >= 2
    for p, idx, _ in closed:
        with open(p, "rb") as f:
            recs = []
            off = 0
            while True:
                r = rf.read_record(f, off)
                if r is None or r.rtype == rf.INDEX:
                    break
                recs.append(r)
                off += r.size
        assert [r.rtype for r in recs[:2]] == [rf.HEAD, rf.INIT]
        assert recs[2].rtype == rf.FRAG and recs[2].flags & rf.F_KEY
    total = sum(idx.frames for _p, idx, _s in closed)
    assert total == sum(1 for i in items if i[3])


def test_truncation_recovers_complete_records(tmp_path):
    items = capture("hevc_s2")
    _o, closed = write(tmp_path, [items])
    path = closed[0][0]
    data = Path(path).read_bytes()
    idx, _ = rf.load_index(path)
    end = idx.data_end
    # record boundaries
    offs, off = [], 0
    with open(path, "rb") as f:
        while off < end:
            r = rf.read_record(f, off)
            offs.append((off, r))
            off += r.size
    rnd = random.Random(7)
    cuts = list(range(end - 300, end + 1)) + [rnd.randrange(64, end) for _ in range(150)]
    p2 = tmp_path / "cut.riv"
    for cut in cuts:
        p2.write_bytes(data[:cut])
        with open(p2, "rb") as f:
            got = rf.scan(f)
        complete = [r for o, r in offs if o + r.size <= cut]
        assert got.data_end == (complete[-1].offset + complete[-1].size if complete else 0)
        assert got.frames == sum(1 for r in complete if r.rtype == rf.FRAG and r.flags & rf.F_VIDEO)


def test_crc_corruption_stops_scan(tmp_path):
    items = capture("hevc_s2")
    _o, closed = write(tmp_path, [items])
    path, idx, _ = closed[0]
    data = bytearray(Path(path).read_bytes())
    k = idx.keyframes[len(idx.keyframes) // 2]
    data[k[2] + rf.HEADER.size + 50] ^= 0xFF     # flip a payload byte
    p2 = tmp_path / "bad.riv"
    p2.write_bytes(bytes(data[:idx.data_end]))    # drop the trailer too
    with open(p2, "rb") as f:
        got = rf.scan(f)
    assert got.data_end == k[2]
    assert len(got.keyframes) == len(idx.keyframes) // 2


def test_recover_file_after_crash(tmp_path):
    """Torn tail cut, trailer appended: the file then loads cleanly and the
    served stream decodes up to the last complete record."""
    items = capture("hevc_s2")
    _o, closed = write(tmp_path, [items])
    path, idx, _ = closed[0]
    data = Path(path).read_bytes()
    crash = tmp_path / "crash.riv"
    crash.write_bytes(data[:idx.data_end - 777])       # mid-record, no trailer
    got, size = rf.recover_file(str(crash))
    again, clean = rf.load_index(str(crash))
    assert clean and again.frames == got.frames == idx.frames - 1
    assert size == os.path.getsize(crash)
    rf.recover_file(str(crash))                         # idempotent
    assert os.path.getsize(crash) == size


# -- serving -----------------------------------------------------------------

def _video_track_times(data: bytes):
    init = data[:sum(len(b) for t, b in list(boxes(data))[:2])]
    tracks = parse_init(init)
    vt = next(t for t in tracks.values() if t.handler == "vide").track_id
    seqs, times = [], {tid: [] for tid in tracks}
    for f in frags(data):
        info = parse_fragment(f)
        seqs.append(struct.unpack_from(">I", f, info.seq_pos)[0])
        for t in info.trafs:
            times[t.track_id].append(t.base_time)
    return vt, tracks, seqs, times


@pytest.mark.parametrize("name", list(FIXTURES))
def test_serve_from_middle_keyframe(tmp_path, name):
    items = capture(name)
    _o, closed = write(tmp_path, [items], jitter=30)
    idx = closed[0][1]
    k = idx.keyframes[len(idx.keyframes) // 2]
    out = serve(run_for(closed, k[0] + 10))       # just after the keyframe
    vt, tracks, seqs, times = _video_track_times(out)
    assert seqs == list(range(1, len(seqs) + 1))
    assert times[vt][0] == 0, "output must start exactly on the keyframe"
    assert times[vt] == sorted(times[vt])
    first = parse_fragment(frags(out)[0])
    assert first.trafs[0].sync
    for tid, ts in times.items():
        assert all(t >= 0 for t in ts)
        assert ts == sorted(ts)
    # same number of video frames as from that keyframe in the input
    key_positions = [n for n, it in enumerate(items) if it[2]]
    from_kf = items[key_positions[len(idx.keyframes) // 2]:]
    assert len(times[vt]) == sum(1 for it in from_kf if it[3])


@pytest.mark.skipif(not FFPROBE, reason="ffprobe not installed")
@pytest.mark.parametrize("name", list(FIXTURES))
def test_served_stream_decodes(tmp_path, name):
    items = capture(name)
    _o, closed = write(tmp_path, [items])
    idx = closed[0][1]
    out = serve(run_for(closed, idx.keyframes[1][0]))
    p = tmp_path / "out.mp4"
    p.write_bytes(out)
    r = subprocess.run(["ffmpeg", "-v", "error", "-i", str(p), "-f", "null", "-"],
                       capture_output=True, text=True)
    # The camera's own timestamps make ffmpeg's null muxer warn about
    # repeated dts on the untouched capture too; anything else is a failure.
    bad = [ln for ln in r.stderr.splitlines()
           if ln.strip() and "non monotonically increasing dts" not in ln]
    assert r.returncode == 0 and not bad, bad[:5]
    j = json.loads(subprocess.run(
        [FFPROBE, "-v", "error", "-select_streams", "v:0", "-show_entries",
         "stream=start_time", "-of", "json", str(p)], capture_output=True, text=True).stdout)
    assert float(j["streams"][0]["start_time"]) == 0.0


def test_serve_across_epochs_and_files(tmp_path):
    """Two connections (timestamps restart at 0) 3 s apart, over several
    files: one continuous output whose timeline follows the wall clock."""
    items = capture("hevc_noaudio")
    _o, closed = write(tmp_path, [items, items], gap_ms=3000, roll=3)
    assert len(closed) >= 3
    out = serve(run_for(closed, closed[0][1].first_wall_ms))
    vt, tracks, seqs, times = _video_track_times(out)
    v = times[vt]
    assert v == sorted(v) and len(set(v)) == len(v)
    total = sum(idx.frames for _p, idx, _s in closed)
    assert len(v) == total
    # the reconnect gap shows up as a ~3 s step in output time (±1 frame)
    steps = [b - a for a, b in zip(v, v[1:])]
    assert max(steps) == pytest.approx(3000 * 90 + steps[0], abs=90 * 150)


def test_serve_stops_at_long_gap(tmp_path):
    items = capture("hevc_noaudio")
    _o, closed = write(tmp_path, [items, items], gap_ms=60_000)
    out = serve(run_for(closed, closed[0][1].first_wall_ms))
    _vt, _tr, _s, times = _video_track_times(out)
    assert len(times[_vt]) == sum(1 for i in items if i[3])


def test_serve_end_ms(tmp_path):
    items = capture("hevc_noaudio")
    _o, closed = write(tmp_path, [items])
    idx = closed[0][1]
    end = idx.keyframes[2][0]
    out = serve(run_for(closed, idx.first_wall_ms), end_ms=end)
    _vt, _tr, _s, times = _video_track_times(out)
    assert max(times[_vt]) < (end - idx.first_wall_ms + 200) * 90


def test_concatenated_epoch_is_valid_mp4(tmp_path):
    """Escape hatch: INIT + FRAGs of one epoch is a plain fMP4."""
    items = capture("h264_aac")
    _o, closed = write(tmp_path, [items])
    path = closed[0][0]
    parts, off = [], 0
    with open(path, "rb") as f:
        while True:
            r = rf.read_record(f, off)
            if r is None or r.rtype == rf.INDEX:
                break
            if r.rtype in (rf.INIT, rf.FRAG):
                parts.append(r.payload)
            off += r.size
    data = b"".join(parts)
    assert data == items[0][0] + b"".join(i[1] for i in items)


def test_reverse_chunks_are_keyframe_aligned(tmp_path, monkeypatch):
    from app.services.riv import serve as sv
    items = capture("hevc_s2")
    _o, closed = write(tmp_path, [items, items], gap_ms=1000)
    srcs = [Source(p, idx.first_wall_ms, idx.last_wall_ms, False) for p, idx, _ in closed]

    async def fake_sources(*_a):
        return srcs
    monkeypatch.setattr(sv, "sources_from", fake_sources)
    walls = sorted(k[0] for _p, idx, _s in closed for k in idx.keyframes)
    start = closed[-1][1].last_wall_ms - 500
    chunks, more = asyncio.run(sv.reverse_chunks(1, "s1", start, chunk_seconds=4))
    assert chunks[0][1] == start
    for (s0, e0), (s1, e1) in zip(chunks, chunks[1:]):
        assert e1 == s0                       # contiguous, latest first
    assert all(s in walls for s, _e in chunks)  # every chunk starts on a keyframe
    assert chunks[-1][0] == walls[0] and not more


def test_keyframe_fast_forward(tmp_path):
    from app.services.riv.serve import stream_keyframes
    items = capture("hevc_noaudio")
    _o, closed = write(tmp_path, [items], roll=4)
    run = run_for(closed, closed[0][1].first_wall_ms)

    async def go():
        return b"".join([b async for b in stream_keyframes(run, 16)])
    out = asyncio.run(go())
    vt, _tr, seqs, times = _video_track_times(out)
    walls = [k[0] for _p, idx, _s in closed for k in idx.keyframes]
    assert len(times[vt]) == len(walls)
    assert seqs == list(range(1, len(seqs) + 1))
    for t, w in zip(times[vt], walls):
        assert abs(t - (w - walls[0]) * 90 // 16) <= 1
    assert all(parse_fragment(f).trafs[0].sync for f in frags(out))
