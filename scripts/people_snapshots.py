#!/opt/richiris/venv/bin/python
"""People snapshots: a crop of every person RichIris ever detected.

Runs beside the NVR (richiris-people.service). For every person event in the
NVR database it decodes keyframes from the recording that covers the event,
runs a higher-resolution person detector on the full 4K frame, and saves one
crop per person per sampled frame, plus a 1280-wide context frame with the
boxes drawn. Events older than the recording archive fall back to the 640x480
onset thumbnail the motion detector saved. Crops are uploaded to Immich (the separate
"RichIris People" user) into ONE "People" album, shared with Richard, as
ARCHIVED assets (out of the main timeline).

Since 2026-11-19 the same daemon also does VEHICLES (cars, trucks, buses,
motorcycles, bicycles) into /var/lib/richiris/vehicles and a "Vehicles" album:
same crops, but a vehicle that sits in the same place at the start and the end
of the event is parked and skipped, and an event where the whole picture moved
(the PTZ lens tracking) is skipped. Both kinds also get a short 720p CLIP of
each event that produced a crop (events from CLIPS_FROM on, no backfill); the
clip is deleted locally once Immich has it.

Layout:  /var/lib/richiris/people/<date>/<camera>/<HHMMSS>_e<event>_<n>.jpg
         /var/lib/richiris/people/<date>/<camera>/ctx/<HHMMSS>_e<event>_f<k>.jpg
         /var/lib/richiris/people/people.db   (what has been done, Immich ids)
         /var/lib/richiris/people/people.log

Usage:   people_snapshots.py --daemon            (what the service runs)
         people_snapshots.py --once [--limit N]  (one cycle)
         people_snapshots.py --event ID [ID...]  (re-process specific events)
         people_snapshots.py --stats
         --kind people|vehicles restricts any of the above to one kind
Env:     PEOPLE_NO_UPLOAD=1 skips Immich, PEOPLE_FORCE=1 re-does events already done,
         PEOPLE_THREADS, PEOPLE_CONF, PEOPLE_MODEL override the defaults below.
"""
from __future__ import annotations

import argparse
import json
import logging
import logging.handlers
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timedelta
from pathlib import Path

import cv2
import numpy as np

# ---------------------------------------------------------------- config
NVR_DB = os.environ.get("PEOPLE_NVR_DB", "/var/lib/richiris/database/richiris.db")
OUT_DIR = Path(os.environ.get("PEOPLE_OUT", "/var/lib/richiris/people"))


class Kind:
    """One album: which NVR events feed it, which COCO classes are cropped, where it lives."""

    def __init__(self, name, noun, labels, classes, out_dir, album_name, album_key,
                 min_h, min_h_small, thumb_fallback, drop_static):
        self.name, self.noun, self.labels, self.classes = name, noun, labels, classes
        self.out_dir, self.album_name, self.album_key = Path(out_dir), album_name, album_key
        self.min_h, self.min_h_small = min_h, min_h_small
        self.thumb_fallback, self.drop_static = thumb_fallback, drop_static


COCO = {0: "person", 1: "bicycle", 2: "car", 3: "motorcycle", 5: "bus", 7: "truck"}
KINDS = {
    "people": Kind("people", "Person", ("person",), {0}, OUT_DIR, "People", "IMMICH_ALBUM",
                   48, 28, thumb_fallback=True, drop_static=False),
    "vehicles": Kind("vehicles", "Vehicle", ("car", "truck", "bus", "motorcycle", "bicycle"), {1, 2, 3, 5, 7},
                     os.environ.get("VEHICLES_OUT", "/var/lib/richiris/vehicles"), "Vehicles",
                     "IMMICH_ALBUM_VEHICLES", 70, 40, thumb_fallback=False, drop_static=True),
}
K = KINDS["people"]         # the kind being processed; switched by run_cycle
MODEL = os.environ.get("PEOPLE_MODEL", "/opt/richiris/dependencies/models/yolo11s-1280.onnx")
THREADS = int(os.environ.get("PEOPLE_THREADS", "3"))
CONF = float(os.environ.get("PEOPLE_CONF", "0.30"))
NMS_IOU = 0.5
# Minimum box height: Kind.min_h in 4K frames, Kind.min_h_small in the 640x480 onset thumbnails.
STATIC_IOU = 0.7            # vehicles: same place in the first and the last frame = parked
STATIC_NEAR_IOU = 0.45      # ... and a box overlapping a parked one this much is the same parked vehicle
STATIC_MIN_SPAN_S = 5.0     # ... only judged when those frames are at least this far apart
LENS_MOVED_DIFF = 22.0      # vehicles: mean grey difference first vs last frame above this = the view moved
CLIPS_FROM = os.environ.get("PEOPLE_CLIPS_FROM", "2026-11-19 00:00:00")   # no clips for older events
CLIP_PRE_S = 6.0            # the recording is 2 to 3.5 s ahead of the event's start_time
CLIP_POST_S = 4.0
CLIP_MAX_S = 40.0
CLIP_HEIGHT = 720           # fit inside CLIP_WIDTH x CLIP_HEIGHT (the panorama is 3.5:1)
CLIP_WIDTH = 1920
CLIP_CRF = 27
CLIP_UPLOAD_BATCH = 6
PRE_S = 8.0                 # window before the event: the AI confirms a person seconds after they appear
POST_S = 12.0               # stream latency is 3-10 s, so the person is later in the file
MIN_GAP_S = 2.5             # sample keyframes at most this often
MAX_FRAMES = 14             # per event
MAX_CROPS = 10              # per event, the tallest (closest) ones win
DUP_IOU = 0.6               # a person who has not moved since the last kept crop is skipped
MARGIN_W = 0.30             # crop margin as a fraction of box width/height
MARGIN_H = 0.12
CTX_WIDTH = 1280
JPEG_CROP_Q = 92
JPEG_CTX_Q = 80
FFMPEG = os.environ.get("PEOPLE_FFMPEG", "ffmpeg")
IMMICH_ENV = os.environ.get("PEOPLE_IMMICH_ENV", "/opt/richiris/secrets/people_immich.env")
UPLOAD_BATCH = 40
EVENTS_PER_CYCLE = 8
EVENT_SETTLE_S = 5.0        # process an event only once end + POST_S + this is in the past
FORCE_CLOSE_S = 150.0       # an event with no end_time this old is treated as closed
RETRY_MAX = 4

