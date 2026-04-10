"""Motion detection + AI object detection using go2rtc snapshots and RT-DETR."""

import asyncio
import json
import logging
import os
from datetime import datetime, timedelta
from pathlib import Path

import cv2
import numpy as np

from app.config import get_config
from app.database import get_session_factory
from app.models import MotionEvent
from app.services.ffmpeg import sanitize_camera_name
from app.services.frame_broker import get_frame_broker
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
MAX_EVENT_DURATION = 120  # Force-close events after 2 minutes to prevent false-positive stretching
POLL_INTERVAL = 0.5       # Seconds between frame checks (broker runs at 2 fps)
BLUR_SIZE = (21, 21)
DIFF_THRESHOLD = 25
MOTION_ALPHA = 0.01       # Slow running-average adaptation (steady state)
MOTION_ALPHA_STARTUP = 0.2
BASELINE_STARTUP_FRAMES = 25
HEARTBEAT_INTERVAL = 300   # Log heartbeat every 5 minutes (seconds)
# Min fraction of a detection bbox that must overlap the motion mask to count as "moving".
# Filters out static objects (e.g. parked cars) that are detected but aren't actually moving.
MIN_MOTION_OVERLAP = 0.10  # 10% of bbox area must have changed pixels
# Multi-frame AI confirmation: require N detections in M frames + positional movement
AI_CONFIRM_REQUIRED = 2   # detections needed within the window
AI_CONFIRM_WINDOW = 3     # sliding window size (frames)
AI_MIN_MOVE_PCT = 1.5     # min bbox center movement as % of frame diagonal


