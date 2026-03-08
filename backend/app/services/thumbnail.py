"""Trickplay thumbnail sprite generator for recording segments."""

import asyncio
import logging
import math
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_config
from app.models import Recording

logger = logging.getLogger(__name__)

DATA_DIR = Path("C:/01-Self-Hosting/RichIris/data/thumbnails")


class ThumbnailGenerator:
    """Generates sprite sheet thumbnails from recording segments."""

    def __init__(self):
        self._queue: asyncio.Queue[int] = asyncio.Queue()
        self._worker_task: asyncio.Task | None = None
        self._running = False

    def start(self) -> None:
        config = get_config()
        if not config.trickplay.enabled:
            logger.info("Trickplay disabled, skipping thumbnail generator")
            return
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        self._running = True
        self._worker_task = asyncio.create_task(self._worker())
        logger.info("Thumbnail generator started")

    async def stop(self) -> None:
        self._running = False
        if self._worker_task:
            self._worker_task.cancel()
            try:
                await self._worker_task
            except asyncio.CancelledError:
                pass
            self._worker_task = None
        logger.info("Thumbnail generator stopped")

    def enqueue(self, recording_id: int) -> None:
        config = get_config()
        if not config.trickplay.enabled:
            return
        self._queue.put_nowait(recording_id)

    async def _worker(self) -> None:
        from app.database import get_session_factory

        while self._running:
            try:
                recording_id = await self._queue.get()
            except asyncio.CancelledError:
                return

            try:
                factory = get_session_factory()
                async with factory() as session:
                    await self._generate(session, recording_id)
            except Exception:
                logger.exception("Thumbnail generation failed", extra={"recording_id": recording_id})

    async def _generate(self, session: AsyncSession, recording_id: int) -> None:
        recording = await session.get(Recording, recording_id)
        if not recording:
            logger.debug("Recording not found for thumbnail", extra={"recording_id": recording_id})
            return

        if recording.has_thumbnail:
            return

        seg_path = Path(recording.file_path)
        if not seg_path.exists():
            logger.debug("Segment file missing for thumbnail", extra={"path": str(seg_path)})
            return

        config = get_config()
        tp = config.trickplay
        duration = recording.duration or 900.0
        frame_count = max(1, int(duration / tp.interval))
        cols = min(frame_count, 10)
        rows = math.ceil(frame_count / cols)

        out_dir = DATA_DIR / str(recording.camera_id)
        out_dir.mkdir(parents=True, exist_ok=True)
        out_path = out_dir / f"{recording_id}.jpg"

        ffmpeg_path = config.ffmpeg.path
        cmd = [
            ffmpeg_path,
            "-hwaccel", "cuda",
            "-i", str(seg_path),
            "-vf", f"fps=1/{tp.interval},scale={tp.thumb_width}:{tp.thumb_height},tile={cols}x{rows}",
            "-frames:v", "1",
            "-q:v", "5",
            "-y",
            str(out_path),
        ]

        logger.debug("Generating thumbnail sprite", extra={
            "recording_id": recording_id, "frames": frame_count, "grid": f"{cols}x{rows}",
        })

        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await proc.communicate()

        if proc.returncode != 0:
            logger.error("FFmpeg thumbnail failed", extra={
                "recording_id": recording_id, "returncode": proc.returncode,
                "stderr": stderr.decode(errors="replace")[-500:],
            })
            return

        recording.has_thumbnail = True
        await session.commit()
        logger.info("Thumbnail sprite generated", extra={
            "recording_id": recording_id,
            "size": out_path.stat().st_size,
        })

    def get_sprite_path(self, camera_id: int, recording_id: int) -> Path | None:
        path = DATA_DIR / str(camera_id) / f"{recording_id}.jpg"
        return path if path.exists() else None

    def delete_sprite(self, camera_id: int, recording_id: int) -> None:
        path = DATA_DIR / str(camera_id) / f"{recording_id}.jpg"
        if path.exists():
            try:
                path.unlink()
                logger.debug("Deleted thumbnail sprite", extra={"path": str(path)})
            except OSError:
                logger.exception("Failed to delete sprite", extra={"path": str(path)})


_generator: ThumbnailGenerator | None = None


def get_thumbnail_generator() -> ThumbnailGenerator:
    global _generator
    if _generator is None:
        _generator = ThumbnailGenerator()
    return _generator