log = logging.getLogger("people")


# ---------------------------------------------------------------- helpers
def parse_ts(s: str | None) -> datetime | None:
    if not s:
        return None
    s = s.strip()
    for fmt in ("%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S.%f", "%Y-%m-%dT%H:%M:%S"):
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            continue
    return None


def safe_name(s: str) -> str:
    return re.sub(r"[^A-Za-z0-9 _.-]", "_", s).strip()


def local_offset_str(dt: datetime) -> str:
    off = dt.astimezone().utcoffset() or timedelta(0)
    total = int(off.total_seconds())
    sign = "+" if total >= 0 else "-"
    total = abs(total)
    return f"{sign}{total // 3600:02d}:{(total % 3600) // 60:02d}"


def iou(a, b) -> float:
    x1, y1 = max(a[0], b[0]), max(a[1], b[1])
    x2, y2 = min(a[2], b[2]), min(a[3], b[3])
    inter = max(0.0, x2 - x1) * max(0.0, y2 - y1)
    ua = (a[2] - a[0]) * (a[3] - a[1]) + (b[2] - b[0]) * (b[3] - b[1]) - inter
    return inter / ua if ua > 0 else 0.0


def setup_logging(to_stdout: bool) -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)   # one log for both kinds
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
    fh = logging.handlers.RotatingFileHandler(OUT_DIR / "people.log", maxBytes=5_000_000, backupCount=3)
    fh.setFormatter(fmt)
    log.addHandler(fh)
    if to_stdout:
        sh = logging.StreamHandler(sys.stdout)
        sh.setFormatter(fmt)
        log.addHandler(sh)
    log.setLevel(logging.INFO)


# ---------------------------------------------------------------- detector
class PersonDetector:
    """YOLO11 ONNX (fixed square input) returning person boxes in frame pixels."""

    def __init__(self, model: str, threads: int):
        import onnxruntime as ort
        so = ort.SessionOptions()
        so.intra_op_num_threads = threads
        so.inter_op_num_threads = 1
        self.sess = ort.InferenceSession(model, so, providers=["CPUExecutionProvider"])
        inp = self.sess.get_inputs()[0]
        self.input_name = inp.name
        self.size = int(inp.shape[2])
        log.info("detector loaded model=%s input=%d threads=%d", Path(model).name, self.size, threads)

    def _prep(self, img: np.ndarray):
        h, w = img.shape[:2]
        r = min(self.size / h, self.size / w)
        nh, nw = max(1, int(round(h * r))), max(1, int(round(w * r)))
        im = cv2.resize(img, (nw, nh), interpolation=cv2.INTER_AREA if r < 1 else cv2.INTER_LINEAR)
        canvas = np.full((self.size, self.size, 3), 114, np.uint8)
        canvas[:nh, :nw] = im
        x = canvas[:, :, ::-1].transpose(2, 0, 1)[None].astype(np.float32) / 255.0
        return np.ascontiguousarray(x), r

    def detect(self, img: np.ndarray, conf: float, classes: set[int]) -> list[tuple]:
        """Boxes (x1, y1, x2, y2, confidence, coco class) of the wanted classes, best first."""
        x, r = self._prep(img)
        out = self.sess.run(None, {self.input_name: x})[0][0].T  # [N, 84]
        scores = out[:, 4:]
        cls = scores.argmax(1)
        cf = scores.max(1)
        m = np.isin(cls, list(classes)) & (cf >= conf)
        boxes = []
        h, w = img.shape[:2]
        for (cx, cy, bw, bh), c, ci in zip(out[m, :4], cf[m], cls[m]):
            x1 = max(0.0, (cx - bw / 2) / r)
            y1 = max(0.0, (cy - bh / 2) / r)
            x2 = min(float(w), (cx + bw / 2) / r)
            y2 = min(float(h), (cy + bh / 2) / r)
            if x2 > x1 and y2 > y1:
                boxes.append((x1, y1, x2, y2, float(c), int(ci)))
        boxes.sort(key=lambda b: -b[4])
        keep: list = []
        for b in boxes:
            if all(iou(b, k) < NMS_IOU for k in keep):
                keep.append(b)
        return keep


