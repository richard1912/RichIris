"""AI guard: a vision model checks that a "person" detection really is a person.

The detector (YOLO) sometimes calls an insect on the lens, a spider web, rain or a
shadow a person, and every such event used to ring the phone as an Intruder Alert.
For each person event this shows a Claude model the frame the detector fired on
(with its box drawn), the main-stream picture and a zoomed crop, and asks one
question: is there actually a human? The answer is stored in `event_assessments`
(shown in the app's Events pane) and the alert scripts ask for it before they
notify (`GET /api/events/human`): a "no human" verdict suppresses the phone alarm
and the email. The lights are not gated, they fire at once as before.

**Fail open**: if the model is slow, errors or is switched off, the answer is
"unknown" and the alert goes out. Only a positive "no human" suppresses anything.

The model runs through the Claude Code CLI on the subscription token (the same
arrangement as Do-My-CTL), in one turn with no tools: images go in as content
blocks on stdin (`--input-format stream-json`), the answer comes back against a
JSON schema. Native `claude` is used when it is on PATH, otherwise a throwaway
container from the image that already has the CLI.

(2026-11-19 to 20 this was a "how suspicious is this" judge in shadow mode. On
the 7 November brick attack it rated the group "low", and Richard cut it back to
this one job. The suspicion columns in the table are unused now.)
"""

import asyncio
import base64
import json
import logging
import os
import shutil
import time
import uuid
from datetime import datetime, timedelta
from pathlib import Path

import cv2
import numpy as np

from app.config import get_bootstrap, get_config
from app.database import get_session_factory
from app.models import EventAssessment
from app.services.live_cache import get_live_cache
from app.services.settings import load_settings_dict

logger = logging.getLogger(__name__)

ENV_FILE = os.environ.get("RICHIRIS_GUARD_ENV_FILE", "/opt/richiris/secrets/guard.env")
DOCKER_IMAGE = os.environ.get("RICHIRIS_GUARD_IMAGE", "lemmy-ai-lemmy-ai:latest")

WIDE_FRAMES = 3            # backfill only: keyframes cut from the recording
LIVE_WIDE_FRAMES = 1       # live: the newest keyframe only, the alert is waiting on the answer
NEXT_KEYFRAME_WAIT = 4.0   # how long to wait for each further keyframe
WIDE_WIDTH = 1568          # the model's useful limit on the long edge
CROP_MAX = 768
CROP_PAD = 0.6             # of the box size, added on every side
JPEG_QUALITY = 82
DECODE_TIMEOUT = 8.0
MODEL_TIMEOUT = 90.0
MAX_CONCURRENT = 2
MAX_PENDING = 8            # beyond this a new event is skipped, not queued for minutes
KEEP_DAYS = 30

SUSPICION_LABELS = ["none", "low", "medium", "high"]

SYSTEM_PROMPT = """You check security camera detections for a home alarm. An object detector has \
flagged a person. Your only job is to say whether a real human being is actually visible in the \
pictures, or whether the detector was fooled. You are careful: a missed real person is far worse \
than a false alarm, so when you cannot tell, you say a human is present."""

QUESTION = """Is a real human actually visible?

Answer human_present = false ONLY when you can see what the detector mistook for a person and it is \
clearly not one: an insect, moth or spider (or its web) close to the lens, rain, glare or a lens flare, \
a shadow, a plant, a post, a bin, a statue, an animal, a reflection, or an empty scene with nothing \
where the box is.

Answer human_present = true when any person or part of a person is visible anywhere in the pictures \
(even small, distant, blurred, partly hidden or walking out of frame), or when it is too dark or \
blurred to rule a person out. People inside vehicles count. A person on the street counts too: \
whether they matter is decided elsewhere."""

SCHEMA = {
    "type": "object",
    "properties": {
        "human_present": {"type": "boolean"},
        "people_count": {"type": "integer", "minimum": 0},
        "description": {"type": "string",
                        "description": "One plain sentence: what is in the detected box / what the person is doing."},
        "reason": {"type": "string",
                   "description": "One short sentence: why this is or is not a real person."},
    },
    "required": ["human_present", "people_count", "description", "reason"],
    "additionalProperties": False,
}


