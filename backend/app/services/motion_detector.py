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
from app.services.face_recognizer import get_face_recognizer
from app.services.frame_broker import get_frame_broker
from app.services.object_detector import get_object_detector
from app.services.zone_mask import get_zone_mask_cache

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
# Once a known face matches at >= this cosine, skip further face inference
# in the event. Well above the default match threshold so we don't lock
# in a marginal match and miss a clearer frame.
FACE_LOCKIN_CONFIDENCE = 0.75
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
        self._script_off: dict[tuple[int, str], list[tuple[str, int]]] = {}  # (script, off_delay)
        self._pending_off_tasks: dict[tuple[int, str], list[asyncio.Task]] = {}
        self._avg_baseline: dict[int, np.ndarray | None] = {}
        self._baseline_frames: dict[int, int] = {}
        # Multi-frame AI confirmation buffers
        self._detection_history: dict[tuple[int, str], list[bool]] = {}
        self._detection_positions: dict[tuple[int, str], list[tuple[float, float] | None]] = {}
        # Best detection seen in current confirmation window: (label, confidence, intensity, frame)
        self._pending_detections: dict[tuple[int, str], tuple] = {}
        # Cached move threshold per camera (pixels), computed from frame dimensions
        self._move_threshold: dict[int, float] = {}
        # Highest cosine similarity known-face match seen so far per active event.
        # Used to skip further face inference once we have a confident identification.
        self._best_face_confidence: dict[tuple[int, str], float] = {}

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
                        face_recognition=getattr(cam, "face_recognition", False),
                        face_match_threshold=getattr(cam, "face_match_threshold", 50) / 100.0,
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
        # Cancel any pending delayed off-scripts
        for tasks in self._pending_off_tasks.values():
            for task in tasks:
                task.cancel()
        self._pending_off_tasks.clear()
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
            # Cancel pending off-tasks and finalize all active events for this camera
            for key in list(self._pending_off_tasks):
                if key[0] == cam_id:
                    for task in self._pending_off_tasks.pop(key, []):
                        task.cancel()
            keys = [k for k in self._active_events if k[0] == cam_id]
            for key in keys:
                await self._finalize_event(key)
            self._clear_ai_history(cam_id)

        if self._running and camera.motion_sensitivity > 0 and camera.enabled:
            if camera.ai_detection:
                detector = get_object_detector()
                await detector.start()
            if getattr(camera, "face_recognition", False):
                recognizer = get_face_recognizer()
                await recognizer.start()
                await recognizer.reload_cache()
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
                    face_recognition=getattr(camera, "face_recognition", False),
                    face_match_threshold=getattr(camera, "face_match_threshold", 50) / 100.0,
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
        face_recognition: bool = False,
        face_match_threshold: float = 0.5,
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
                                best_bbox = (best.x1, best.y1, best.x2, best.y2)
                                if prev is None or best.confidence > prev[1]:
                                    self._pending_detections[pkey] = (
                                        best.label, best.confidence, changed_pct, frame.copy(), best_bbox,
                                    )
                                if confirmed:
                                    any_confirmed = True
                                    pending = self._pending_detections.pop(pkey, None)
                                    if pending:
                                        p_label, p_conf, p_intensity, p_frame, p_bbox = pending
                                    else:
                                        p_label, p_conf, p_intensity, p_frame, p_bbox = (
                                            best.label, best.confidence, changed_pct, frame, best_bbox,
                                        )
                                    # Always run SCRFD on person events so the enrollment UI
                                    # can filter thumbnails to ones with an actual face, even
                                    # on cameras where face recognition itself is off.
                                    # Full match + embedding only runs when FR is enabled and
                                    # we haven't already locked in a confident match.
                                    face_info = None
                                    thumb_frame = p_frame
                                    if cat == "person":
                                        ev_key = (cam_id, cat)
                                        locked_in = (
                                            self._best_face_confidence.get(ev_key, 0.0)
                                            >= FACE_LOCKIN_CONFIDENCE
                                        )
                                        if face_recognition and not locked_in:
                                            face_info = await self._recognize_faces(
                                                cam_name, p_frame, p_bbox, face_match_threshold,
                                            )
                                            mf = (face_info or {}).get("source_frame")
                                            if mf is not None:
                                                thumb_frame = mf
                                        elif not face_recognition:
                                            # Cheap SCRFD-only pass to populate face_detected
                                            face_info = await self._scrfd_only(
                                                cam_name, p_frame, p_bbox,
                                            )
                                            mf = (face_info or {}).get("source_frame")
                                            if mf is not None:
                                                thumb_frame = mf
                                    await self._on_motion(
                                        cam_id, cam_name, p_intensity, scripts,
                                        detection_label=p_label,
                                        detection_confidence=p_conf,
                                        frame=thumb_frame,
                                        face_info=face_info,
                                        detection_bbox=p_bbox,
                                        motion_mask=thresh,
                                        motion_threshold_pct=threshold_pct,
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
                        await self._on_motion(
                            cam_id, cam_name, changed_pct, scripts, frame=frame,
                            motion_mask=thresh, motion_threshold_pct=threshold_pct,
                        )
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
        """Save the detection frame as a JPEG thumbnail. Returns the file path.

        Written at the native sub-stream resolution (no downscale) so faces are
        large enough for SCRFD enrollment. JPEG compression keeps the disk cost
        modest even at 640–1280 px wide.
        """
        try:
            config = get_config()
            safe_name = sanitize_camera_name(cam_name)
            date_str = now.strftime("%Y-%m-%d")
            thumb_dir = Path(config.storage.thumbnails_dir) / safe_name / date_str / "detection_thumbs"
            thumb_dir.mkdir(parents=True, exist_ok=True)
            filename = f"detect_{now.strftime('%H%M%S_%f')}.jpg"
            path = thumb_dir / filename
            cv2.imwrite(str(path), frame, [cv2.IMWRITE_JPEG_QUALITY, 85])
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

    @staticmethod
    def _scripts_match_face(script: dict, matched_face_ids: set[int], face_unknown: bool) -> bool:
        """Evaluate face-based trigger filters on a script entry.

        - If `faces` is set (non-empty), the script fires only when any listed face matches.
        - If `face_unknown` is true, the script fires only on unknown-face events.
        - If both are set, either condition triggers (OR).
        - If neither is set, the script behaves as before (always fires for the category).
        """
        required_faces = script.get("faces") or []
        wants_unknown = bool(script.get("face_unknown", False))
        if not required_faces and not wants_unknown:
            return True
        if required_faces and matched_face_ids.intersection(required_faces):
            return True
        if wants_unknown and face_unknown:
            return True
        return False

    async def _recognize_faces(
        self, cam_name: str, sub_frame: np.ndarray,
        sub_bbox: tuple[int, int, int, int], threshold: float,
    ) -> dict:
        """Run SCRFD + ArcFace on a fresh main-stream snapshot.

        The sub-stream used for motion + RT-DETR is usually 640×480, where
        faces end up at 10–25 px and ArcFace embeddings become unreliable.
        We fetch a single JPEG from the main-stream via go2rtc and rescale
        the person bbox to match its dimensions, then run the face pipeline
        on that high-res crop. Falls back to the sub-stream frame if the
        main-stream snapshot is unavailable.
        """
        recognizer = get_face_recognizer()
        if not recognizer.available:
            return {"matches": [], "unknown": False}

        frame, bbox = await self._fetch_main_for_face(cam_name, sub_frame, sub_bbox)
        used_main = frame is not sub_frame
        try:
            hits = await recognizer.detect_and_embed(frame, person_bbox=bbox)
        except Exception:
            logger.exception("Face recognition failed")
            return {"matches": [], "unknown": False, "source_frame": None}

        matches: list[dict] = []
        seen_ids: set[int] = set()
        unknown = False
        # Collect unknown-hit details for the clustering queue. We save crops
        # immediately (cheap filesystem write) but defer DB inserts to the
        # caller so we can stamp them with the created motion_event_id.
        from app.services.face_crop import save_face_crop
        unknown_hits: list[dict] = []
        for h in hits:
            m = recognizer.match(h.embedding, threshold)
            if m is None:
                unknown = True
                crop_path = save_face_crop(frame, (h.x1, h.y1, h.x2, h.y2))
                unknown_hits.append({
                    "embedding": h.embedding.tobytes(),
                    "score": float(h.score),
                    "crop_path": crop_path,
                })
                continue
            if m.face_id in seen_ids:
                for existing in matches:
                    if existing["face_id"] == m.face_id and m.confidence > existing["confidence"]:
                        existing["confidence"] = m.confidence
                continue
            seen_ids.add(m.face_id)
            matches.append({
                "face_id": m.face_id,
                "name": m.name,
                "confidence": round(m.confidence, 3),
            })
        return {
            "matches": matches,
            "unknown": unknown,
            "face_detected": len(hits) > 0,
            "unknown_hits": unknown_hits,
            # Return the main-stream frame we pulled so the caller can use it
            # as the saved detection thumbnail — future enrollments then have
            # a high-res source to extract a cleaner face crop from.
            "source_frame": frame if used_main else None,
        }

    async def _scrfd_only(
        self, cam_name: str, sub_frame: np.ndarray,
        sub_bbox: tuple[int, int, int, int],
    ) -> dict:
        """Run only SCRFD face detection (no ArcFace embedding / matching).

        Used to populate the `face_detected` flag for person events on cameras
        that don't have face recognition enabled, so the enrollment UI can
        still filter thumbnails to ones containing an actual face.
        """
        recognizer = get_face_recognizer()
        if not recognizer.available:
            return {"matches": [], "unknown": False, "face_detected": False, "source_frame": None}
        frame, bbox = await self._fetch_main_for_face(cam_name, sub_frame, sub_bbox)
        used_main = frame is not sub_frame
        try:
            hits = await recognizer.detect_and_embed(frame, person_bbox=bbox)
        except Exception:
            logger.exception("SCRFD-only face detection failed")
            return {"matches": [], "unknown": False, "face_detected": False, "source_frame": None}
        return {
            "matches": [],
            "unknown": False,
            "face_detected": len(hits) > 0,
            "source_frame": frame if used_main else None,
        }

    async def _fetch_main_for_face(
        self, cam_name: str, sub_frame: np.ndarray,
        sub_bbox: tuple[int, int, int, int],
    ) -> tuple[np.ndarray, tuple[int, int, int, int]]:
        """Fetch a main-stream JPEG snapshot and scale the sub-stream bbox to it.

        Returns (frame, bbox_xyxy). On any failure, returns the original
        sub-stream frame + bbox so face recognition still runs (just at lower
        resolution).
        """
        try:
            from app.services.go2rtc_client import get_go2rtc_client, get_stream_name
            stream = f"{get_stream_name(cam_name)}_s1_direct"
            jpeg = await get_go2rtc_client().fetch_jpeg(stream, timeout=1.5)
            if not jpeg:
                return sub_frame, sub_bbox
            arr = np.frombuffer(jpeg, dtype=np.uint8)
            main = cv2.imdecode(arr, cv2.IMREAD_COLOR)
            if main is None or main.size == 0:
                return sub_frame, sub_bbox
            sh, sw = sub_frame.shape[:2]
            mh, mw = main.shape[:2]
            sx, sy = mw / sw, mh / sh
            x1, y1, x2, y2 = sub_bbox
            return main, (
                int(x1 * sx), int(y1 * sy),
                int(x2 * sx), int(y2 * sy),
            )
        except Exception:
            logger.exception("Failed to fetch main-stream snapshot for face recognition")
            return sub_frame, sub_bbox

    async def _zones_triggered_for_event(
        self,
        cam_id: int,
        shape: tuple[int, int] | None,
        detection_bbox: tuple[int, int, int, int] | None,
        motion_mask: np.ndarray | None,
        motion_threshold_pct: float,
    ) -> list[str]:
        """Return names of all zones on this camera that contain the triggering event.

        Bbox events: zone contains the bbox's bottom-center (feet/wheels anchor).
        Motion-only events: motion intensity inside the zone exceeds sensitivity.
        Returns [] when no shape is available or the camera has no zones.
        """
        if shape is None:
            return []
        from sqlalchemy import select
        from app.models import Zone as ZoneModel
        factory = get_session_factory()
        async with factory() as session:
            result = await session.execute(
                select(ZoneModel.id, ZoneModel.name).where(ZoneModel.camera_id == cam_id)
            )
            zones = list(result.all())
        if not zones:
            return []
        cache = get_zone_mask_cache()
        names: list[str] = []
        for zid, zname in zones:
            mask = await cache.get_mask(zid, shape)
            if mask is None:
                continue
            if detection_bbox is not None:
                if cache.bbox_in_mask(detection_bbox, mask):
                    names.append(zname)
            elif motion_mask is not None:
                if cache.motion_in_mask(motion_mask, mask, motion_threshold_pct):
                    names.append(zname)
        return names

    async def _filter_firing_by_zone(
        self,
        firing: list[dict],
        shape: tuple[int, int],
        detection_bbox: tuple[int, int, int, int] | None,
        motion_mask: np.ndarray | None,
        motion_threshold_pct: float,
    ) -> list[dict]:
        """Drop scripts whose zone_ids don't contain the triggering bbox/motion.

        - Script with empty zone_ids → always passes (current behavior).
        - Script with zone_ids + a detection_bbox → passes if bbox's bottom-center
          falls inside the union of its zones.
        - Script with zone_ids + motion-only (no bbox) → passes if motion pixels
          inside the union zone exceed the camera's sensitivity threshold.
        - If the union mask can't be built (zones missing/invalid), the script
          is skipped — fail-safe: a broken zone doesn't accidentally fire.
        """
        out: list[dict] = []
        cache = get_zone_mask_cache()
        h, w = shape
        for s in firing:
            zids = s.get("zone_ids") or []
            if not zids:
                out.append(s)
                continue
            union = await cache.get_union_mask(zids, shape)
            if union is None:
                logger.info(
                    "Zone filter: no valid union mask — skipping script",
                    extra={"zone_ids": zids, "shape": shape},
                )
                continue
            if detection_bbox is not None:
                x1, y1, x2, y2 = detection_bbox
                cx = int((x1 + x2) / 2.0)
                cy = int(y2)
                passed = cache.bbox_in_mask(detection_bbox, union)
                logger.info(
                    "Zone filter: bbox " + ("PASS" if passed else "REJECT"),
                    extra={
                        "zone_ids": zids,
                        "bbox": [x1, y1, x2, y2],
                        "anchor_xy": [cx, cy],
                        "shape_wh": [w, h],
                        "anchor_norm": [round(cx / max(w, 1), 3), round(cy / max(h, 1), 3)],
                    },
                )
                if passed:
                    out.append(s)
            elif motion_mask is not None:
                passed = cache.motion_in_mask(motion_mask, union, motion_threshold_pct)
                logger.info(
                    "Zone filter: motion " + ("PASS" if passed else "REJECT"),
                    extra={"zone_ids": zids, "threshold_pct": motion_threshold_pct},
                )
                if passed:
                    out.append(s)
            else:
                logger.info(
                    "Zone filter: no signal — skipping script",
                    extra={"zone_ids": zids},
                )
        return out

    async def _on_motion(
        self, cam_id: int, cam_name: str, intensity: float,
        scripts: list[dict],
        detection_label: str | None = None, detection_confidence: float | None = None,
        frame: np.ndarray | None = None,
        face_info: dict | None = None,
        detection_bbox: tuple[int, int, int, int] | None = None,
        motion_mask: np.ndarray | None = None,
        motion_threshold_pct: float = 0.0,
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

        # Cancel any pending delayed off-scripts (motion resumed before they fired)
        for task in self._pending_off_tasks.pop(key, []):
            task.cancel()

        face_matches = (face_info or {}).get("matches") or []
        face_unknown = bool((face_info or {}).get("unknown"))
        face_detected = bool((face_info or {}).get("face_detected"))
        matched_face_ids = {m["face_id"] for m in face_matches}
        # Track the best known-face confidence for this event so the loop can
        # short-circuit further face inference once we have a confident match.
        if face_matches:
            top = max(m["confidence"] for m in face_matches)
            prev_best = self._best_face_confidence.get(key, 0.0)
            if top > prev_best:
                self._best_face_confidence[key] = top

        if key not in self._active_events:
            # Save detection thumbnail from the frame that triggered this event
            thumb_path = None
            if frame is not None:
                thumb_path = self._save_detection_thumbnail(cam_name, frame, now)

            # Resolve firing scripts BEFORE inserting the event so we can
            # snapshot their display names onto the row. Firing items are
            # references drawn from `scripts`, so identity lookup is safe.
            matching = self._scripts_for_category(scripts, category)
            firing = [s for s in matching if self._scripts_match_face(s, matched_face_ids, face_unknown)]
            # Compute shape once — used for both zone resolution and per-script
            # zone filtering. Frames come from the FrameBroker (sub-stream), so
            # shape == frame.shape[:2] when available; fall back to motion_mask.
            shape_hw: tuple[int, int] | None = None
            if frame is not None:
                shape_hw = (int(frame.shape[0]), int(frame.shape[1]))
            elif motion_mask is not None:
                shape_hw = (int(motion_mask.shape[0]), int(motion_mask.shape[1]))
            if firing and shape_hw is not None:
                firing = await self._filter_firing_by_zone(
                    firing, shape_hw, detection_bbox, motion_mask, motion_threshold_pct,
                )
            # Snapshot display names (custom name if set, else "Script N" using
            # the script's 1-based index in the camera's scripts list at this moment).
            name_by_id = {
                id(s): (s.get("name") or "").strip() or f"Script {i + 1}"
                for i, s in enumerate(scripts)
            }
            fired_names = [name_by_id[id(s)] for s in firing if id(s) in name_by_id]
            # Snapshot zone names the detection fell inside (camera-wide, not
            # restricted to the scripts' zone_ids).
            zone_names = await self._zones_triggered_for_event(
                cam_id, shape_hw, detection_bbox, motion_mask, motion_threshold_pct,
            )

            factory = get_session_factory()
            async with factory() as session:
                event = MotionEvent(
                    camera_id=cam_id, start_time=now, peak_intensity=intensity,
                    detection_label=detection_label, detection_confidence=detection_confidence,
                    thumbnail_path=thumb_path,
                    face_matches=json.dumps(face_matches) if face_matches else None,
                    face_unknown=face_unknown and not face_matches,
                    face_detected=face_detected,
                    scripts_fired=json.dumps(fired_names) if fired_names else None,
                    zones_triggered=json.dumps(zone_names) if zone_names else None,
                )
                session.add(event)
                await session.commit()
                await session.refresh(event)
                self._active_events[key] = event.id
                self._event_start[key] = now

                # Queue unknown-face embeddings for the background clusterer.
                # Only done at event-creation to avoid flooding the queue when
                # the same unknown person stands in view across many frames.
                unknown_hits = (face_info or {}).get("unknown_hits") or []
                if unknown_hits and not face_matches:
                    from app.models import UnclusteredFace
                    for uh in unknown_hits:
                        session.add(UnclusteredFace(
                            motion_event_id=event.id,
                            camera_id=cam_id,
                            embedding=uh["embedding"],
                            face_crop_path=uh.get("crop_path"),
                            detection_score=uh.get("score"),
                        ))
                    await session.commit()
                # Store off-scripts with their delays for _finalize_event
                self._script_off[key] = [
                    (s.get("off"), s.get("off_delay", COOLDOWN_SECONDS))
                    for s in firing if s.get("off")
                ]

            log_extra = {"camera": cam_name, "intensity": round(intensity, 2), "category": category}
            if detection_label:
                log_extra["detection"] = detection_label
                log_extra["confidence"] = round(detection_confidence, 2)
            if thumb_path:
                log_extra["thumbnail"] = thumb_path
            if face_matches:
                log_extra["faces"] = [m["name"] for m in face_matches]
            if face_unknown:
                log_extra["face_unknown"] = True
            logger.info("Motion started", extra=log_extra)

            # Run all matching on-scripts for this category (honoring face filters)
            for s in firing:
                if s.get("on"):
                    asyncio.create_task(self._run_script(
                        s["on"], cam_name, now, intensity, detection_label, detection_confidence,
                        face_names=[m["name"] for m in face_matches],
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
                    # Merge face matches: keep highest-confidence per face_id across the event
                    if face_matches or face_unknown:
                        existing: dict[int, dict] = {}
                        if event.face_matches:
                            try:
                                for m in json.loads(event.face_matches):
                                    existing[m["face_id"]] = m
                            except (json.JSONDecodeError, TypeError, KeyError):
                                pass
                        for m in face_matches:
                            prev = existing.get(m["face_id"])
                            if prev is None or m["confidence"] > prev["confidence"]:
                                existing[m["face_id"]] = m
                        if existing:
                            event.face_matches = json.dumps(list(existing.values()))
                            # Once we have a known match, any earlier "unknown"
                            # flag was almost certainly the same person at a
                            # worse angle — clear it so the timeline reflects
                            # the identity we now know about.
                            event.face_unknown = False
                        elif face_unknown:
                            event.face_unknown = True
                    if face_detected and not event.face_detected:
                        event.face_detected = True
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
        self._best_face_confidence.pop(key, None)
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
        # Schedule off-scripts with per-script delays
        pending_tasks = []
        for item in off_scripts:
            # Handle legacy (single string) and new (script, delay) tuple formats
            if isinstance(item, str):
                script_off, delay = item, COOLDOWN_SECONDS
            else:
                script_off, delay = item
            if not script_off:
                continue
            # Event finalizes COOLDOWN_SECONDS after last motion.
            # If off_delay > COOLDOWN_SECONDS, schedule additional wait.
            remaining = max(0, delay - COOLDOWN_SECONDS)
            if remaining > 0:
                task = asyncio.create_task(
                    self._delayed_run_script(remaining, script_off, str(key[0]), datetime.now(), 0.0))
                pending_tasks.append(task)
            else:
                asyncio.create_task(self._run_script(script_off, str(key[0]), datetime.now(), 0.0))
        if pending_tasks:
            self._pending_off_tasks[key] = pending_tasks

    async def _delayed_run_script(
        self, delay: float, script: str, cam_name: str,
        timestamp: datetime, intensity: float,
    ) -> None:
        """Wait *delay* seconds then run an off-script. Cancellation-safe."""
        await asyncio.sleep(delay)
        await self._run_script(script, cam_name, timestamp, intensity)

    async def _run_script(
        self, script: str, cam_name: str, timestamp: datetime, intensity: float,
        detection_label: str | None = None, detection_confidence: float | None = None,
        face_names: list[str] | None = None,
    ) -> None:
        try:
            env = {
                **os.environ,
                "MOTION_CAMERA": cam_name,
                "MOTION_TIME": timestamp.isoformat(),
                "MOTION_INTENSITY": str(round(intensity, 2)),
                "DETECTION_LABEL": detection_label or "",
                "DETECTION_CONFIDENCE": str(round(detection_confidence, 2)) if detection_confidence else "",
                "FACE_NAMES": ",".join(face_names) if face_names else "",
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