# ---------------------------------------------------------------- state db
SCHEMA = """
create table if not exists events (
  event_id integer primary key, camera text, start_time text, end_time text,
  source text, frames integer default 0, persons integer default 0, crops integer default 0,
  status text, attempts integer default 0, error text, processed_at text);
create table if not exists crops (
  id integer primary key autoincrement, event_id integer, camera text, ts text, path text unique,
  ctx_path text, x1 integer, y1 integer, x2 integer, y2 integer, conf real, height integer,
  frame_w integer, frame_h integer, source text,
  immich_id text, immich_status text default 'pending', immich_attempts integer default 0,
  uploaded_at text, created_at text default (datetime('now','localtime')));
create index if not exists crops_pending on crops(immich_status);
create index if not exists crops_event on crops(event_id);
create table if not exists clips (
  event_id integer primary key, camera text, ts text, path text, seconds real,
  immich_id text, immich_status text default 'pending', immich_attempts integer default 0,
  uploaded_at text, created_at text default (datetime('now','localtime')));
"""


def state_db() -> sqlite3.Connection:
    K.out_dir.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(K.out_dir / f"{K.name}.db", timeout=30)
    con.row_factory = sqlite3.Row
    con.executescript(SCHEMA)
    return con


def nvr_db() -> sqlite3.Connection:
    con = sqlite3.connect(f"file:{NVR_DB}?mode=ro", uri=True, timeout=30)
    con.row_factory = sqlite3.Row
    return con


# ---------------------------------------------------------------- nvr queries
def camera_names(nvr: sqlite3.Connection) -> dict[int, str]:
    return {r["id"]: r["name"] for r in nvr.execute("select id, name from cameras")}


def closed_events(nvr: sqlite3.Connection, before: str | None, limit: int) -> list[sqlite3.Row]:
    now = datetime.now()
    q = ("select id, camera_id, start_time, end_time, thumbnail_path from motion_events "
         f"where detection_label in ({','.join('?' * len(K.labels))}) "
         "and (end_time is not null or start_time < ?) ")
    args: list = [*K.labels, (now - timedelta(seconds=FORCE_CLOSE_S)).strftime("%Y-%m-%d %H:%M:%S")]
    if before:
        q += "and start_time < ? "
        args.append(before)
    q += "order by start_time desc limit ?"
    args.append(limit)
    return list(nvr.execute(q, args))


def event_by_id(nvr: sqlite3.Connection, event_id: int) -> sqlite3.Row | None:
    return nvr.execute(
        "select id, camera_id, start_time, end_time, thumbnail_path, detection_label from motion_events where id = ?",
        (event_id,),
    ).fetchone()


RIV_API = os.environ.get("RICHIRIS_API", "http://127.0.0.1:8700")


def riv_covering(nvr: sqlite3.Connection, camera_id: int, t0: datetime, t1: datetime) -> list[dict]:
    """The camera's RIV footage over [t0, t1] as one pseudo-segment whose
    file_path is the local RIV stream URL (ffmpeg reads it like a file; its
    first frame is the keyframe at ``start_time``). [] when the camera does
    not record RIV or RIV has nothing there."""
    import urllib.parse
    import urllib.request
    try:
        fmt = nvr.execute("select rec_format from cameras where id = ?", (camera_id,)).fetchone()
    except sqlite3.OperationalError:
        return []
    if not fmt or (fmt[0] or "ts") == "ts":
        return []
    q = urllib.parse.urlencode({"start": t0.isoformat(timespec="milliseconds")})
    try:
        with urllib.request.urlopen(f"{RIV_API}/api/riv/{camera_id}/run?{q}", timeout=10) as r:
            run = json.load(r)
    except Exception:
        return []
    rs, re_ = datetime.fromisoformat(run["start"]), datetime.fromisoformat(run["end"])
    if rs >= t1 or (fmt[0] == "shadow" and (rs - t0).total_seconds() > 5):
        return []
    end = min(t1 + timedelta(seconds=2), re_ if not run.get("live") else t1 + timedelta(seconds=2))
    url = (f"{RIV_API}/api/riv/{camera_id}/stream.mp4?"
           + urllib.parse.urlencode({"start": run["start"],
                                     "end": end.isoformat(timespec="milliseconds")}))
    return [{"id": f"riv{camera_id}", "file_path": url, "in_progress": 0, "duration": None,
             "start_time": rs.strftime("%Y-%m-%d %H:%M:%S.%f"),
             "end_time": end.strftime("%Y-%m-%d %H:%M:%S.%f")}]


def recordings_covering(nvr: sqlite3.Connection, camera_id: int, t0: datetime, t1: datetime) -> list:
    """Segments of this camera that overlap [t0, t1] (in-progress ones have no end_time).
    RIV footage (riv_covering) comes first when the camera records it."""
    riv = riv_covering(nvr, camera_id, t0, t1)
    if riv:
        return riv
    rows = nvr.execute(
        "select id, file_path, start_time, end_time, duration, in_progress from recordings "
        "where camera_id = ? and start_time <= ? and (end_time is null or end_time >= ?) "
        "order by start_time",
        (camera_id, t1.strftime("%Y-%m-%d %H:%M:%S.%f"), t0.strftime("%Y-%m-%d %H:%M:%S.%f")),
    ).fetchall()
    return [r for r in rows if r["file_path"] and os.path.exists(r["file_path"])]


# ---------------------------------------------------------------- frames
def keyframes_from_segment(path: str, offset: float, duration: float, work: Path) -> list[tuple[float, Path]]:
    """Decode only keyframes in [offset, offset+duration] of a .ts; returns (seconds-from-offset, jpeg)."""
    for f in work.glob("kf_*.jpg"):
        f.unlink()
    offset = max(0.0, offset)
    cmd = [FFMPEG, "-hide_banner", "-nostdin", "-loglevel", "info", "-threads", "2",
           "-analyzeduration", "20M", "-skip_frame", "nokey",
           "-ss", f"{offset:.3f}", "-i", path, "-t", f"{duration:.3f}",
           "-an", "-sn", "-dn", "-vsync", "0", "-vf", "showinfo", "-q:v", "2",
           str(work / "kf_%04d.jpg")]
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=240)
    except subprocess.TimeoutExpired:
        log.warning("ffmpeg timed out on %s", path)
        return []
    times = [float(m) for m in re.findall(r"pts_time:\s*([0-9.]+)", p.stderr)]
    files = sorted(work.glob("kf_*.jpg"))
    out = []
    for i, f in enumerate(files):
        t = times[i] if i < len(times) else (times[-1] if times else 0.0)
        out.append((t, f))
    return out


