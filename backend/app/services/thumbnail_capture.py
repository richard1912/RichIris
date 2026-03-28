"""Real-time thumbnail capture service.

Extracts a single JPEG frame from the latest recording segment on disk at
configurable intervals. Thumbnails are stored as individual files alongside
recordings in {recordings_dir}/{camera_name}/{YYYY-MM-DD}/thumbs/thumb_HHMMSS.jpg
"""

import asyncio
import logging
from datetime import datetime
from pathlib import Path

from app.config import get_config
from app.services.ffmpeg import sanitize_camera_name
from app.services.job_object import assign_to_job

logger = logging.getLogger(__name__)


class ThumbnailCapture:
    """Captures periodic JPEG thumbnails from the latest recording segment."""

    def __init__(self):
        self._tasks: list[asyncio.Task] = []
        self._running = False

    def start(self, cameras: list) -> None:
        config = get_config()
        if not config.trickplay.enabled:
            logger.info("Trickplay disabled, skipping thumbnail capture")
            return
        self._running = True
        for cam in cameras:
            task = asyncio.create_task(self._capture_loop(cam))
            self._tasks.append(task)
        logger.info("Thumbnail capture started", extra={"cameras": len(cameras)})

    async def stop(self) -> None:
        self._running = False
        for task in self._tasks:
            task.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks = []
        logger.info("Thumbnail capture stopped")

    def _find_latest_segment(self, safe_name: str) -> Path | None:
        """Find the most recent .ts segment file for a camera."""
        config = get_config()
        cam_dir = Path(config.storage.recordings_dir) / safe_name
        if not cam_dir.exists():
            return None

        # Look in date directories, newest first
        date_dirs = sorted(
            [d for d in cam_dir.iterdir() if d.is_dir() and d.name != "thumbs"],
            reverse=True,
        )
        for date_dir in date_dirs[:2]:  # Only check today + yesterday
            segments = sorted(date_dir.glob("*.ts"), key=lambda p: p.stat().st_mtime, reverse=True)
            if segments:
                return segments[0]
        return None

    async def _capture_loop(self, camera) -> None:
        config = get_config()
        tp = config.trickplay
        safe_name = sanitize_camera_name(camera.name)

        while self._running:
            try:
                await asyncio.sleep(tp.interval)
            except asyncio.CancelledError:
                return

            now = datetime.now()
            date_str = now.strftime("%Y-%m-%d")
            time_str = now.strftime("%H%M%S")

            # Find latest recording segment to extract frame from
            segment = self._find_latest_segment(safe_name)
            if not segment:
                logger.debug("No segment found for thumbnail", extra={"camera": camera.name})
                continue

            thumbs_dir = Path(config.storage.recordings_dir) / safe_name / date_str / "thumbs"
            thumbs_dir.mkdir(parents=True, exist_ok=True)
            out_path = thumbs_dir / f"thumb_{time_str}.jpg"

            # Extract recent frame from segment (seek near end for freshest frame)
            cmd = [
                config.ffmpeg.path,
                "-sseof", "-3",
                "-i", str(segment),
                "-frames:v", "1",
                "-update", "1",
                "-vf", f"scale={tp.thumb_width}:{tp.thumb_height}",
                "-q:v", "5",
                "-y",
                str(out_path),
            ]

            try:
                proc = await asyncio.create_subprocess_exec(
                    *cmd,
                    stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.PIPE,
                )
                assign_to_job(proc.pid)
                _, stderr = await asyncio.wait_for(proc.communicate(), timeout=30)

                if proc.returncode != 0:
                    logger.debug("Thumbnail capture failed", extra={
                        "camera": camera.name,
                        "returncode": proc.returncode,
                        "stderr": stderr.decode(errors="replace")[-200:],
                    })
                    continue

                # Reject blank frames (< 2KB)
                if out_path.exists() and out_path.stat().st_size < 2000:
                    out_path.unlink()
                    logger.debug("Rejected blank thumbnail", extra={"camera": camera.name})
                    continue

                if out_path.exists():
                    logger.debug("Captured thumbnail", extra={
                        "camera": camera.name,
                        "path": str(out_path),
                        "size": out_path.stat().st_size,
                    })

            except asyncio.TimeoutError:
                logger.warning("Thumbnail capture timed out", extra={"camera": camera.name})
            except asyncio.CancelledError:
                return
            except Exception:
                logger.exception("Thumbnail capture error", extra={"camera": camera.name})

    def get_thumbnails_for_date(self, camera_name: str, date_str: str) -> list[dict]:
        """Scan filesystem and return sorted list of thumbnails for a camera/date."""
        config = get_config()
        safe_name = sanitize_camera_name(camera_name)
        thumbs_dir = Path(config.storage.recordings_dir) / safe_name / date_str / "thumbs"

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
