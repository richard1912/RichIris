"""Real-time thumbnail capture service.

Reads frames from the shared FrameBroker (persistent ffmpeg against go2rtc's
sub-stream relay — no new camera connections) and writes scaled JPEG thumbnails
at the configured interval.

Thumbnails are stored as individual files in
{thumbnails_dir}/{camera_name}/{YYYY-MM-DD}/thumbs/thumb_HHMMSS.jpg
"""

import asyncio
import logging
from datetime import datetime
from pathlib import Path

import cv2

from app.config import get_config
from app.services.ffmpeg import sanitize_camera_name
from app.services.frame_broker import get_frame_broker

logger = logging.getLogger(__name__)


class ThumbnailCapture:
    """Periodically saves JPEG thumbnails from the shared frame broker."""

    def __init__(self):
        self._tasks: list[asyncio.Task] = []
        self._running = False

    def start(self, cameras: list) -> None:
        config = get_config()
        if not config.trickplay.enabled:
            logger.info("Trickplay disabled, skipping thumbnail capture")
            return
        self._running = True
        for i, cam in enumerate(cameras):
            task = asyncio.create_task(self._capture_loop(cam, startup_delay=i * 2))
            self._tasks.append(task)
        logger.info("Thumbnail capture started", extra={"cameras": len(cameras)})

    def add_camera(self, camera) -> None:
        if not self._running:
            return
        task = asyncio.create_task(self._capture_loop(camera, startup_delay=5))
        self._tasks.append(task)
        logger.info("Thumbnail capture added camera", extra={"camera": camera.name})

    def remove_camera(self, camera_name: str) -> None:
        pass  # Tasks check self._running; full cleanup happens in stop()

    async def stop(self) -> None:
        self._running = False
        for task in self._tasks:
            task.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks = []
        logger.info("Thumbnail capture stopped")

    async def _capture_loop(self, camera, startup_delay: float = 0) -> None:
        if startup_delay > 0:
            await asyncio.sleep(startup_delay)
        config = get_config()
        tp = config.trickplay
        safe_name = sanitize_camera_name(camera.name)
        broker = get_frame_broker()
        consecutive_failures = 0

        logger.info("Thumbnail capture loop started", extra={"camera": camera.name})

        while self._running:
            try:
                await asyncio.sleep(tp.interval)
            except asyncio.CancelledError:
                return

            now = datetime.now()
            date_str = now.strftime("%Y-%m-%d")
            time_str = now.strftime("%H%M%S")
            thumbs_dir = Path(config.storage.thumbnails_dir) / safe_name / date_str / "thumbs"
            out_path = thumbs_dir / f"thumb_{time_str}.jpg"

            try:
                frame = broker.get_latest(camera.id)
                if frame is None:
                    consecutive_failures += 1
                    if consecutive_failures == 1 or consecutive_failures % 30 == 0:
                        logger.debug("Thumbnail capture failed", extra={
                            "camera": camera.name,
                            "consecutive_failures": consecutive_failures,
                        })
                    continue

                consecutive_failures = 0
                thumbs_dir.mkdir(parents=True, exist_ok=True)
                resized = cv2.resize(
                    frame, (tp.thumb_width, tp.thumb_height), interpolation=cv2.INTER_AREA
                )
                cv2.imwrite(str(out_path), resized, [cv2.IMWRITE_JPEG_QUALITY, 75])

                logger.debug("Captured thumbnail", extra={
                    "camera": camera.name,
                    "path": str(out_path),
                    "size": out_path.stat().st_size,
                })

            except asyncio.CancelledError:
                return
            except Exception:
                logger.exception("Thumbnail capture error", extra={"camera": camera.name})

    def get_thumbnails_for_date(self, camera_name: str, date_str: str) -> list[dict]:
        """Scan filesystem and return sorted list of thumbnails for a camera/date."""
        config = get_config()
        safe_name = sanitize_camera_name(camera_name)
        thumbs_dir = Path(config.storage.thumbnails_dir) / safe_name / date_str / "thumbs"

        if not thumbs_dir.exists():
            return []

        thumbnails = []
        for f in sorted(thumbs_dir.glob("thumb_*.jpg")):
            name = f.stem
            time_part = name.replace("thumb_", "")
            if len(time_part) == 6:
                thumbnails.append({
                    "timestamp": f"{time_part[0:2]}:{time_part[2:4]}:{time_part[4:6]}",
                    "filename": f.name,
                })

        return thumbnails


_capture: ThumbnailCapture | None = None


def get_thumbnail_capture() -> ThumbnailCapture:
    global _capture
    if _capture is None:
        _capture = ThumbnailCapture()
    return _capture