def subsample(frames: list[tuple[datetime, Path]], gap: float, cap: int) -> list[tuple[datetime, Path]]:
    picked: list[tuple[datetime, Path]] = []
    last: datetime | None = None
    for ts, f in frames:
        if last is None or (ts - last).total_seconds() >= gap:
            picked.append((ts, f))
            last = ts
    if len(picked) > cap:
        idx = np.linspace(0, len(picked) - 1, cap).round().astype(int)
        picked = [picked[i] for i in sorted(set(idx.tolist()))]
    return picked


def gather_frames(nvr: sqlite3.Connection, ev: sqlite3.Row, work: Path) -> tuple[str, list[tuple[datetime, Path]]]:
    """Return (source, [(frame_time, jpeg_path)]) for the event."""
    start = parse_ts(ev["start_time"])
    end = parse_ts(ev["end_time"]) or (start + timedelta(seconds=30))
    t0, t1 = start - timedelta(seconds=PRE_S), end + timedelta(seconds=POST_S)
    frames: list[tuple[datetime, Path]] = []
    for seg in recordings_covering(nvr, ev["camera_id"], t0, t1):
        seg_start = parse_ts(seg["start_time"])
        seg_end = parse_ts(seg["end_time"]) or datetime.now()
        a, b = max(t0, seg_start), min(t1, seg_end)
        if b <= a:
            continue
        off = (a - seg_start).total_seconds()
        sub = work / f"seg{seg['id']}"
        sub.mkdir(exist_ok=True)
        for rel, f in keyframes_from_segment(seg["file_path"], off, (b - a).total_seconds(), sub):
            frames.append((seg_start + timedelta(seconds=off + rel), f))
    frames.sort(key=lambda x: x[0])
    if frames:
        return "recording", subsample(frames, MIN_GAP_S, MAX_FRAMES)
    thumb = ev["thumbnail_path"]
    if K.thumb_fallback and thumb and os.path.exists(thumb):
        return "thumbnail", [(start, Path(thumb))]
    return "none", []


# ---------------------------------------------------------------- crops
def crop_box(b, w: int, h: int) -> tuple[int, int, int, int]:
    x1, y1, x2, y2 = b[:4]
    bw, bh = x2 - x1, y2 - y1
    X1 = int(max(0, x1 - bw * MARGIN_W))
    X2 = int(min(w, x2 + bw * MARGIN_W))
    Y1 = int(max(0, y1 - bh * MARGIN_H))
    Y2 = int(min(h, y2 + bh * MARGIN_H))
    return X1, Y1, X2, Y2


def write_exif(path: Path, when: datetime, description: str) -> None:
    try:
        import piexif
        stamp = when.strftime("%Y:%m:%d %H:%M:%S")
        off = local_offset_str(when)
        exif = {
            "0th": {piexif.ImageIFD.ImageDescription: description.encode("utf-8"),
                    piexif.ImageIFD.Make: b"RichIris", piexif.ImageIFD.Software: b"people_snapshots",
                    piexif.ImageIFD.DateTime: stamp.encode()},
            "Exif": {piexif.ExifIFD.DateTimeOriginal: stamp.encode(),
                     piexif.ExifIFD.DateTimeDigitized: stamp.encode(),
                     piexif.ExifIFD.OffsetTimeOriginal: off.encode(),
                     piexif.ExifIFD.OffsetTime: off.encode(),
                     piexif.ExifIFD.SubSecTimeOriginal: f"{when.microsecond // 1000:03d}".encode()},
        }
        piexif.insert(piexif.dump(exif), str(path))
    except Exception as e:  # EXIF is a nicety; the crop matters
        log.warning("exif failed for %s: %s", path.name, e)


def view_moved(first: Path, last: Path) -> bool:
    """True when the first and last frame differ everywhere: the PTZ lens moved, or the light changed."""
    a, b = (cv2.imread(str(p), cv2.IMREAD_GRAYSCALE) for p in (first, last))
    if a is None or b is None:
        return False
    a, b = (cv2.GaussianBlur(cv2.resize(x, (160, 90), interpolation=cv2.INTER_AREA), (5, 5), 0) for x in (a, b))
    return float(cv2.absdiff(a, b).mean()) > LENS_MOVED_DIFF


