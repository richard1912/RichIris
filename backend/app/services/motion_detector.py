"""Motion detection + AI object detection using go2rtc snapshots and YOLO."""

import asyncio
import json
import logging
import os
from datetime import datetime
from pathlib import Path

import cv2
import httpx
import numpy as np

from app.config import get_config
from app.database import get_session_factory
from app.models import MotionEvent
from app.services.ffmpeg import sanitize_camera_name
from app.services.go2rtc_client import get_stream_name
from app.services.object_detector import get_object_detector

_VEHICLE_LABELS = {"bicycle", "car", "motorcycle", "bus", "truck"}


def _label_category(label: str | None) -> str:
    """Map a detection label to its category (for event grouping)."""
    if label is None:
        return "motion"
    if label == "person":
        return "person"
    if label in _VEHICLE_LABELS:
        return "vehicle"
    return "animal"

logger = logging.getLogger(__name__)

COOLDOWN_SECONDS = 10
POLL_INTERVAL = 1.0       # Seconds between snapshot fetches
BLUR_SIZE = (21, 21)
DIFF_THRESHOLD = 25
MAX_CHANGE_PCT = 40       # Ignore frames where >40% changed (IR switch, exposure shift)
MOTION_ALPHA = 0.01       # Slow running-average adaptation (steady state)
MOTION_ALPHA_STARTUP = 0.2
BASELINE_STARTUP_FRAMES = 25
FRAME_TIMEOUT = 10.0      # HTTP timeout for snapshot fetch
HEARTBEAT_INTERVAL = 300   # Log heartbeat every 5 minutes (seconds)
# Min fraction of a YOLO bbox that must overlap the motion mask to count as "moving".
# Filters out static objects (e.g. parked cars) that YOLO detects but aren't actually moving.
MIN_MOTION_OVERLAP = 0.10  # 10% of bbox area must have changed pixels


def _snapshot_url(camera_name: str, host: str, port: int) -> str:
    """Return go2rtc snapshot URL for a camera's sub-stream."""
    stream_name = get_stream_name(camera_name) + "_s2_direct"
    return f"http://{host}:{port}/api/frame.jpeg?src={stream_name}"


