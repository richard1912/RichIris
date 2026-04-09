"""Real-time thumbnail capture service.

Captures JPEG snapshots from go2rtc's frame API at configurable intervals.
Thumbnails are stored as individual files in
{thumbnails_dir}/{camera_name}/{YYYY-MM-DD}/thumbs/thumb_HHMMSS.jpg
"""

import asyncio
import logging
from datetime import datetime
from pathlib import Path

import httpx

from app.config import get_config
from app.services.ffmpeg import sanitize_camera_name
from app.services.go2rtc_client import get_stream_name

logger = logging.getLogger(__name__)


class ThumbnailCapture:
    """Captures periodic JPEG thumbnails from go2rtc live streams."""

    def __init__(self):
        self._tasks: list[asyncio.Task] = []
        self._running = False
        self._client: httpx.AsyncClient | None = None

    def start(self, cameras: list) -> None:
        config = get_config()
        if not config.trickplay.enabled:
            logger.info("Trickplay disabled, skipping thumbnail capture")
            return
        self._running = True
        self._client = httpx.AsyncClient(timeout=15)
        # Stagger camera starts to avoid concurrent go2rtc stream creation
        for i, cam in enumerate(cameras):
            task = asyncio.create_task(self._capture_loop(cam, startup_delay=i * 2))
            self._tasks.append(task)
        logger.info("Thumbnail capture started", extra={"cameras": len(cameras)})

    def add_camera(self, camera) -> None:
        """Start thumbnail capture for a newly added camera."""
        if not self._running:
            return
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=15)
        task = asyncio.create_task(self._capture_loop(camera, startup_delay=2))
        self._tasks.append(task)
        logger.info("Thumbnail capture added camera", extra={"camera": camera.name})

    def remove_camera(self, camera_name: str) -> None:
        """Stop thumbnail capture for a removed camera (handled by task cancellation on next stop/restart)."""
        pass  # Tasks check self._running; full cleanup happens in stop()

    async def stop(self) -> None:
        self._running = False
        for task in self._tasks:
            task.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks = []
        if self._client:
            await self._client.aclose()
            self._client = None
        logger.info("Thumbnail capture stopped")

    async def _capture_loop(self, camera, startup_delay: float = 0) -> None:
        if startup_delay > 0:
            await asyncio.sleep(startup_delay)
        config = get_config()
        tp = config.trickplay
        safe_name = sanitize_camera_name(camera.name)
        stream_name = get_stream_name(camera.name)
        go2rtc_base = f"http://{config.go2rtc.host}:{config.go2rtc.port}"
        # Use sub-stream direct for thumbnails — lightweight, no transcode
        snapshot_url = (
            f"{go2rtc_base}/api/frame.jpeg"
            f"?src={stream_name}_s2_direct"
            f"&width={tp.thumb_width}&height={tp.thumb_height}"
        )

        while self._running:
            try:
                await asyncio.sleep(tp.interval)
            except asyncio.CancelledError:
                return

            now = datetime.now()
            date_str = now.strftime("%Y-%m-%d")
            time_str = now.strftime("%H%M%S")

            thumbs_dir = Path(config.storage.thumbnails_dir) / safe_name / date_str / "thumbs"
            thumbs_dir.mkdir(parents=True, exist_ok=True)
            out_path = thumbs_dir / f"thumb_{time_str}.jpg"

            try:
                from app.services.go2rtc_client import get_snapshot_semaphore, wait_for_go2rtc_ready
                await wait_for_go2rtc_ready()
                async with get_snapshot_semaphore():
                    resp = await self._client.get(snapshot_url)
                if resp.status_code != 200:
                    logger.debug("Snapshot request failed", extra={
                        "camera": camera.name, "status": resp.status_code,
                    })
                    continue

                data = resp.content
                if len(data) < 2000:
                    logger.debug("Snapshot too small, skipping", extra={
                        "camera": camera.name, "size": len(data),
                    })
                    continue

                out_path.write_bytes(data)
                logger.debug("Captured thumbnail", extra={
                    "camera": camera.name,
                    "path": str(out_path),
                    "size": len(data),
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
            # Parse HHMMSS from filename
            name = f.stem  # thumb_HHMMSS
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