def make_clip(nvr: sqlite3.Connection, st: sqlite3.Connection, ev: sqlite3.Row, cam: str) -> bool:
    """A short 720p H.264 clip of the event from the recording that covers most of it."""
    start = parse_ts(ev["start_time"])
    end = parse_ts(ev["end_time"]) or (start + timedelta(seconds=30))
    t0 = start - timedelta(seconds=CLIP_PRE_S)
    t1 = min(end + timedelta(seconds=CLIP_POST_S), t0 + timedelta(seconds=CLIP_MAX_S))
    best = None
    for seg in recordings_covering(nvr, ev["camera_id"], t0, t1):
        seg_start = parse_ts(seg["start_time"])
        a, b = max(t0, seg_start), min(t1, parse_ts(seg["end_time"]) or datetime.now())
        if best is None or (b - a) > (best[2] - best[1]):
            best = (seg, a, b, seg_start)
    if best is None or (best[2] - best[1]).total_seconds() < 3:
        return False
    seg, a, b, seg_start = best
    out_dir = K.out_dir / start.strftime("%Y-%m-%d") / safe_name(cam) / "clips"
    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / f"{a.strftime('%H%M%S')}_e{ev['id']}.mp4"
    cmd = [FFMPEG, "-hide_banner", "-nostdin", "-loglevel", "error", "-y", "-threads", "3",
           "-analyzeduration", "20M", "-ss", f"{max(0.0, (a - seg_start).total_seconds()):.3f}",
           "-i", seg["file_path"], "-t", f"{(b - a).total_seconds():.3f}",
           "-map", "0:v:0", "-map", "0:a:0?", "-sn", "-dn",
           "-vf", f"scale=w='min({CLIP_WIDTH},iw)':h='min({CLIP_HEIGHT},ih)':force_original_aspect_ratio=decrease:force_divisible_by=2", "-c:v", "libx264", "-preset", "veryfast",
           "-crf", str(CLIP_CRF), "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "48k", "-ac", "1",
           "-movflags", "+faststart",
           "-metadata", f"creation_time={a.astimezone().isoformat()}",
           "-metadata", f"comment={cam} {a:%a %d %b %Y %H:%M:%S}, RichIris event {ev['id']}",
           str(out)]
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
    except subprocess.TimeoutExpired:
        log.warning("clip timed out for event %s", ev["id"])
        out.unlink(missing_ok=True)
        return False
    if p.returncode != 0 or not out.exists() or out.stat().st_size < 20_000:
        log.warning("clip failed for event %s: %s", ev["id"], p.stderr.strip()[-200:])
        out.unlink(missing_ok=True)
        return False
    with st:
        st.execute("insert or replace into clips(event_id,camera,ts,path,seconds) values (?,?,?,?,?)",
                   (ev["id"], cam, a.strftime("%Y-%m-%d %H:%M:%S.%f"), str(out), round((b - a).total_seconds(), 1)))
    return True


