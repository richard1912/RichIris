"""Trickplay thumbnail sprite generator for recording segments."""

import asyncio
import logging
import math
import shutil
import tempfile
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_config
from app.models import Recording
from app.services.job_object import assign_to_job

logger = logging.getLogger(__name__)

DATA_DIR = Path("C:/01-Self-Hosting/RichIris/data/thumbnails")


class ThumbnailGenerator:
    """Generates sprite sheet thumbnails from recording segments."""

    def __init__(self):
        self._queue: asyncio.Queue[int] = asyncio.Queue()
        self._worker_tasks: list[asyncio.Task] = []
        self._running = False

    def start(self) -> None:
        config = get_config()
        if not config.trickplay.enabled:
            logger.info("Trickplay disabled, skipping thumbnail generator")
            return
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        self._running = True
        num_workers = config.trickplay.workers
        for i in range(num_workers):
            task = asyncio.create_task(self._worker())
            self._worker_tasks.append(task)
        logger.info("Thumbnail generator started", extra={"workers": num_workers})

    async def stop(self) -> None:
        self._running = False
        for task in self._worker_tasks:
            task.cancel()
        if self._worker_tasks:
            try:
                await asyncio.gather(*self._worker_tasks, return_exceptions=True)
            except asyncio.CancelledError:
                pass
        self._worker_tasks = []
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

        # Create temp directory for frame extraction
        temp_dir = Path(tempfile.mkdtemp(prefix=f"thumb_{recording_id}_"))

        try:
            logger.debug("Generating thumbnail sprite", extra={
                "recording_id": recording_id, "frames": frame_count, "grid": f"{cols}x{rows}",
            })

            # Phase 1: Extract frames in parallel with fast seeks
            frame_paths = await self._extract_frames(
                seg_path, temp_dir, frame_count, tp.interval, tp.thumb_width, tp.thumb_height
            )

            if len(frame_paths) < frame_count:
                logger.warning("Failed to extract all frames", extra={
                    "recording_id": recording_id,
                    "expected": frame_count,
                    "extracted": len(frame_paths),
                })

            # Phase 2: Compose frames into sprite sheet
            await self._compose_sprite(frame_paths, out_path, cols, rows, tp.thumb_width, tp.thumb_height)

            recording.has_thumbnail = True
            await session.commit()
            logger.info("Thumbnail sprite generated", extra={
                "recording_id": recording_id,
                "size": out_path.stat().st_size,
            })

        finally:
            # Cleanup temp directory
            if temp_dir.exists():
                shutil.rmtree(temp_dir, ignore_errors=True)

    async def _extract_frames(
        self,
        seg_path: Path,
        temp_dir: Path,
        frame_count: int,
        interval: int,
        width: int,
        height: int,
    ) -> list[Path]:
        """Extract frames in parallel using fast seeks, return sorted list of frame paths."""
        config = get_config()
        ffmpeg_path = config.ffmpeg.path

        async def extract_frame(frame_idx: int) -> Path | None:
            timestamp = frame_idx * interval
            frame_path = temp_dir / f"frame_{frame_idx:03d}.jpg"

            cmd = [
                ffmpeg_path,
                "-ss", str(timestamp),
                "-i", str(seg_path),
                "-frames:v", "1",
                "-vf", f"scale={width}:{height}",
                "-q:v", "3",
                "-y",
                str(frame_path),
            ]

            try:
                proc = await asyncio.create_subprocess_exec(
                    *cmd,
                    stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.PIPE,
                )
                assign_to_job(proc.pid)
                _, stderr = await proc.communicate()

                if proc.returncode != 0:
                    logger.debug("Frame extraction failed", extra={
                        "frame_idx": frame_idx,
                        "returncode": proc.returncode,
                        "stderr": stderr.decode(errors="replace")[-200:],
                    })
                    return None

                return frame_path
            except Exception as e:
                logger.debug("Frame extraction exception", extra={"frame_idx": frame_idx, "error": str(e)})
                return None

        # Extract all frames concurrently
        tasks = [extract_frame(i) for i in range(frame_count)]
        results = await asyncio.gather(*tasks)

        # Return sorted list of successfully extracted frames
        return sorted([p for p in results if p is not None])

    async def _compose_sprite(
        self,
        frame_paths: list[Path],
        out_path: Path,
        cols: int,
        rows: int,
        width: int,
        height: int,
    ) -> None:
        """Compose frame JPEGs into a single sprite sheet."""
        config = get_config()
        ffmpeg_path = config.ffmpeg.path

        if not frame_paths:
            logger.error("No frames to compose", extra={"out_path": str(out_path)})
            return

        # Handle single frame edge case
        if len(frame_paths) == 1:
            # Just copy/scale the single frame
            cmd = [
                ffmpeg_path,
                "-i", str(frame_paths[0]),
                "-vf", f"scale={width}:{height}",
                "-frames:v", "1",
                "-q:v", "5",
                "-y",
                str(out_path),
            ]
        else:
            # Build filter graph with hstack/vstack
            filter_graph = self._build_tile_filter(len(frame_paths), cols, rows, width, height)

            inputs = [item for path in frame_paths for item in ["-i", str(path)]]

            cmd = [
                ffmpeg_path,
                *inputs,
                "-filter_complex", filter_graph,
                "-frames:v", "1",
                "-q:v", "5",
                "-y",
                str(out_path),
            ]

        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
        assign_to_job(proc.pid)
        _, stderr = await proc.communicate()

        if proc.returncode != 0:
            logger.error("Sprite composition failed", extra={
                "returncode": proc.returncode,
                "stderr": stderr.decode(errors="replace")[-500:],
            })

    def _build_tile_filter(self, frame_count: int, cols: int, rows: int, width: int, height: int) -> str:
        """Build hstack/vstack filter graph for tiling frames.

        Example for 4 frames (2x2):
        [0:v][1:v]hstack=inputs=2[row0];
        [2:v][3:v]hstack=inputs=2[row1];
        [row0][row1]vstack=inputs=2
        """
        if frame_count == 1:
            return "[0:v]copy"

        row_outputs = []
        frame_idx = 0

        # Build rows with hstack
        for row_idx in range(rows):
            frames_in_row = min(cols, frame_count - frame_idx)
            if frames_in_row == 0:
                break

            if frames_in_row == 1:
                # Single frame in row
                input_spec = f"[{frame_idx}:v]"
                output_name = f"[row{row_idx}]"
                row_outputs.append(f"{input_spec}copy{output_name}")
                frame_idx += 1
            else:
                # Multiple frames in row, use hstack
                inputs = "".join([f"[{frame_idx + i}:v]" for i in range(frames_in_row)])
                output_name = f"[row{row_idx}]"
                row_outputs.append(f"{inputs}hstack=inputs={frames_in_row}{output_name}")
                frame_idx += frames_in_row

        # Combine rows with vstack
        if len(row_outputs) == 1:
            return row_outputs[0]
        else:
            row_inputs = "".join([f"[row{i}]" for i in range(len(row_outputs))])
            filter_parts = row_outputs + [f"{row_inputs}vstack=inputs={len(row_outputs)}"]
            return ";".join(filter_parts)

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