class MotionDetector:
    def __init__(self):
        self._tasks: dict[int, asyncio.Task] = {}
        self._running = False
        # Keyed by (cam_id, category) — one active event per category per camera
        self._active_events: dict[tuple[int, str], int] = {}
        self._last_motion: dict[tuple[int, str], datetime] = {}
        self._script_off: dict[tuple[int, str], list[str]] = {}
        self._avg_baseline: dict[int, np.ndarray | None] = {}
        self._baseline_frames: dict[int, int] = {}
        self._client: httpx.AsyncClient | None = None

    @staticmethod
    def _parse_motion_scripts(camera) -> list[dict]:
        """Parse motion_scripts JSON from camera, falling back to legacy fields."""
        if camera.motion_scripts:
            try:
                return json.loads(camera.motion_scripts)
            except (json.JSONDecodeError, TypeError):
                pass
        # Legacy fallback
        if camera.motion_script:
            return [{"on": camera.motion_script, "off": camera.motion_script_off,
                     "persons": True, "vehicles": True, "animals": True, "motion_only": True}]
        return []

    def start(self, cameras: list) -> None:
        self._running = True
        self._client = httpx.AsyncClient(timeout=FRAME_TIMEOUT)
        cfg = get_config().go2rtc
        # Stagger camera starts to avoid concurrent go2rtc stream creation
        stagger_delay = 0
        for cam in cameras:
            if cam.motion_sensitivity > 0 and (cam.sub_stream_url or cam.rtsp_url):
                url = _snapshot_url(cam.name, cfg.host, cfg.port)
                scripts = self._parse_motion_scripts(cam)
                task = asyncio.create_task(
                    self._detect_loop(
                        cam.id, cam.name, url,
                        cam.motion_sensitivity, scripts,
                        ai_detection=cam.ai_detection,
                        ai_detect_persons=cam.ai_detect_persons,
                        ai_detect_vehicles=cam.ai_detect_vehicles,
                        ai_detect_animals=cam.ai_detect_animals,
                        ai_threshold=cam.ai_confidence_threshold / 100.0,
                        startup_delay=stagger_delay,
                    )
                )
                self._tasks[cam.id] = task
                stagger_delay += 2
        if self._tasks:
            logger.info("Motion detector started", extra={"cameras": len(self._tasks)})

    async def stop(self) -> None:
        self._running = False
        for task in self._tasks.values():
            task.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks.values(), return_exceptions=True)
        self._tasks.clear()
        if self._client:
            await self._client.aclose()
            self._client = None
        logger.info("Motion detector stopped")

    async def update_camera(self, camera) -> None:
        cam_id = camera.id
        if cam_id in self._tasks:
            self._tasks[cam_id].cancel()
            try:
                await self._tasks[cam_id]
            except (asyncio.CancelledError, Exception):
                pass
            del self._tasks[cam_id]
            # Finalize all active events for this camera
            keys = [k for k in self._active_events if k[0] == cam_id]
            for key in keys:
                await self._finalize_event(key)

        if self._running and camera.motion_sensitivity > 0 and camera.enabled:
            cfg = get_config().go2rtc
            url = _snapshot_url(camera.name, cfg.host, cfg.port)
            if camera.ai_detection:
                detector = get_object_detector()
                await detector.start()
            if self._client is None:
                self._client = httpx.AsyncClient(timeout=FRAME_TIMEOUT)
            scripts = self._parse_motion_scripts(camera)
            task = asyncio.create_task(
                self._detect_loop(
                    camera.id, camera.name, url,
                    camera.motion_sensitivity, scripts,
                    ai_detection=camera.ai_detection,
                    ai_detect_persons=camera.ai_detect_persons,
                    ai_detect_vehicles=camera.ai_detect_vehicles,
                    ai_detect_animals=camera.ai_detect_animals,
                    ai_threshold=camera.ai_confidence_threshold / 100.0,
                )
            )
            self._tasks[cam_id] = task
            logger.info("Motion detection restarted", extra={"camera": camera.name})

    async def _fetch_frame(self, url: str) -> np.ndarray | None:
        """Fetch a JPEG snapshot from go2rtc and decode it."""
        from app.services.go2rtc_client import get_snapshot_semaphore, wait_for_go2rtc_ready
        try:
            await wait_for_go2rtc_ready()
            async with get_snapshot_semaphore():
                resp = await self._client.get(url)
            if resp.status_code != 200:
                return None
            return cv2.imdecode(np.frombuffer(resp.content, np.uint8), cv2.IMREAD_COLOR)
        except (httpx.TimeoutException, httpx.HTTPError):
            return None

    async def _detect_loop(
        self, cam_id: int, cam_name: str, url: str,
        sensitivity: int, scripts: list[dict],
        ai_detection: bool = False,
        ai_detect_persons: bool = True, ai_detect_vehicles: bool = False,
        ai_detect_animals: bool = False,
        ai_threshold: float = 0.5,
        startup_delay: float = 0,
    ) -> None:
        """Per-camera detection loop: snapshot → motion check → YOLO → event."""
        if startup_delay > 0:
            await asyncio.sleep(startup_delay)
        threshold_pct = (101 - sensitivity) * 0.05
        consecutive_failures = 0
        last_heartbeat = datetime.now()

        self._avg_baseline[cam_id] = None
        self._baseline_frames[cam_id] = 0

        logger.info(
            "Motion detection started",
            extra={"camera": cam_name, "sensitivity": sensitivity,
                   "threshold_pct": round(threshold_pct, 2), "ai": ai_detection},
        )

        while self._running:
            try:
                frame = await self._fetch_frame(url)
                if frame is None:
                    consecutive_failures += 1
                    if consecutive_failures == 1 or consecutive_failures % 60 == 0:
                        logger.warning(
                            "Failed to fetch snapshot",
                            extra={"camera": cam_name, "consecutive_failures": consecutive_failures},
                        )
                    # Exponential backoff: 1s, 2s, 4s, 8s, max 15s
                    backoff = min(POLL_INTERVAL * (2 ** min(consecutive_failures - 1, 4)), 15)
                    await asyncio.sleep(backoff)
                    continue

                consecutive_failures = 0

                # Heartbeat
                now = datetime.now()
                if (now - last_heartbeat).total_seconds() >= HEARTBEAT_INTERVAL:
                    logger.debug("Motion loop alive", extra={"camera": cam_name})
                    last_heartbeat = now

                # --- Motion check (cheap pre-filter) ---
                gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
                gray = cv2.GaussianBlur(gray, BLUR_SIZE, 0)
                self._baseline_frames[cam_id] = self._baseline_frames.get(cam_id, 0) + 1

                if self._avg_baseline.get(cam_id) is None:
                    self._avg_baseline[cam_id] = gray.astype(np.float32)
                    await asyncio.sleep(POLL_INTERVAL)
                    continue

                avg = self._avg_baseline[cam_id]
                diff = cv2.absdiff(gray, avg.astype(np.uint8))
                _, thresh = cv2.threshold(diff, DIFF_THRESHOLD, 255, cv2.THRESH_BINARY)
                changed_pct = (np.count_nonzero(thresh) / thresh.size) * 100

                if changed_pct > MAX_CHANGE_PCT:
                    # IR switch / exposure shift — reset baseline
                    self._avg_baseline[cam_id] = gray.astype(np.float32)
                    self._baseline_frames[cam_id] = 0
                    await self._check_cooldown(cam_id)
                elif changed_pct >= threshold_pct:
                    # Motion detected — run YOLO if AI enabled, otherwise fire event
                    if ai_detection:
                        from app.services.object_detector import build_class_list
                        detector = get_object_detector()
                        classes = build_class_list(ai_detect_persons, ai_detect_vehicles, ai_detect_animals)
                        detections = await detector.detect_objects(frame, ai_threshold, classes=classes) if classes else []
                        # Filter out static objects: only keep detections whose
                        # bounding box overlaps sufficiently with the motion mask
                        moving = []
                        for d in detections:
                            roi = thresh[d.y1:d.y2, d.x1:d.x2]
                            box_area = roi.size
                            if box_area > 0 and np.count_nonzero(roi) / box_area >= MIN_MOTION_OVERLAP:
                                moving.append(d)
                        if moving:
                            # Group by category, fire one event per category
                            by_cat: dict[str, list] = {}
                            for d in moving:
                                cat = _label_category(d.label)
                                by_cat.setdefault(cat, []).append(d)
                            for cat_detections in by_cat.values():
                                best = max(cat_detections, key=lambda d: d.confidence)
                                await self._on_motion(
                                    cam_id, cam_name, changed_pct, scripts,
                                    detection_label=best.label,
                                    detection_confidence=best.confidence,
                                    frame=frame,
                                )
                        else:
                            await self._check_cooldown(cam_id)
                    else:
                        await self._on_motion(cam_id, cam_name, changed_pct, scripts, frame=frame)
                else:
                    await self._check_cooldown(cam_id)

                # Update running average baseline
                if changed_pct <= MAX_CHANGE_PCT and self._avg_baseline.get(cam_id) is not None:
                    alpha = (
                        MOTION_ALPHA_STARTUP
                        if self._baseline_frames.get(cam_id, 0) <= BASELINE_STARTUP_FRAMES
                        else MOTION_ALPHA
                    )
                    self._avg_baseline[cam_id] = (
                        alpha * gray.astype(np.float32) + (1.0 - alpha) * self._avg_baseline[cam_id]
                    )

                await asyncio.sleep(POLL_INTERVAL)

            except asyncio.CancelledError:
                return
            except Exception:
                logger.exception("Motion detection error", extra={"camera": cam_name})
                await asyncio.sleep(5)

    def _save_detection_thumbnail(self, cam_name: str, frame: np.ndarray, now: datetime) -> str | None:
        """Save the detection frame as a JPEG thumbnail. Returns the file path."""
        try:
            config = get_config()
            tp = config.trickplay
            safe_name = sanitize_camera_name(cam_name)
            date_str = now.strftime("%Y-%m-%d")
            thumb_dir = Path(config.storage.thumbnails_dir) / safe_name / date_str / "detection_thumbs"
            thumb_dir.mkdir(parents=True, exist_ok=True)
            filename = f"detect_{now.strftime('%H%M%S_%f')}.jpg"
            path = thumb_dir / filename
            resized = cv2.resize(frame, (tp.thumb_width, tp.thumb_height), interpolation=cv2.INTER_AREA)
            cv2.imwrite(str(path), resized, [cv2.IMWRITE_JPEG_QUALITY, 85])
            return str(path)
        except Exception:
            logger.exception("Failed to save detection thumbnail")
            return None

    @staticmethod
    def _scripts_for_category(scripts: list[dict], category: str) -> list[dict]:
        """Return script configs that match the given detection category."""
        cat_key = {
            "person": "persons",
            "vehicle": "vehicles",
            "animal": "animals",
            "motion": "motion_only",
        }.get(category, "motion_only")
        return [s for s in scripts if s.get(cat_key, False)]

    async def _on_motion(
        self, cam_id: int, cam_name: str, intensity: float,
        scripts: list[dict],
        detection_label: str | None = None, detection_confidence: float | None = None,
        frame: np.ndarray | None = None,
    ) -> None:
        now = datetime.now()
        category = _label_category(detection_label)
        key = (cam_id, category)
        self._last_motion[key] = now

        if key not in self._active_events:
            # Save detection thumbnail from the frame that triggered this event
            thumb_path = None
            if frame is not None:
                thumb_path = self._save_detection_thumbnail(cam_name, frame, now)

            factory = get_session_factory()
            async with factory() as session:
                event = MotionEvent(
                    camera_id=cam_id, start_time=now, peak_intensity=intensity,
                    detection_label=detection_label, detection_confidence=detection_confidence,
                    thumbnail_path=thumb_path,
                )
                session.add(event)
                await session.commit()
                await session.refresh(event)
                self._active_events[key] = event.id
                # Store off-scripts for this category so _finalize_event can run them
                matching = self._scripts_for_category(scripts, category)
                self._script_off[key] = [s.get("off") for s in matching if s.get("off")]

            log_extra = {"camera": cam_name, "intensity": round(intensity, 2), "category": category}
            if detection_label:
                log_extra["detection"] = detection_label
                log_extra["confidence"] = round(detection_confidence, 2)
            if thumb_path:
                log_extra["thumbnail"] = thumb_path
            logger.info("Motion started", extra=log_extra)

            # Run all matching on-scripts for this category
            for s in matching:
                if s.get("on"):
                    asyncio.create_task(self._run_script(
                        s["on"], cam_name, now, intensity, detection_label, detection_confidence,
                    ))
        else:
            factory = get_session_factory()
            async with factory() as session:
                event = await session.get(MotionEvent, self._active_events[key])
                if event:
                    if intensity > event.peak_intensity:
                        event.peak_intensity = intensity
                    if detection_confidence and (
                        event.detection_confidence is None
                        or detection_confidence > event.detection_confidence
                    ):
                        event.detection_confidence = detection_confidence
                        event.detection_label = detection_label
                    await session.commit()

    async def _check_cooldown(self, cam_id: int) -> None:
        """Check cooldown for all active event categories on this camera."""
        now = datetime.now()
        expired = [
            key for key in self._active_events
            if key[0] == cam_id
            and (now - self._last_motion.get(key, now)).total_seconds() >= COOLDOWN_SECONDS
        ]
        for key in expired:
            await self._finalize_event(key)

    async def _finalize_event(self, key: tuple[int, str]) -> None:
        event_id = self._active_events.pop(key, None)
        self._last_motion.pop(key, None)
        off_scripts = self._script_off.pop(key, None) or []
        if event_id is None:
            return
        factory = get_session_factory()
        async with factory() as session:
            event = await session.get(MotionEvent, event_id)
            if event:
                event.end_time = datetime.now()
                await session.commit()
        logger.info("Motion ended", extra={"camera_id": key[0], "category": key[1], "event_id": event_id})
        # Handle both legacy (single string) and new (list) formats
        if isinstance(off_scripts, str):
            off_scripts = [off_scripts]
        for script_off in off_scripts:
            if script_off:
                asyncio.create_task(self._run_script(script_off, str(key[0]), datetime.now(), 0.0))

    async def _run_script(
        self, script: str, cam_name: str, timestamp: datetime, intensity: float,
        detection_label: str | None = None, detection_confidence: float | None = None,
    ) -> None:
        try:
            env = {
                **os.environ,
                "MOTION_CAMERA": cam_name,
                "MOTION_TIME": timestamp.isoformat(),
                "MOTION_INTENSITY": str(round(intensity, 2)),
                "DETECTION_LABEL": detection_label or "",
                "DETECTION_CONFIDENCE": str(round(detection_confidence, 2)) if detection_confidence else "",
            }
            proc = await asyncio.create_subprocess_shell(
                script, env=env,
                stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE,
            )
            _, stderr = await asyncio.wait_for(proc.communicate(), timeout=30)
            if proc.returncode != 0:
                logger.warning("Motion script failed (rc=%d): %s",
                               proc.returncode, stderr.decode(errors="replace")[-200:].strip())
        except asyncio.TimeoutError:
            logger.warning("Motion script timed out", extra={"script": script})
        except Exception:
            logger.exception("Motion script error")


_detector: MotionDetector | None = None


def get_motion_detector() -> MotionDetector:
    global _detector
    if _detector is None:
        _detector = MotionDetector()
    return _detector