def process_event(nvr: sqlite3.Connection, st: sqlite3.Connection, det: PersonDetector,
                  ev: sqlite3.Row, cams: dict[int, str], work: Path) -> dict:
    cam = cams.get(ev["camera_id"], f"camera{ev['camera_id']}")
    start = parse_ts(ev["start_time"])
    source, frames = gather_frames(nvr, ev, work)
    day_dir = K.out_dir / start.strftime("%Y-%m-%d") / safe_name(cam)
    ctx_dir = day_dir / "ctx"
    # Pass 1: detect on every sampled frame.
    per_frame: dict[int, list] = {}
    n_persons = 0
    for k, (ts, f) in enumerate(frames):
        img = cv2.imread(str(f))
        if img is None:
            continue
        h = img.shape[0]
        min_h = K.min_h if h >= 1000 else K.min_h_small
        boxes = [b for b in det.detect(img, CONF, K.classes) if (b[3] - b[1]) >= min_h]
        if not boxes:
            continue
        n_persons += len(boxes)
        per_frame[k] = boxes
    # Vehicles: skip what is parked, and skip the event when the whole view moved.
    static: list = []
    if K.drop_static and len(per_frame) >= 2:
        k0, k1 = min(per_frame), max(per_frame)
        if (frames[k1][0] - frames[k0][0]).total_seconds() >= STATIC_MIN_SPAN_S:
            if view_moved(frames[k0][1], frames[k1][1]):
                return {"camera": cam, "source": "viewmoved", "frames": len(frames), "persons": n_persons, "crops": 0}
            # Parked = in the same place in the first and the last frame, or there in at
            # least half the frames (a passing car hides a parked one for a frame or two).
            need = max(3, (len(per_frame) + 1) // 2)
            for k in per_frame:
                for a in per_frame[k]:
                    if any(iou(a, sb) >= STATIC_IOU for sb in static):
                        continue
                    seen = sum(1 for j in per_frame if any(iou(a, o) >= STATIC_IOU for o in per_frame[j]))
                    ends = all(any(iou(a, o) >= STATIC_IOU for o in per_frame[j]) for j in (k0, k1))
                    if ends or seen >= need:
                        static.append(a)
    # Drop subjects that have not moved since the last kept box.
    kept: list[tuple] = []
    candidates: list[tuple[int, tuple]] = []   # (frame index, box)
    for k in sorted(per_frame):
        for b in per_frame[k]:
            if any(iou(b, sb) >= STATIC_NEAR_IOU for sb in static):
                continue
            if all(iou(b, kb) < DUP_IOU for kb in kept):
                kept.append(b[:4])
                candidates.append((k, b))
    # Pass 2: keep the closest (tallest) MAX_CROPS, written in time order.
    if len(candidates) > MAX_CROPS:
        candidates = sorted(candidates, key=lambda c: -(c[1][3] - c[1][1]))[:MAX_CROPS]
        candidates.sort(key=lambda c: (c[0], -c[1][4]))
    rows = []
    ctx_written: dict[int, Path] = {}
    for k, b in candidates:
        ts, f = frames[k]
        img = cv2.imread(str(f))
        if img is None:
            continue
        h, w = img.shape[:2]
        day_dir.mkdir(parents=True, exist_ok=True)
        ctx_dir.mkdir(exist_ok=True)
        stem = f"{ts.strftime('%H%M%S')}_e{ev['id']}"
        if k not in ctx_written:
            ctx_path = ctx_dir / f"{stem}_f{k}.jpg"
            ctx = img.copy()
            for bb in per_frame.get(k, []):
                cv2.rectangle(ctx, (int(bb[0]), int(bb[1])), (int(bb[2]), int(bb[3])), (0, 200, 255), max(2, w // 960))
            scale = CTX_WIDTH / w
            ctx = cv2.resize(ctx, (CTX_WIDTH, int(h * scale)), interpolation=cv2.INTER_AREA)
            label = f"{cam}  {ts:%Y-%m-%d %H:%M:%S}  event {ev['id']}"
            y = ctx.shape[0] - 14   # bottom-left, clear of the camera's burnt-in clock
            cv2.putText(ctx, label, (10, y), cv2.FONT_HERSHEY_SIMPLEX, 0.8, (0, 0, 0), 4)
            cv2.putText(ctx, label, (10, y), cv2.FONT_HERSHEY_SIMPLEX, 0.8, (255, 255, 255), 2)
            cv2.imwrite(str(ctx_path), ctx, [cv2.IMWRITE_JPEG_QUALITY, JPEG_CTX_Q])
            ctx_written[k] = ctx_path
        X1, Y1, X2, Y2 = crop_box(b, w, h)
        n = len(rows) + 1
        cpath = day_dir / f"{stem}_{n:02d}.jpg"
        cv2.imwrite(str(cpath), img[Y1:Y2, X1:X2], [cv2.IMWRITE_JPEG_QUALITY, JPEG_CROP_Q])
        height = int(b[3] - b[1])
        desc = (f"{cam} {ts:%a %d %b %Y %H:%M:%S}. {COCO.get(b[5], K.noun).capitalize()} {height} px tall, confidence {b[4]:.2f}, "
                f"RichIris event {ev['id']} ({source}).")
        write_exif(cpath, ts, desc)
        rows.append((ev["id"], cam, ts.strftime("%Y-%m-%d %H:%M:%S.%f"), str(cpath), str(ctx_written[k]),
                     int(b[0]), int(b[1]), int(b[2]), int(b[3]), round(b[4], 3), height, w, h, source))
    with st:
        for r in rows:
            st.execute("insert or ignore into crops(event_id,camera,ts,path,ctx_path,x1,y1,x2,y2,conf,height,"
                       "frame_w,frame_h,source) values (?,?,?,?,?,?,?,?,?,?,?,?,?,?)", r)
    clip = False
    if rows and source == "recording" and (ev["start_time"] >= CLIPS_FROM or os.environ.get("PEOPLE_FORCE_CLIP") == "1"):
        clip = make_clip(nvr, st, ev, cam)
    return {"camera": cam, "source": source, "frames": len(frames), "persons": n_persons, "crops": len(rows),
            "clip": clip}


def mark_event(st: sqlite3.Connection, ev: sqlite3.Row, cam: str, result: dict | None, error: str | None) -> None:
    prev = st.execute("select attempts from events where event_id = ?", (ev["id"],)).fetchone()
    attempts = (prev["attempts"] if prev else 0) + 1
    if error and attempts < RETRY_MAX:
        status = "retry"
    elif error:
        status = "failed"
    else:
        status = "done" if result["source"] != "none" else "nosource"
    with st:
        st.execute(
            "insert or replace into events(event_id,camera,start_time,end_time,source,frames,persons,crops,"
            "status,attempts,error,processed_at) values (?,?,?,?,?,?,?,?,?,?,?,?)",
            (ev["id"], cam, ev["start_time"], ev["end_time"], (result or {}).get("source"),
             (result or {}).get("frames", 0), (result or {}).get("persons", 0), (result or {}).get("crops", 0),
             status, attempts, error, datetime.now().strftime("%Y-%m-%d %H:%M:%S")),
        )


# ---------------------------------------------------------------- immich
class Immich:
    def __init__(self):
        self.ok = False
        if os.environ.get("PEOPLE_NO_UPLOAD") == "1" or not os.path.exists(IMMICH_ENV):
            log.info("immich upload disabled (%s)", "PEOPLE_NO_UPLOAD" if os.environ.get("PEOPLE_NO_UPLOAD") else "no env file")
            return
        cfg: dict[str, str] = {}
        for line in Path(IMMICH_ENV).read_text().splitlines():
            if "=" in line and not line.startswith("#"):
                k, v = line.split("=", 1)
                cfg[k.strip()] = v.strip()
        import requests
        self.rq = requests
        self.url = cfg["IMMICH_URL"].rstrip("/")
        self.h = {"x-api-key": cfg["IMMICH_API_KEY"], "Accept": "application/json"}
        self.albums: dict[str, str] = {k.name: cfg[k.album_key] for k in KINDS.values() if cfg.get(k.album_key)}
        self.ok = True

    def _album(self, cam: str) -> str | None:
        """The one album of the current kind (all cameras). Created on first use if the env has no id."""
        if K.name in self.albums:
            return self.albums[K.name]
        try:
            r = self.rq.post(f"{self.url}/api/albums", headers=self.h, timeout=30,
                             json={"albumName": K.album_name,
                                   "description": f"Every {K.noun.lower()} RichIris detected, all cameras."})
            r.raise_for_status()
            self.albums[K.name] = r.json()["id"]
            with open(IMMICH_ENV, "a") as fh:
                fh.write(f"{K.album_key}={self.albums[K.name]}" + chr(10))
            return self.albums[K.name]
        except Exception as e:
            log.warning("album create failed: %s", e)
            return None

    def upload_clips(self, st: sqlite3.Connection, limit: int = CLIP_UPLOAD_BATCH) -> int:
        """Upload finished clips (archived, into the kind's album) and delete the local file."""
        if not self.ok:
            return 0
        rows = st.execute("select * from clips where immich_status in ('pending','retry') and immich_attempts < 6 "
                          "order by event_id desc limit ?", (limit,)).fetchall()
        ids: list[str] = []
        for r in rows:
            p = Path(r["path"])
            if not p.exists():
                with st:
                    st.execute("update clips set immich_status='missing' where event_id=?", (r["event_id"],))
                continue
            ts = parse_ts(r["ts"])
            iso = ts.strftime("%Y-%m-%dT%H:%M:%S.") + f"{ts.microsecond // 1000:03d}" + local_offset_str(ts)
            data = {"deviceAssetId": f"richiris-{K.name}-clip-{r['event_id']}", "deviceId": f"richiris-{K.name}",
                    "fileCreatedAt": iso, "fileModifiedAt": iso, "isFavorite": "false", "visibility": "archive"}
            try:
                with open(p, "rb") as fh:
                    resp = self.rq.post(f"{self.url}/api/assets", headers=self.h, data=data,
                                        files={"assetData": (f"{safe_name(r['camera'])} {p.name}", fh, "video/mp4")},
                                        timeout=180)
                if resp.status_code not in (200, 201):
                    raise RuntimeError(f"{resp.status_code} {resp.text[:120]}")
                aid = resp.json()["id"]
                with st:
                    st.execute("update clips set immich_id=?, immich_status='uploaded', "
                               "uploaded_at=datetime('now','localtime'), immich_attempts=immich_attempts+1 "
                               "where event_id=?", (aid, r["event_id"]))
                ids.append(aid)
                p.unlink(missing_ok=True)
            except Exception as e:
                log.warning("upload failed clip %s: %s", r["event_id"], e)
                with st:
                    st.execute("update clips set immich_status='retry', immich_attempts=immich_attempts+1 "
                               "where event_id=?", (r["event_id"],))
                if "Connection" in str(e) or "timed out" in str(e):
                    break
        alb = self._album("") if ids else None
        if alb:
            try:
                r3 = self.rq.put(f"{self.url}/api/albums/{alb}/assets", headers=self.h, timeout=60, json={"ids": ids})
                if r3.status_code >= 300:
                    log.warning("album add failed: %s %s", r3.status_code, r3.text[:120])
            except Exception as e:
                log.warning("album add failed: %s", e)
        if ids:
            log.info("immich: uploaded %d %s clips", len(ids), K.name)
        return len(ids)

    def upload_pending(self, st: sqlite3.Connection, limit: int = UPLOAD_BATCH) -> int:
        if not self.ok:
            return 0
        rows = st.execute("select * from crops where immich_status in ('pending','retry') and immich_attempts < 6 "
                          "order by id desc limit ?", (limit,)).fetchall()
        if not rows:
            return 0
        done = 0
        by_album: dict[str, list[str]] = {}
        new_ids: list[str] = []
        for r in rows:
            p = Path(r["path"])
            if not p.exists():
                with st:
                    st.execute("update crops set immich_status='missing' where id=?", (r["id"],))
                continue
            ts = parse_ts(r["ts"])
            iso = ts.strftime("%Y-%m-%dT%H:%M:%S.") + f"{ts.microsecond // 1000:03d}" + local_offset_str(ts)
            # visibility=archive ON the upload: the asset is never in the timeline state, so a phone
            # syncing in the seconds before the bulk archive PUT below cannot pick it up (2026-11-10).
            data = {"deviceAssetId": f"richiris-{K.name}-{r['id']}", "deviceId": f"richiris-{K.name}",
                    "fileCreatedAt": iso, "fileModifiedAt": iso, "isFavorite": "false",
                    "visibility": "archive"}
            try:
                with open(p, "rb") as fh:
                    resp = self.rq.post(f"{self.url}/api/assets", headers=self.h, data=data,
                                        files={"assetData": (p.name, fh, "image/jpeg")}, timeout=60)
                if resp.status_code not in (200, 201):
                    raise RuntimeError(f"{resp.status_code} {resp.text[:120]}")
                aid = resp.json()["id"]
                with st:
                    st.execute("update crops set immich_id=?, immich_status='uploaded', uploaded_at=datetime('now','localtime'), "
                               "immich_attempts=immich_attempts+1 where id=?", (aid, r["id"]))
                new_ids.append(aid)
                alb = self._album(r["camera"])
                if alb:
                    by_album.setdefault(alb, []).append(aid)
                done += 1
            except Exception as e:
                log.warning("upload failed crop %s: %s", r["id"], e)
                with st:
                    st.execute("update crops set immich_status='retry', immich_attempts=immich_attempts+1 where id=?", (r["id"],))
                if "Connection" in str(e) or "timed out" in str(e):
                    break
        if new_ids:
            try:
                r2 = self.rq.put(f"{self.url}/api/assets", headers=self.h, timeout=60,
                                 json={"ids": new_ids, "visibility": "archive"})
                if r2.status_code >= 300:
                    log.warning("archive flag failed: %s %s", r2.status_code, r2.text[:120])
            except Exception as e:
                log.warning("archive flag failed: %s", e)
        for alb, ids in by_album.items():
            try:
                r3 = self.rq.put(f"{self.url}/api/albums/{alb}/assets", headers=self.h, timeout=60, json={"ids": ids})
                if r3.status_code >= 300:
                    log.warning("album add failed: %s %s", r3.status_code, r3.text[:120])
            except Exception as e:
                log.warning("album add failed: %s", e)
        if done:
            log.info("immich: uploaded %d %s crops", done, K.name)
        return done


# ---------------------------------------------------------------- cycle
def pick_events(nvr: sqlite3.Connection, st: sqlite3.Connection, limit: int, force: bool) -> list[sqlite3.Row]:
    """Newest closed events of the current kind that have not been processed yet (or need a retry)."""
    done = {r["event_id"] for r in st.execute("select event_id from events where status in ('done','nosource','failed')")}
    if force:
        done = set()
    picked: list[sqlite3.Row] = []
    before: str | None = None
    settle = datetime.now() - timedelta(seconds=POST_S + EVENT_SETTLE_S)
    while len(picked) < limit:
        page = closed_events(nvr, before, 400)
        if not page:
            break
        for ev in page:
            if ev["id"] in done:
                continue
            end = parse_ts(ev["end_time"]) or (parse_ts(ev["start_time"]) + timedelta(seconds=30))
            if end > settle:
                continue
            picked.append(ev)
            if len(picked) >= limit:
                break
        before = page[-1]["start_time"]
        if len(page) < 400:
            break
    return picked


def run_cycle(det: PersonDetector, immich: Immich, limit: int, force: bool, event_ids: list[int] | None = None,
              kind: str = "people") -> int:
    global K
    K = KINDS[kind]
    nvr = nvr_db()
    st = state_db()
    cams = camera_names(nvr)
    if event_ids:
        events = [e for e in (event_by_id(nvr, i) for i in event_ids)
                  if e is not None and e["detection_label"] in K.labels]
    else:
        events = pick_events(nvr, st, limit, force)
    work = Path(tempfile.mkdtemp(prefix="people-", dir="/tmp"))
    try:
        for ev in events:
            cam = cams.get(ev["camera_id"], "?")
            t0 = time.time()
            try:
                res = process_event(nvr, st, det, ev, cams, work)
                mark_event(st, ev, cam, res, None)
                log.info("%s event %s %s %s src=%s frames=%d found=%d crops=%d clip=%s %.1fs",
                         K.name, ev["id"], cam, ev["start_time"][:19], res["source"], res["frames"], res["persons"],
                         res["crops"], "y" if res.get("clip") else "n", time.time() - t0)
            except Exception as e:
                log.exception("event %s failed", ev["id"])
                mark_event(st, ev, cam, None, str(e)[:300])
            for sub in work.iterdir():
                shutil.rmtree(sub, ignore_errors=True)
            immich.upload_pending(st)
            immich.upload_clips(st)
        immich.upload_pending(st)
        immich.upload_clips(st)
    finally:
        shutil.rmtree(work, ignore_errors=True)
        nvr.close()
        st.close()
    return len(events)


def stats(kinds: list[str]) -> None:
    global K
    nvr = nvr_db()
    for name in kinds:
        K = KINDS[name]
        st = state_db()
        print(f"== {name}")
        for row in st.execute("select status, count(*) n, sum(crops) crops from events group by status"):
            print(f"events {row['status']:9s} {row['n']:6d}  crops {row['crops'] or 0}")
        for row in st.execute("select immich_status, count(*) n from crops group by immich_status"):
            print(f"crops  {row['immich_status']:9s} {row['n']:6d}")
        for row in st.execute("select immich_status, count(*) n, round(sum(seconds)/60) m from clips group by immich_status"):
            print(f"clips  {row['immich_status']:9s} {row['n']:6d}  {row['m'] or 0:.0f} min")
        for row in st.execute("select camera, count(*) n from crops group by camera order by n desc"):
            print(f"  {row['camera']:16s} {row['n']}")
        total = nvr.execute(f"select count(*) from motion_events where detection_label in ({','.join('?' * len(K.labels))})",
                            K.labels).fetchone()[0]
        done = st.execute("select count(*) from events").fetchone()[0]
        print(f"{name} events in NVR: {total}, processed: {done}, remaining: {total - done}")


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--daemon", action="store_true")
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--event", type=int, nargs="*")
    ap.add_argument("--stats", action="store_true")
    ap.add_argument("--limit", type=int, default=EVENTS_PER_CYCLE)
    ap.add_argument("--kind", choices=sorted(KINDS))
    a = ap.parse_args(argv)
    kinds = [a.kind] if a.kind else list(KINDS)
    if a.stats:
        stats(kinds)
        return 0
    setup_logging(to_stdout=not a.daemon or sys.stdout.isatty())
    force = os.environ.get("PEOPLE_FORCE") == "1"
    try:
        if os.nice(0) < 10:
            os.nice(10 - os.nice(0))
    except OSError:
        pass
    det = PersonDetector(MODEL, THREADS)
    immich = Immich()
    if a.event:
        for kind in kinds:
            run_cycle(det, immich, a.limit, True, a.event, kind)
        return 0
    if a.once:
        n = sum(run_cycle(det, immich, a.limit, force, None, kind) for kind in kinds)
        log.info("cycle done, %d events", n)
        return 0
    log.info("daemon start")
    while True:
        try:
            n = sum(run_cycle(det, immich, a.limit, force, None, kind) for kind in kinds)
        except Exception:
            log.exception("cycle failed")
            n = 0
            time.sleep(30)
        time.sleep(1.0 if n else 20.0)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
