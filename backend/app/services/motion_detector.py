"""Motion detection service using OpenCV frame differencing on RTSP sub-streams."""

import asyncio
import logging
import os
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

import cv2
import numpy as np

from app.database import get_session_factory
from app.models import MotionEvent
from app.services.object_detector import get_object_detector

logger = logging.getLogger(__name__)

COOLDOWN_SECONDS = 10
DETECT_FPS = 2
BLUR_SIZE = (21, 21)
DIFF_THRESHOLD = 25
MAX_CHANGE_PCT = 40  # Ignore frames where >40% changed (IR switch, exposure shift)


class MotionDetector:
    def __init__(self):
        self._tasks: dict[int, asyncio.Task] = {}
        self._running = False
        self._executor = ThreadPoolExecutor(max_workers=6, thread_name_prefix="motion")
        self._active_events: dict[int, int] = {}  # camera_id -> motion_event DB id
        self._last_motion: dict[int, datetime] = {}  # camera_id -> last motion time
        self._script_off: dict[int, str | None] = {}  # camera_id -> off script

    def start(self, cameras: list) -> None:
        """Start motion detection for cameras with sensitivity > 0."""
        self._running = True
        for cam in cameras:
            if cam.motion_sensitivity > 0 and (cam.sub_stream_url or cam.rtsp_url):
                task = asyncio.create_task(
                    self._detect_loop(
                        cam.id, cam.name,
                        cam.sub_stream_url or cam.rtsp_url,
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
            url = camera.sub_stream_url or camera.rtsp_url
            if url:
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

                prev_gray = None
                while self._running:
                    frame = await loop.run_in_executor(self._executor, self._read_frame, cap)
                    if frame is None:
                        break  # reconnect

                    gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
                    gray = cv2.GaussianBlur(gray, BLUR_SIZE, 0)

                    if prev_gray is not None:
                        diff = cv2.absdiff(prev_gray, gray)
                        _, thresh = cv2.threshold(diff, DIFF_THRESHOLD, 255, cv2.THRESH_BINARY)
                        changed_pct = (np.count_nonzero(thresh) / thresh.size) * 100

                        if changed_pct > MAX_CHANGE_PCT:
                            # Whole-frame change = camera artifact (IR switch, exposure)
                            prev_gray = gray  # reset baseline to new lighting
                            await self._check_cooldown(cam_id)
                        elif changed_pct >= threshold_pct:
                            if ai_detection:
                                detector = get_object_detector()
                                detections = await detector.detect_persons(frame, ai_threshold)
                                if detections:
                                    best = max(detections, key=lambda d: d.confidence)
                                    await self._on_motion(
                                        cam_id, cam_name, changed_pct, script, script_off,
                                        detection_label=best.label,
                                        detection_confidence=best.confidence,
                                    )
                                else:
                                    await self._check_cooldown(cam_id)
                            else:
                                await self._on_motion(cam_id, cam_name, changed_pct, script, script_off)
                        else:
                            await self._check_cooldown(cam_id)

                    prev_gray = gray
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

    @staticmethod
    def _open_capture(url: str):
        """Open RTSP stream (blocking, runs in executor)."""
        cap = cv2.VideoCapture(url, cv2.CAP_FFMPEG)
        if not cap.isOpened():
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