class MotionDetector:
    def __init__(self):
        self._tasks: dict[int, asyncio.Task] = {}
        self._running = False
        # Keyed by (cam_id, category) — one active event per category per camera
        self._active_events: dict[tuple[int, str], int] = {}
        self._event_start: dict[tuple[int, str], datetime] = {}
        self._last_motion: dict[tuple[int, str], datetime] = {}
        self._script_off: dict[tuple[int, str], list[str]] = {}
        self._avg_baseline: dict[int, np.ndarray | None] = {}
        self._baseline_frames: dict[int, int] = {}
        # Multi-frame AI confirmation buffers
        self._detection_history: dict[tuple[int, str], list[bool]] = {}
        self._detection_positions: dict[tuple[int, str], list[tuple[float, float] | None]] = {}
        # Best detection seen in current confirmation window: (label, confidence, intensity, frame)
        self._pending_detections: dict[tuple[int, str], tuple] = {}
        # Cached move threshold per camera (pixels), computed from frame dimensions
        self._move_threshold: dict[int, float] = {}

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

    async def start(self, cameras: list) -> None:
        self._running = True
        # Close any orphaned events from previous runs (service restart / crash)
        await self._close_orphaned_events()
        stagger_delay = 0
        for cam in cameras:
            if cam.motion_sensitivity > 0 and (cam.sub_stream_url or cam.rtsp_url):
                scripts = self._parse_motion_scripts(cam)
                task = asyncio.create_task(
                    self._detect_loop(
                        cam.id, cam.name,
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
                stagger_delay += 1
        if self._tasks:
            logger.info("Motion detector started", extra={"cameras": len(self._tasks)})

    async def _close_orphaned_events(self) -> None:
        """Close any events with no end_time (orphaned by previous restart/crash)."""
        factory = get_session_factory()
        async with factory() as session:
            from sqlalchemy import select
            result = await session.execute(
                select(MotionEvent).where(MotionEvent.end_time.is_(None))
            )
            orphaned = result.scalars().all()
            if orphaned:
                for event in orphaned:
                    # Set end_time to start_time + MAX_EVENT_DURATION or now, whichever is earlier
                    max_end = event.start_time + timedelta(seconds=MAX_EVENT_DURATION)
                    event.end_time = min(max_end, datetime.now())
                await session.commit()
                logger.info("Closed orphaned motion events", extra={"count": len(orphaned)})

    async def stop(self) -> None:
        self._running = False
        for task in self._tasks.values():
            task.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks.values(), return_exceptions=True)
        self._tasks.clear()
        # Finalize all active events before shutdown
        for key in list(self._active_events):
            await self._finalize_event(key)
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
            # Finalize all active events and clear AI history for this camera
            keys = [k for k in self._active_events if k[0] == cam_id]
            for key in keys:
                await self._finalize_event(key)
            self._clear_ai_history(cam_id)

        if self._running and camera.motion_sensitivity > 0 and camera.enabled:
            if camera.ai_detection:
                detector = get_object_detector()
                await detector.start()
            scripts = self._parse_motion_scripts(camera)
            task = asyncio.create_task(
                self._detect_loop(
                    camera.id, camera.name,
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

    def _clear_ai_history(self, cam_id: int) -> None:
        """Clear all AI confirmation buffers for a camera (e.g. on baseline reset)."""
        for key in list(self._detection_history):
            if key[0] == cam_id:
                del self._detection_history[key]
        for key in list(self._detection_positions):
            if key[0] == cam_id:
                del self._detection_positions[key]
        for key in list(self._pending_detections):
            if key[0] == cam_id:
                del self._pending_detections[key]

    def _record_detection(self, cam_id: int, category: str, detected: bool,
                          bbox_center: tuple[float, float] | None = None) -> bool:
        """Record a frame result. Returns True if confirmed (enough frames + movement)."""
        key = (cam_id, category)
        history = self._detection_history.setdefault(key, [])
        history.append(detected)
        if len(history) > AI_CONFIRM_WINDOW:
            history.pop(0)
        positions = self._detection_positions.setdefault(key, [])
        positions.append(bbox_center)
        if len(positions) > AI_CONFIRM_WINDOW:
            positions.pop(0)
        # Not enough detections yet
        if sum(history) < AI_CONFIRM_REQUIRED:
            return False
        # Check positional movement between detected frames
        detected_pos = [p for p in positions if p is not None]
        if len(detected_pos) < 2:
            return True  # only 1 position so far — trust it (conservative)
        max_dist = 0.0
        for i, p1 in enumerate(detected_pos):
            for p2 in detected_pos[i + 1:]:
                d = ((p1[0] - p2[0]) ** 2 + (p1[1] - p2[1]) ** 2) ** 0.5
                if d > max_dist:
                    max_dist = d
        return max_dist >= self._move_threshold.get(cam_id, 0)

    async def _detect_loop(
        self, cam_id: int, cam_name: str,
        sensitivity: int, scripts: list[dict],
        ai_detection: bool = False,
        ai_detect_persons: bool = True, ai_detect_vehicles: bool = False,
        ai_detect_animals: bool = False,
        ai_threshold: float = 0.5,
        startup_delay: float = 0,
    ) -> None:
        """Per-camera detection loop: pull frame → motion check → AI detection → event."""
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

        broker = get_frame_broker()

        while self._running:
            try:
                frame = await broker.get_fresh(cam_id, max_wait=5.0)
                if frame is None:
                    consecutive_failures += 1
                    if consecutive_failures == 1 or consecutive_failures % 30 == 0:
                        logger.warning(
                            "Failed to fetch snapshot",
                            extra={"camera": cam_name, "consecutive_failures": consecutive_failures},
                        )
                    await asyncio.sleep(1.0)
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

                if changed_pct >= threshold_pct:
                    # Motion detected — run AI detection if enabled, otherwise fire event
                    if ai_detection:
                        # Compute move threshold once from frame dimensions
                        if cam_id not in self._move_threshold:
                            h, w = frame.shape[:2]
                            diag = (h ** 2 + w ** 2) ** 0.5
                            self._move_threshold[cam_id] = diag * AI_MIN_MOVE_PCT / 100

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
                        # Group moving detections by category
                        by_cat: dict[str, list] = {}
                        for d in moving:
                            cat = _label_category(d.label)
                            by_cat.setdefault(cat, []).append(d)
                        # Build set of all enabled AI categories for this camera
                        enabled_cats = set()
                        if ai_detect_persons:
                            enabled_cats.add("person")
                        if ai_detect_vehicles:
                            enabled_cats.add("vehicle")
                        if ai_detect_animals:
                            enabled_cats.add("animal")
                        # Record detection result for each enabled category
                        any_confirmed = False
                        for cat in enabled_cats:
                            if cat in by_cat:
                                best = max(by_cat[cat], key=lambda d: d.confidence)
                                cx = (best.x1 + best.x2) / 2.0
                                cy = (best.y1 + best.y2) / 2.0
                                confirmed = self._record_detection(cam_id, cat, True, (cx, cy))
                                # Track best pending detection for when confirmation fires
                                pkey = (cam_id, cat)
                                prev = self._pending_detections.get(pkey)
                                if prev is None or best.confidence > prev[1]:
                                    self._pending_detections[pkey] = (
                                        best.label, best.confidence, changed_pct, frame.copy(),
                                    )
                                if confirmed:
                                    any_confirmed = True
                                    pending = self._pending_detections.pop(pkey, None)
                                    if pending:
                                        await self._on_motion(
                                            cam_id, cam_name, pending[2], scripts,
                                            detection_label=pending[0],
                                            detection_confidence=pending[1],
                                            frame=pending[3],
                                        )
                                    else:
                                        await self._on_motion(
                                            cam_id, cam_name, changed_pct, scripts,
                                            detection_label=best.label,
                                            detection_confidence=best.confidence,
                                            frame=frame,
                                        )
                            else:
                                # Category not detected this frame — record miss
                                self._record_detection(cam_id, cat, False)
                                # Clear pending if window is all misses
                                pkey = (cam_id, cat)
                                hist = self._detection_history.get(pkey, [])
                                if hist and not any(hist):
                                    self._pending_detections.pop(pkey, None)
                        if not any_confirmed:
                            await self._check_cooldown(cam_id)
                    else:
                        await self._on_motion(cam_id, cam_name, changed_pct, scripts, frame=frame)
                else:
                    await self._check_cooldown(cam_id)

                # Update running average baseline
                if self._avg_baseline.get(cam_id) is not None:
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

        # Force-close events that exceed max duration to prevent false-positive stretching
        if key in self._active_events:
            start = self._event_start.get(key)
            if start and (now - start).total_seconds() >= MAX_EVENT_DURATION:
                await self._finalize_event(key)

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
                self._event_start[key] = now
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
        self._event_start.pop(key, None)
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
