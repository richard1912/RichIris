"""Motion detection + AI person detection using go2rtc snapshots and YOLO."""

import asyncio
import logging
import os
from datetime import datetime

import cv2
import httpx
import numpy as np

from app.config import get_config
from app.database import get_session_factory
from app.models import MotionEvent
from app.services.go2rtc_client import get_stream_name
from app.services.object_detector import get_object_detector

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


def _snapshot_url(camera_name: str, host: str, port: int) -> str:
    """Return go2rtc snapshot URL for a camera's sub-stream."""
    stream_name = get_stream_name(camera_name) + "_s2_direct"
    return f"http://{host}:{port}/api/frame.jpeg?src={stream_name}"


class MotionDetector:
    def __init__(self):
        self._tasks: dict[int, asyncio.Task] = {}
        self._running = False
        self._active_events: dict[int, int] = {}
        self._last_motion: dict[int, datetime] = {}
        self._script_off: dict[int, str | None] = {}
        self._avg_baseline: dict[int, np.ndarray | None] = {}
        self._baseline_frames: dict[int, int] = {}
        self._client: httpx.AsyncClient | None = None

    def start(self, cameras: list) -> None:
        self._running = True
        self._client = httpx.AsyncClient(timeout=FRAME_TIMEOUT)
        cfg = get_config().go2rtc
        for cam in cameras:
            if cam.motion_sensitivity > 0 and (cam.sub_stream_url or cam.rtsp_url):
                url = _snapshot_url(cam.name, cfg.host, cfg.port)
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
            await self._finalize_event(cam_id)

        if self._running and camera.motion_sensitivity > 0 and camera.enabled:
            cfg = get_config().go2rtc
            url = _snapshot_url(camera.name, cfg.host, cfg.port)
            if camera.ai_detection:
                detector = get_object_detector()
                await detector.start()
            if self._client is None:
                self._client = httpx.AsyncClient(timeout=FRAME_TIMEOUT)
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

    async def _fetch_frame(self, url: str) -> np.ndarray | None:
        """Fetch a JPEG snapshot from go2rtc and decode it."""
        try:
            resp = await self._client.get(url)
            if resp.status_code != 200:
                return None
            return cv2.imdecode(np.frombuffer(resp.content, np.uint8), cv2.IMREAD_COLOR)
        except (httpx.TimeoutException, httpx.HTTPError):
            return None

    async def _detect_loop(
        self, cam_id: int, cam_name: str, url: str,
        sensitivity: int, script: str | None, script_off: str | None = None,
        ai_detection: bool = False, ai_threshold: float = 0.5,
    ) -> None:
        """Per-camera detection loop: snapshot → motion check → YOLO → event."""
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
                    await asyncio.sleep(POLL_INTERVAL)
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

    async def _on_motion(
        self, cam_id: int, cam_name: str, intensity: float,
        script: str | None, script_off: str | None = None,
        detection_label: str | None = None, detection_confidence: float | None = None,
    ) -> None:
        now = datetime.now()
        self._last_motion[cam_id] = now

        if cam_id not in self._active_events:
            factory = get_session_factory()
            async with factory() as session:
                event = MotionEvent(
                    camera_id=cam_id, start_time=now, peak_intensity=intensity,
                    detection_label=detection_label, detection_confidence=detection_confidence,
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

            if script:
                asyncio.create_task(self._run_script(
                    script, cam_name, now, intensity, detection_label, detection_confidence,
                ))
        else:
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
        if cam_id not in self._active_events:
            return
        last = self._last_motion.get(cam_id)
        if last and (datetime.now() - last).total_seconds() >= COOLDOWN_SECONDS:
            await self._finalize_event(cam_id)

    async def _finalize_event(self, cam_id: int) -> None:
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
