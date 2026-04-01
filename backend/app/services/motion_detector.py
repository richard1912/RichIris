"""Motion detection service using OpenCV frame differencing on RTSP sub-streams."""

import asyncio
import logging
import os
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

import cv2
import numpy as np

from app.config import get_config
from app.database import get_session_factory
from app.models import MotionEvent
from app.services.go2rtc_client import get_stream_name
from app.services.object_detector import get_object_detector

logger = logging.getLogger(__name__)

COOLDOWN_SECONDS = 10
DETECT_FPS = 2
BLUR_SIZE = (21, 21)
DIFF_THRESHOLD = 25
MAX_CHANGE_PCT = 40       # Ignore frames where >40% changed (IR switch, exposure shift)

# Frigate-style AI detection pipeline
SCORE_HISTORY_SIZE = 10   # Rolling window of YOLO scores per camera
MAX_DISAPPEARED = 3       # Consecutive no-detection frames before losing confirmation
MOTION_ALPHA = 0.01       # Slow running-average adaptation (steady state)
MOTION_ALPHA_STARTUP = 0.2  # Fast adaptation for first N frames to establish baseline
BASELINE_STARTUP_FRAMES = 25


def _go2rtc_rtsp_url(camera_name: str, rtsp_port: int) -> str:
    """Return go2rtc's local RTSP URL for a camera's sub-stream.

    Routes motion detection through go2rtc instead of connecting directly to the
    camera. go2rtc already holds a persistent connection to the sub-stream — reusing
    it avoids a second RTSP session which causes cameras to drop connections.
    Note: go2rtc serves RTSP on rtsp_port (default 8554), NOT the HTTP API port.
    """
    stream_name = get_stream_name(camera_name) + "_s2_direct"
    return f"rtsp://127.0.0.1:{rtsp_port}/{stream_name}"



