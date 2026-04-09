"""Real-time thumbnail capture service.

Uses ffmpeg to grab JPEG snapshots from go2rtc's local RTSP relay.
No new camera connections — piggybacks on existing go2rtc keepalives.
Thumbnails are stored as individual files in
{thumbnails_dir}/{camera_name}/{YYYY-MM-DD}/thumbs/thumb_HHMMSS.jpg
"""

import asyncio
import logging
from datetime import datetime
from pathlib import Path

from app.config import get_config
from app.services.ffmpeg import sanitize_camera_name
from app.services.go2rtc_client import get_stream_name

logger = logging.getLogger(__name__)


class ThumbnailCapture:
    """Captures periodic JPEG thumbnails via ffmpeg from go2rtc's local RTSP relay."""

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
        """Start thumbnail capture for a newly added camera."""
        if not self._running:
            return
        task = asyncio.create_task(self._capture_loop(camera, startup_delay=5))
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
        logger.info("Thumbnail capture stopped")

    async def _grab_frame(self, rtsp_url: str, out_path: Path, width: int, height: int) -> bool:
        """Use ffmpeg to grab a single JPEG frame from go2rtc's local RTSP relay."""
        config = get_config()
        cmd = [
            config.ffmpeg.path,
            "-rtsp_transport", "tcp",
            "-timeout", "5000000",  # 5s connection timeout
            "-i", rtsp_url,
            "-frames:v", "1",
            "-vf", f"scale={width}:{height}",
            "-q:v", "5",
            "-y",
            str(out_path),
        ]
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            await asyncio.wait_for(proc.communicate(), timeout=10)
            return proc.returncode == 0 and out_path.exists() and out_path.stat().st_size > 1000
        except asyncio.TimeoutError:
            try:
                proc.kill()
            except Exception:
                pass
            return False
        except Exception:
            return False

    async def _capture_loop(self, camera, startup_delay: float = 0) -> None:
        if startup_delay > 0:
            await asyncio.sleep(startup_delay)
        config = get_config()
        tp = config.trickplay
        safe_name = sanitize_camera_name(camera.name)
        stream_name = get_stream_name(camera.name)

        # Connect to go2rtc's local RTSP relay — reuses existing keepalive connection
        # to the camera, no new RTSP connections made. Try sub-stream first (lighter).
        from app.services.go2rtc_manager import get_rtsp_port
        rtsp_port = get_rtsp_port()
        relay_urls = [
            f"rtsp://127.0.0.1:{rtsp_port}/{stream_name}_s2_direct",
            f"rtsp://127.0.0.1:{rtsp_port}/{stream_name}_s1_direct",
        ]
        url_index = 0
        consecutive_failures = 0
        url_failures = 0

        logger.info("Thumbnail capture loop started", extra={
            "camera": camera.name, "relay": relay_urls[0],
        })

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
                ok = await self._grab_frame(relay_urls[url_index], out_path, tp.thumb_width, tp.thumb_height)
                if not ok:
                    if out_path.exists():
                        out_path.unlink(missing_ok=True)
                    consecutive_failures += 1
                    url_failures += 1
                    # Fall back to main stream after 10 sub-stream failures
                    if url_failures >= 10 and url_index == 0:
                        url_index = 1
                        url_failures = 0
                        logger.info("Thumbnail falling back to main stream relay", extra={
                            "camera": camera.name,
                        })
                    if consecutive_failures == 1 or consecutive_failures % 30 == 0:
                        logger.debug("Thumbnail capture failed", extra={
                            "camera": camera.name,
                            "consecutive_failures": consecutive_failures,
                            "stream": "s2" if url_index == 0 else "s1",
                        })
                    continue

                consecutive_failures = 0
                url_failures = 0
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