def _encode(img: np.ndarray) -> bytes:
    ok, buf = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, JPEG_QUALITY])
    return buf.tobytes() if ok else b""


def _fit(img: np.ndarray, long_edge: int) -> np.ndarray:
    h, w = img.shape[:2]
    scale = long_edge / max(h, w)
    if scale >= 1:
        return img
    return cv2.resize(img, (max(int(w * scale), 2), max(int(h * scale), 2)), interpolation=cv2.INTER_AREA)


def _crop(img: np.ndarray, box: tuple[float, float, float, float]) -> np.ndarray | None:
    """Padded crop around a normalised (x1, y1, x2, y2) box, or None if it is degenerate."""
    h, w = img.shape[:2]
    x1, y1, x2, y2 = box
    bw, bh = (x2 - x1) * w, (y2 - y1) * h
    if bw < 4 or bh < 4:
        return None
    px, py = bw * CROP_PAD, bh * CROP_PAD
    ax, ay = int(max(x1 * w - px, 0)), int(max(y1 * h - py, 0))
    bx, by = int(min(x2 * w + px, w)), int(min(y2 * h + py, h))
    if bx - ax < 16 or by - ay < 16:
        return None
    return _fit(img[ay:by, ax:bx], CROP_MAX)


class Guard:
    def __init__(self) -> None:
        self._sem = asyncio.Semaphore(MAX_CONCURRENT)
        self._pending = 0
        self._last: dict[int, float] = {}
        self._tasks: set[asyncio.Task] = set()
        self._claude = shutil.which("claude")
        self._last_prune = 0.0
        # Person events whose verdict is still being worked out: event id -> done flag.
        self._waiting: dict[int, asyncio.Event] = {}
        self.backfill_state: dict = {"running": False}

    # ------------------------------------------------------------------ entry
    def submit(self, event_id: int, cam_id: int, cam_name: str, event_time: datetime,
               bbox: tuple[int, int, int, int] | None, frame_hw: tuple[int, int] | None,
               frame: np.ndarray | None = None) -> None:
        """Called by the motion detector when a person event starts. Never blocks, never raises.

        `frame` is the sub-stream frame the detector fired on; it is shown to the model with
        the detected box drawn on it.
        """
        try:
            self._waiting[event_id] = asyncio.Event()
            detection = _mark_detection(frame, bbox)
            task = asyncio.get_running_loop().create_task(
                self._run(event_id, cam_id, cam_name, event_time, _normalise(bbox, frame_hw), detection))
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)
        except Exception:
            self._finish(event_id)
            logger.warning("Guard: could not queue event", exc_info=True, extra={"event_id": event_id})

    def _finish(self, event_id: int) -> None:
        done = self._waiting.pop(event_id, None)
        if done is not None:
            done.set()

    async def _run(self, event_id: int, cam_id: int, cam_name: str, event_time: datetime,
                   box: tuple[float, float, float, float] | None, detection: bytes = b"") -> None:
        try:
            cfg = await self._settings()
            if not cfg["enabled"]:
                return
            if cfg["cameras"] and cam_name not in cfg["cameras"]:
                return
            now = time.monotonic()
            if now - self._last.get(cam_id, 0.0) < cfg["cooldown"]:
                return
            if self._pending >= MAX_PENDING:
                logger.warning("Guard: busy, event skipped", extra={"event_id": event_id, "camera_name": cam_name})
                return
            self._last[cam_id] = now
            self._pending += 1
            try:
                async with self._sem:
                    await self._assess_and_store(event_id, cam_id, cam_name, event_time, box, cfg,
                                                 detection=detection)
            finally:
                self._pending -= 1
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.warning("Guard: assessment crashed", exc_info=True, extra={"event_id": event_id})
        finally:
            self._finish(event_id)

    async def human_on_camera(self, cam_id: int, window: float, wait: float) -> dict:
        """Did the model confirm a human on this camera in the last `window` seconds?

        Waits up to `wait` seconds for verdicts still in progress. `human` is True if any recent
        person event was confirmed, False if every one of them was judged a false detection, and
        None (unknown: callers must treat it as "yes") if there is nothing to go on.
        """
        from sqlalchemy import select
        from app.models import MotionEvent
        since = datetime.now() - timedelta(seconds=window)
        async with get_session_factory()() as session:
            ids = [r[0] for r in (await session.execute(
                select(MotionEvent.id).where(MotionEvent.camera_id == cam_id,
                                             MotionEvent.detection_label == "person",
                                             MotionEvent.start_time >= since))).all()]
        if not ids:
            return {"human": None, "events": 0, "detail": "no recent person event"}
        pending = [self._waiting[i].wait() for i in ids if i in self._waiting]
        if pending:
            try:
                await asyncio.wait_for(asyncio.gather(*pending), wait)
            except asyncio.TimeoutError:
                pass
        async with get_session_factory()() as session:
            rows = (await session.execute(
                select(EventAssessment).where(EventAssessment.motion_event_id.in_(ids)))).scalars().all()
        ok = [r for r in rows if r.status == "ok"]
        confirmed = [r for r in ok if r.would_alert]
        if confirmed:
            return {"human": True, "events": len(ids), "detail": confirmed[-1].description}
        if ok and len(ok) == len(ids):
            return {"human": False, "events": len(ids), "detail": ok[-1].description}
        return {"human": None, "events": len(ids), "detail": "verdict not available"}

    async def _settings(self) -> dict:
        async with get_session_factory()() as session:
            s = await load_settings_dict(session)
        return {
            "enabled": s.get("guard.enabled", "false").lower() == "true",
            "model": s.get("guard.model") or "claude-sonnet-5-5",
            "cameras": {c.strip() for c in s.get("guard.cameras", "").split(",") if c.strip()},
            "cooldown": float(s.get("guard.cooldown_seconds") or 0),
        }

    # ----------------------------------------------------------------- frames
    async def collect_frames(self, cam_name: str, box: tuple[float, float, float, float] | None,
                             wide_frames: int = LIVE_WIDE_FRAMES) -> list[tuple[str, bytes]]:
        """[(kind, jpeg)]: `wide_frames` main-stream shots one GOP apart, then a crop from the first."""
        from app.services.go2rtc_client import get_stream_name
        stream = f"{get_stream_name(cam_name)}_s1_direct"
        cache = get_live_cache()
        out: list[tuple[str, bytes]] = []
        crop_jpeg = b""
        seen_key = None
        for n in range(wide_frames):
            deadline = time.monotonic() + (0 if n == 0 else NEXT_KEYFRAME_WAIT)
            snap = cache.newest_keyframe(stream)
            while (snap is None or snap[2] == seen_key) and time.monotonic() < deadline:
                await asyncio.sleep(0.2)
                snap = cache.newest_keyframe(stream)
            if snap is None or snap[2] == seen_key:
                break
            seen_key = snap[2]
            img = await self._decode(snap[0] + snap[1])
            if img is None:
                continue
            if n == 0 and box is not None:
                crop = _crop(img, box)
                if crop is not None:
                    crop_jpeg = _encode(crop)
            out.append(("wide", _encode(_fit(img, WIDE_WIDTH))))
        if crop_jpeg:
            out.append(("crop", crop_jpeg))
        return [(k, j) for k, j in out if j]

    async def _decode(self, data: bytes) -> np.ndarray | None:
        """One keyframe fragment -> full-resolution BGR image (software decode: one frame)."""
        cmd = [get_config().ffmpeg.path, "-v", "error", "-nostdin", "-f", "mp4", "-i", "pipe:0",
               "-frames:v", "1", "-vf", "format=yuvj420p", "-q:v", "2", "-f", "mjpeg", "pipe:1"]
        proc = await asyncio.create_subprocess_exec(
            *cmd, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE)
        try:
            out, err = await asyncio.wait_for(proc.communicate(data), DECODE_TIMEOUT)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            return None
        if proc.returncode != 0 or not out:
            logger.warning("Guard: frame decode failed", extra={
                "rc": proc.returncode, "stderr": err.decode("utf-8", "replace")[-300:]})
            return None
        return await asyncio.to_thread(cv2.imdecode, np.frombuffer(out, np.uint8), cv2.IMREAD_COLOR)

    async def recorded_frames(self, cam_id: int, when: datetime) -> list[tuple[str, bytes]]:
        """Wide frames for a past event, cut from the recording (no crop: the box was not kept).

        The detection stream runs about 3 s behind the recording, so the window starts 4 s
        before the event's start time. Keyframes only: one per GOP, like the live path.
        """
        from app.services.footage import footage_input
        t0 = when - timedelta(seconds=4)
        src = await footage_input(cam_id, t0, when)
        if src is None:
            return []  # no recording, or a gap over this event
        cmd = [get_config().ffmpeg.path, "-v", "error", "-nostdin",
               "-skip_frame", "nokey", *src.args,
               "-frames:v", str(WIDE_FRAMES), "-fps_mode", "passthrough",
               "-vf", f"scale={WIDE_WIDTH}:-2,format=yuvj420p", "-q:v", "4",
               "-f", "image2pipe", "-c:v", "mjpeg", "pipe:1"]
        proc = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        try:
            out, err = await asyncio.wait_for(proc.communicate(), 40)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            return []
        soi = bytes([0xFF, 0xD8, 0xFF])
        jpegs = [soi + part for part in out.split(soi) if part]
        if not jpegs:
            logger.warning("Guard: no frames from recording", extra={
                "file": src.source, "stderr": err.decode("utf-8", "replace")[-200:]})
        return [("wide", j) for j in jpegs[:WIDE_FRAMES]]

    async def backfill(self, day: datetime, cameras: set[str] | None = None,
                       end: datetime | None = None) -> None:
        """Assess every person event from `day` to `end` (default: 24 h) that has no verdict yet,
        from the recordings."""
        end = end or day + timedelta(days=1)
        from sqlalchemy import select
        from app.models import Camera, MotionEvent
        cfg = await self._settings()
        wanted = cameras or cfg["cameras"]
        async with get_session_factory()() as session:
            rows = (await session.execute(
                select(MotionEvent.id, MotionEvent.camera_id, Camera.name, MotionEvent.start_time)
                .join(Camera, Camera.id == MotionEvent.camera_id)
                .outerjoin(EventAssessment, EventAssessment.motion_event_id == MotionEvent.id)
                .where(MotionEvent.detection_label == "person", EventAssessment.id.is_(None),
                       MotionEvent.start_time >= day, MotionEvent.start_time < end)
                .order_by(MotionEvent.start_time.desc()))).all()
        rows = [r for r in rows if not wanted or r[2] in wanted]
        self.backfill_state = {"day": f"{day:%Y-%m-%d %H:%M} to {end:%Y-%m-%d %H:%M}", "total": len(rows), "done": 0, "running": True}
        logger.info("Guard backfill started", extra=dict(self.backfill_state))

        async def one(event_id: int, cam_id: int, cam_name: str, when: datetime) -> None:
            try:
                async with self._sem:
                    await self._assess_and_store(event_id, cam_id, cam_name, when, None, cfg, recorded=True)
            except Exception:
                logger.warning("Guard backfill: event failed", exc_info=True, extra={"event_id": event_id})
            self.backfill_state["done"] += 1

        try:
            for i in range(0, len(rows), MAX_CONCURRENT):
                await asyncio.gather(*(one(*r) for r in rows[i:i + MAX_CONCURRENT]))
        finally:
            self.backfill_state["running"] = False
            logger.info("Guard backfill finished", extra=dict(self.backfill_state))

    # ------------------------------------------------------------------ model
    def _prompt(self, cam_name: str, when: datetime, frames: list[tuple[str, bytes]]) -> str:
        parts = []
        if any(k == "detection" for k, _ in frames):
            parts.append("the low-resolution frame the detector fired on, with its box drawn in red")
        wide = sum(1 for k, _ in frames if k == "wide")
        if wide == 1:
            parts.append("the full-resolution camera picture from about the same moment")
        elif wide > 1:
            parts.append(f"{wide} camera pictures in time order, about two seconds apart")
        if any(k == "crop" for k, _ in frames):
            parts.append("a zoomed crop of the boxed area from the full-resolution picture")
        return (
            f"Camera \"{cam_name}\", {when:%A %d %B %Y, %H:%M:%S} local time. The detector reported a person.\n"
            f"Images, in order: {'; '.join(parts)}.\n\n{QUESTION}"
        )

    async def ask(self, model: str, prompt: str, frames: list[tuple[str, bytes]]) -> dict:
        """Run the model once. Returns the parsed verdict; raises RuntimeError with a short reason."""
        content = [{"type": "image", "source": {"type": "base64", "media_type": "image/jpeg",
                                                "data": base64.b64encode(j).decode()}} for _, j in frames]
        content.append({"type": "text", "text": prompt})
        stdin = (json.dumps({"type": "user", "message": {"role": "user", "content": content}}) + "\n").encode()
        args = ["-p", "--input-format", "stream-json", "--output-format", "stream-json", "--verbose",
                "--model", model, "--tools", "", "--system-prompt", SYSTEM_PROMPT,
                "--no-session-persistence", "--json-schema", json.dumps(SCHEMA)]
        name = None
        env = None
        if self._claude:
            cmd = [self._claude, *args]
            env = {**os.environ, **_read_env(ENV_FILE)}
        else:
            name = f"richiris-guard-{uuid.uuid4().hex[:10]}"
            cmd = ["docker", "run", "--rm", "-i", "--name", name, "--network", "bridge",
                   "--env-file", ENV_FILE, "-e", "IS_SANDBOX=1", "--entrypoint", "claude",
                   DOCKER_IMAGE, *args]
        proc = await asyncio.create_subprocess_exec(
            *cmd, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE, env=env)
        try:
            out, err = await asyncio.wait_for(proc.communicate(stdin), MODEL_TIMEOUT)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            if name:  # killing the docker client does not stop the container
                killer = await asyncio.create_subprocess_exec(
                    "docker", "kill", name, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
                await killer.wait()
            raise RuntimeError(f"model timed out after {int(MODEL_TIMEOUT)} s")
        return _parse(out.decode("utf-8", "replace"), err.decode("utf-8", "replace"), proc.returncode)

    async def assess(self, cam_name: str, when: datetime, box, cfg: dict,
                     cam_id: int | None = None, detection: bytes = b"") -> tuple[dict, list[tuple[str, bytes]]]:
        """Live frames by default; with `cam_id`, frames cut from the recording at `when`."""
        if cam_id is not None:
            frames = await self.recorded_frames(cam_id, when)
            if not frames:
                raise RuntimeError("no recording covers this event")
        else:
            frames = await self.collect_frames(cam_name, box)
            if detection:
                frames = [("detection", detection), *frames]
            if not frames:
                raise RuntimeError("no frame available (live cache cold)")
        verdict = await self.ask(cfg["model"], self._prompt(cam_name, when, frames), frames)
        return verdict, frames

    # ------------------------------------------------------------------ store
    async def _assess_and_store(self, event_id: int, cam_id: int, cam_name: str, event_time: datetime,
                                box, cfg: dict, recorded: bool = False, detection: bytes = b"") -> None:
        t0 = time.monotonic()
        verdict: dict = {}
        frames: list[tuple[str, bytes]] = []
        error = None
        try:
            verdict, frames = await self.assess(cam_name, event_time, box, cfg,
                                                cam_id=cam_id if recorded else None, detection=detection)
        except RuntimeError as e:
            error = str(e)
        latency = int((time.monotonic() - t0) * 1000)
        frames_dir = await asyncio.to_thread(_save_frames, event_id, event_time, frames) if frames else None
        # Anything but an explicit False counts as a human: the gate fails open.
        human = verdict.get("human_present") is not False
        row = EventAssessment(
            motion_event_id=event_id, camera_id=cam_id, event_time=event_time,
            status="error" if error else "ok", model=cfg["model"],
            subject=None if error else ("unknown" if human else "none"),
            people_count=int(verdict.get("people_count", 0) or 0),
            description=verdict.get("description"),
            activity=None if error else ("human confirmed" if human else "false detection"),
            suspicion=0, reason=verdict.get("reason"),
            would_alert=not error and human,
            latency_ms=latency, frames_dir=frames_dir, frame_count=len(frames), error=error,
        )
        async with get_session_factory()() as session:
            session.add(row)
            await session.commit()
        logger.info("Guard verdict", extra={
            "event_id": event_id, "camera_name": cam_name, "human": None if error else human,
            "latency_ms": latency, "frames": len(frames), "error": error,
            "description": (verdict.get("description") or "")[:160]})
        if time.monotonic() - self._last_prune > 86400:
            self._last_prune = time.monotonic()
            await asyncio.to_thread(_prune_frames)


def _mark_detection(frame: np.ndarray | None, bbox) -> bytes:
    """The detector's own frame as a JPEG, with the detected box outlined. Empty on any problem."""
    if frame is None or getattr(frame, "size", 0) == 0:
        return b""
    try:
        img = frame.copy()
        if bbox:
            x1, y1, x2, y2 = (int(v) for v in bbox)
            cv2.rectangle(img, (x1, y1), (x2, y2), (0, 0, 255), 2)
        return _encode(img)
    except Exception:
        return b""


def _normalise(bbox, frame_hw) -> tuple[float, float, float, float] | None:
    """Sub-stream pixel box -> fractions of the frame, which hold for the main stream too."""
    if not bbox or not frame_hw or frame_hw[0] <= 0 or frame_hw[1] <= 0:
        return None
    h, w = frame_hw
    x1, y1, x2, y2 = bbox
    return (max(x1 / w, 0.0), max(y1 / h, 0.0), min(x2 / w, 1.0), min(y2 / h, 1.0))


def _read_env(path: str) -> dict[str, str]:
    env: dict[str, str] = {}
    try:
        for raw in Path(path).read_text(encoding="utf-8").splitlines():
            if "=" in raw and not raw.lstrip().startswith("#"):
                k, _, v = raw.partition("=")
                env[k.strip()] = v.strip().strip('"').strip("'")
    except OSError:
        pass
    return env


def _parse(stdout: str, stderr: str, rc: int | None) -> dict:
    result = None
    for line in stdout.splitlines():
        try:
            obj = json.loads(line)
        except ValueError:
            continue
        if isinstance(obj, dict) and obj.get("type") == "result":
            result = obj
    if result is None:
        tail = (stderr or stdout).strip()[-300:]
        raise RuntimeError(f"no result from the model (exit {rc}): {tail}")
    if result.get("is_error") or result.get("subtype") not in (None, "success"):
        raise RuntimeError(f"model error: {str(result.get('result') or result.get('subtype'))[:300]}")
    structured = result.get("structured_output")
    if isinstance(structured, dict):
        return structured
    text = str(result.get("result") or "").strip()
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        raise RuntimeError(f"model did not answer in JSON: {text[:200]}")
    try:
        return json.loads(text[start:end + 1])
    except ValueError:
        raise RuntimeError(f"model answer was not valid JSON: {text[:200]}")


def frames_root() -> Path:
    return Path(get_bootstrap().data_dir) / "guard"


def _save_frames(event_id: int, when: datetime, frames: list[tuple[str, bytes]]) -> str:
    d = frames_root() / f"{when:%Y-%m-%d}" / str(event_id)
    d.mkdir(parents=True, exist_ok=True)
    for n, (_, jpeg) in enumerate(frames):
        (d / f"{n}.jpg").write_bytes(jpeg)
    return str(d)


def _prune_frames() -> None:
    cutoff = f"{datetime.now() - timedelta(days=KEEP_DAYS):%Y-%m-%d}"
    root = frames_root()
    if not root.is_dir():
        return
    for day in root.iterdir():
        if day.is_dir() and day.name < cutoff:
            shutil.rmtree(day, ignore_errors=True)


_guard: Guard | None = None


def get_guard() -> Guard:
    global _guard
    if _guard is None:
        _guard = Guard()
    return _guard