class MotionDetector:
    def __init__(self):
        self._tasks: dict[int, asyncio.Task] = {}
        self._running = False
        self._executor = ThreadPoolExecutor(max_workers=6, thread_name_prefix="motion")
        self._active_events: dict[int, int] = {}      # camera_id -> motion_event DB id
        self._last_motion: dict[int, datetime] = {}   # camera_id -> last motion time
        self._script_off: dict[int, str | None] = {}  # camera_id -> off script

        # Frigate-style per-camera AI detection state
        self._score_history: dict[int, deque] = {}         # cam_id -> deque(maxlen=SCORE_HISTORY_SIZE)
        self._disappeared: dict[int, int] = {}             # cam_id -> consecutive frames with no detection
        self._confirmed: set[int] = set()                  # cam_ids with confirmed active detection
        self._avg_baseline: dict[int, np.ndarray | None] = {}  # cam_id -> running average frame
        self._baseline_frames: dict[int, int] = {}         # cam_id -> frame count since loop start

    def start(self, cameras: list) -> None:
        """Start motion detection for cameras with sensitivity > 0."""
        self._running = True
        rtsp_port = get_config().go2rtc.rtsp_port
        for cam in cameras:
            if cam.motion_sensitivity > 0 and (cam.sub_stream_url or cam.rtsp_url):
                url = _go2rtc_rtsp_url(cam.name, rtsp_port)
                task = asyncio.create_task(
                    self._detect_loop(
                        cam.id, cam.name, url,
                        cam.motion_sensitivity, cam.motion_script,
                        cam.motion_script_off,
                        ai_detection=cam.ai_detection,
                        ai_threshold=cam.ai_confidence_threshold / 100.0,
                    )
                )
                self._tasks[cam.id] = task
        if self._tasks:
            logger.info("Motion detector started", extra={"cameras": len(self._tasks)})

    async def stop(self) -> None:
        """Stop all detection loops."""
        self._running = False
        for task in self._tasks.values():
            task.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks.values(), return_exceptions=True)
        self._tasks.clear()
        self._executor.shutdown(wait=False)
        logger.info("Motion detector stopped")

    async def update_camera(self, camera) -> None:
        """Restart or stop detection when camera settings change."""
        cam_id = camera.id
        if cam_id in self._tasks:
            self._tasks[cam_id].cancel()
            try:
                await self._tasks[cam_id]
            except (asyncio.CancelledError, Exception):
                pass
            del self._tasks[cam_id]
            # Finalize any active event
            await self._finalize_event(cam_id)

        if self._running and camera.motion_sensitivity > 0 and camera.enabled:
            url = _go2rtc_rtsp_url(camera.name, get_config().go2rtc.rtsp_port)
            # Ensure object detector is started if AI detection is enabled
            if camera.ai_detection:
                detector = get_object_detector()
                await detector.start()

            task = asyncio.create_task(
                self._detect_loop(
                    camera.id, camera.name, url,
                    camera.motion_sensitivity, camera.motion_script,
                    camera.motion_script_off,
                    ai_detection=camera.ai_detection,
                    ai_threshold=camera.ai_confidence_threshold / 100.0,
                )
            )
            self._tasks[cam_id] = task
            logger.info("Motion detection restarted", extra={"camera": camera.name})

    async def _detect_loop(
        self, cam_id: int, cam_name: str, url: str,
        sensitivity: int, script: str | None, script_off: str | None = None,
        ai_detection: bool = False, ai_threshold: float = 0.5,
    ) -> None:
        """Per-camera detection loop. Runs until cancelled."""
        loop = asyncio.get_event_loop()
        threshold_pct = (101 - sensitivity) * 0.05

        logger.info(
            "Motion detection started",
            extra={"camera": cam_name, "sensitivity": sensitivity, "threshold_pct": round(threshold_pct, 2)},
        )

        while self._running:
            cap = None
            try:
                cap = await loop.run_in_executor(self._executor, self._open_capture, url)
                if cap is None:
                    logger.warning("Failed to open stream for motion detection", extra={"camera": cam_name})
                    await asyncio.sleep(5)
                    continue

                # Reset baseline on each (re)connect
                self._avg_baseline[cam_id] = None
                self._baseline_frames[cam_id] = 0

                while self._running:
                    frame = await loop.run_in_executor(self._executor, self._read_frame, cap)
                    if frame is None:
                        break  # reconnect

                    gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
                    gray = cv2.GaussianBlur(gray, BLUR_SIZE, 0)

                    self._baseline_frames[cam_id] = self._baseline_frames.get(cam_id, 0) + 1

                    if self._avg_baseline.get(cam_id) is None:
                        # First frame: establish baseline, nothing to diff yet
                        self._avg_baseline[cam_id] = gray.astype(np.float32)
                        await asyncio.sleep(1.0 / DETECT_FPS)
                        continue

                    avg = self._avg_baseline[cam_id]
                    diff = cv2.absdiff(gray, avg.astype(np.uint8))
                    _, thresh = cv2.threshold(diff, DIFF_THRESHOLD, 255, cv2.THRESH_BINARY)
                    changed_pct = (np.count_nonzero(thresh) / thresh.size) * 100

                    if changed_pct > MAX_CHANGE_PCT:
                        # Whole-frame change = camera artifact (IR switch, exposure)
                        # Hard-reset baseline and any in-progress AI confirmation
                        self._avg_baseline[cam_id] = gray.astype(np.float32)
                        self._baseline_frames[cam_id] = 0
                        if cam_id in self._score_history:
                            self._score_history[cam_id].clear()
                        self._disappeared[cam_id] = 0
                        self._confirmed.discard(cam_id)
                        await self._check_cooldown(cam_id)
                    elif changed_pct >= threshold_pct:
                        if ai_detection:
                            await self._process_ai_frame(
                                cam_id, cam_name, frame, changed_pct,
                                ai_threshold, script, script_off,
                            )
                        else:
                            await self._on_motion(cam_id, cam_name, changed_pct, script, script_off)
                    else:
                        # Below motion threshold
                        if ai_detection:
                            # Feed 0.0 to pull the median score down during quiet periods
                            await self._feed_score(
                                cam_id, cam_name, 0.0, None,
                                changed_pct, ai_threshold, script, script_off,
                            )
                        await self._check_cooldown(cam_id)

                    # Update running average baseline (skip on hard-reset frames)
                    if changed_pct <= MAX_CHANGE_PCT and self._avg_baseline.get(cam_id) is not None:
                        alpha = (
                            MOTION_ALPHA_STARTUP
                            if self._baseline_frames.get(cam_id, 0) <= BASELINE_STARTUP_FRAMES
                            else MOTION_ALPHA
                        )
                        self._avg_baseline[cam_id] = (
                            alpha * gray.astype(np.float32) + (1.0 - alpha) * self._avg_baseline[cam_id]
                        )

                    await asyncio.sleep(1.0 / DETECT_FPS)

            except asyncio.CancelledError:
                return
            except Exception:
                logger.exception("Motion detection error", extra={"camera": cam_name})
            finally:
                if cap is not None:
                    try:
                        await loop.run_in_executor(self._executor, cap.release)
                    except Exception:
                        pass

            # Reconnect delay
            if self._running:
                await asyncio.sleep(5)

    async def _process_ai_frame(
        self, cam_id: int, cam_name: str, frame, changed_pct: float,
        ai_threshold: float, script: str | None, script_off: str | None,
    ) -> None:
        """Run YOLO on a motion frame and feed the score into the pipeline."""
        detector = get_object_detector()
        detections = await detector.detect_persons(frame, ai_threshold)
        if detections:
            best = max(detections, key=lambda d: d.confidence)
            score = best.confidence
        else:
            best = None
            score = 0.0
        await self._feed_score(cam_id, cam_name, score, best,
                               changed_pct, ai_threshold, script, script_off)

    async def _feed_score(
        self, cam_id: int, cam_name: str, score: float, best_detection,
        changed_pct: float, ai_threshold: float,
        script: str | None, script_off: str | None,
    ) -> None:
        """Frigate-style scoring pipeline: median over rolling history, with disappeared tolerance.

        - Appends score to a per-camera deque(maxlen=SCORE_HISTORY_SIZE)
        - computed_score = median(history) — robust to single-frame jitter
        - Fires event when computed_score crosses ai_threshold (NOT confirmed yet → confirmed)
        - Tolerates MAX_DISAPPEARED consecutive missed frames before dropping confirmation
        """
        if cam_id not in self._score_history:
            self._score_history[cam_id] = deque(maxlen=SCORE_HISTORY_SIZE)

        self._score_history[cam_id].append(score)
        computed_score = float(np.median(list(self._score_history[cam_id])))

        if score == 0.0:
            self._disappeared[cam_id] = self._disappeared.get(cam_id, 0) + 1
        else:
            self._disappeared[cam_id] = 0

        disappeared = self._disappeared.get(cam_id, 0)

        if cam_id in self._confirmed:
            if disappeared >= MAX_DISAPPEARED:
                # Too many consecutive misses — drop confirmation, let cooldown end event
                self._confirmed.discard(cam_id)
                self._score_history[cam_id].clear()
                logger.debug(
                    "AI confirmation lost (disappeared)",
                    extra={"camera_id": cam_id, "disappeared": disappeared},
                )
            elif best_detection is not None:
                # Still confirmed and person visible: keep event alive with updated detection
                await self._on_motion(
                    cam_id, cam_name, changed_pct, script, script_off,
                    detection_label=best_detection.label,
                    detection_confidence=best_detection.confidence,
                )
            else:
                # Within MAX_DISAPPEARED tolerance: keep event alive without a DB update
                self._last_motion[cam_id] = datetime.now()
        else:
            if computed_score >= ai_threshold and best_detection is not None:
                # Median has crossed threshold — confirmed
                self._confirmed.add(cam_id)
                self._disappeared[cam_id] = 0
                logger.debug(
                    "AI confirmed",
                    extra={"camera_id": cam_id, "computed_score": round(computed_score, 2)},
                )
                await self._on_motion(
                    cam_id, cam_name, changed_pct, script, script_off,
                    detection_label=best_detection.label,
                    detection_confidence=best_detection.confidence,
                )
            else:
                await self._check_cooldown(cam_id)

    @staticmethod
    def _open_capture(url: str):
        """Open RTSP stream (blocking, runs in executor).

        Sets a 5s open/read timeout so reconnect dead windows are ~15s instead of ~90s.
        """
        cap = cv2.VideoCapture()
        cap.set(cv2.CAP_PROP_OPEN_TIMEOUT_MSEC, 5000)
        cap.set(cv2.CAP_PROP_READ_TIMEOUT_MSEC, 5000)
        if not cap.open(url, cv2.CAP_FFMPEG):
            return None
        return cap

    @staticmethod
    def _read_frame(cap):
        """Read a single frame (blocking, runs in executor)."""
        ret, frame = cap.read()
        return frame if ret else None

    async def _on_motion(
        self, cam_id: int, cam_name: str, intensity: float,
        script: str | None, script_off: str | None = None,
        detection_label: str | None = None, detection_confidence: float | None = None,
    ) -> None:
        """Handle detected motion - create or update event."""
        now = datetime.now()
        self._last_motion[cam_id] = now

        if cam_id not in self._active_events:
            # Start new motion event
            factory = get_session_factory()
            async with factory() as session:
                event = MotionEvent(
                    camera_id=cam_id,
                    start_time=now,
                    peak_intensity=intensity,
                    detection_label=detection_label,
                    detection_confidence=detection_confidence,
                )
                session.add(event)
                await session.commit()
                await session.refresh(event)
                self._active_events[cam_id] = event.id
                self._script_off[cam_id] = script_off

            log_extra = {"camera": cam_name, "intensity": round(intensity, 2)}
            if detection_label:
                log_extra["detection"] = detection_label
                log_extra["confidence"] = round(detection_confidence, 2)
            logger.info("Motion started", extra=log_extra)

            # Execute script if configured
            if script:
                asyncio.create_task(self._run_script(
                    script, cam_name, now, intensity,
                    detection_label, detection_confidence,
                ))
        else:
            # Update peak intensity and detection confidence if higher
            factory = get_session_factory()
            async with factory() as session:
                event = await session.get(MotionEvent, self._active_events[cam_id])
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
        """End motion event if cooldown has elapsed."""
        if cam_id not in self._active_events:
            return

        last = self._last_motion.get(cam_id)
        if last and (datetime.now() - last).total_seconds() >= COOLDOWN_SECONDS:
            await self._finalize_event(cam_id)

    async def _finalize_event(self, cam_id: int) -> None:
        """Close an active motion event."""
        event_id = self._active_events.pop(cam_id, None)
        self._last_motion.pop(cam_id, None)
        script_off = self._script_off.pop(cam_id, None)

        # Clear Frigate-style tracking state
        self._confirmed.discard(cam_id)
        self._score_history.pop(cam_id, None)
        self._disappeared.pop(cam_id, None)

        if event_id is None:
            return

        factory = get_session_factory()
        async with factory() as session:
            event = await session.get(MotionEvent, event_id)
            if event:
                event.end_time = datetime.now()
                await session.commit()

        logger.info("Motion ended", extra={"camera_id": cam_id, "event_id": event_id})

        if script_off:
            asyncio.create_task(self._run_script(script_off, str(cam_id), datetime.now(), 0.0))

    async def _run_script(
        self, script: str, cam_name: str, timestamp: datetime, intensity: float,
        detection_label: str | None = None, detection_confidence: float | None = None,
    ) -> None:
        """Execute the configured motion script with environment variables."""
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
                script,
                env=env,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE,
            )
            _, stderr = await asyncio.wait_for(proc.communicate(), timeout=30)
            if proc.returncode != 0:
                err_msg = stderr.decode(errors="replace")[-200:].strip()
                logger.warning(
                    "Motion script failed (rc=%d): %s", proc.returncode, err_msg,
                )
        except asyncio.TimeoutError:
            logger.warning("Motion script timed out", extra={"script": script})
        except Exception:
            logger.exception("Motion script error")


_detector: MotionDetector | None = None


def get_motion_detector() -> MotionDetector:
    """Get or create the singleton motion detector."""
    global _detector
    if _detector is None:
        _detector = MotionDetector()
    return _detector
