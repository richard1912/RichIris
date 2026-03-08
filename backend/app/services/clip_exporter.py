"""Clip export service - extracts time ranges from recordings into MP4 files."""

import asyncio
import logging
from datetime import datetime, timedelta
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_config
from app.models import Camera, ClipExport, Recording
from app.services.ffmpeg import sanitize_camera_name
from app.services.job_object import assign_to_job

logger = logging.getLogger(__name__)

EXPORTS_DIR = Path("G:/RichIris/exports")


def get_exports_dir() -> Path:
    """Return and ensure the exports directory exists."""
    EXPORTS_DIR.mkdir(parents=True, exist_ok=True)
    return EXPORTS_DIR


async def find_overlapping_segments(
    session: AsyncSession, camera_id: int, start: datetime, end: datetime
) -> list[Recording]:
    """Find all recording segments that overlap with [start, end]."""
    result = await session.execute(
        select(Recording)
        .where(Recording.camera_id == camera_id)
        .order_by(Recording.start_time)
    )
    all_segs = result.scalars().all()

    overlapping = []
    for seg in all_segs:
        seg_start = seg.start_time
        if seg.end_time:
            seg_end = seg.end_time
        elif seg.duration:
            seg_end = seg_start + timedelta(seconds=seg.duration)
        else:
            seg_end = seg_start + timedelta(seconds=900)

        if seg_start < end and seg_end > start:
            overlapping.append(seg)

    return overlapping


async def export_clip(clip_id: int, session_factory) -> None:
    """Run the ffmpeg export for a clip. Called as a background task."""
    config = get_config()

    async with session_factory() as session:
        clip = await session.get(ClipExport, clip_id)
        if not clip:
            logger.error("Clip not found", extra={"clip_id": clip_id})
            return

        clip.status = "processing"
        await session.commit()

        try:
            segments = await find_overlapping_segments(
                session, clip.camera_id, clip.start_time, clip.end_time
            )

            if not segments:
                clip.status = "failed"
                await session.commit()
                logger.warning("No segments found for clip", extra={"clip_id": clip_id})
                return

            exports_dir = get_exports_dir()
            camera = await session.get(Camera, clip.camera_id)
            cam_name = camera.name if camera else f"Camera {clip.camera_id}"
            date_str = clip.start_time.strftime("%Y-%m-%d")
            start_str = clip.start_time.strftime("%H.%M")
            end_str = clip.end_time.strftime("%H.%M")
            output_file = exports_dir / f"{cam_name} {date_str} {start_str} - {end_str}.mp4"

            # Build concat file
            concat_lines = []
            for seg in segments:
                seg_path = Path(seg.file_path)
                if seg_path.exists():
                    concat_lines.append(f"file '{seg_path.as_posix()}'")

            if not concat_lines:
                clip.status = "failed"
                await session.commit()
                logger.warning("No segment files on disk", extra={"clip_id": clip_id})
                return

            concat_file = exports_dir / f"_concat_{clip_id}.txt"
            concat_file.write_text("\n".join(concat_lines), encoding="utf-8")

            try:
                # Trim offsets relative to concatenated stream
                ss_offset = max(0, (clip.start_time - segments[0].start_time).total_seconds())
                clip_duration = (clip.end_time - clip.start_time).total_seconds()

                cmd = [
                    config.ffmpeg.path,
                    "-y",
                    "-f", "concat",
                    "-safe", "0",
                    "-i", str(concat_file),
                    "-ss", f"{ss_offset:.3f}",
                    "-t", f"{clip_duration:.3f}",
                    "-c", "copy",
                    "-movflags", "+faststart",
                    str(output_file),
                ]

                logger.info(
                    "Starting clip export",
                    extra={"clip_id": clip_id, "segments": len(segments), "output": str(output_file)},
                )

                proc = await asyncio.create_subprocess_exec(
                    *cmd,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                assign_to_job(proc.pid)
                _, stderr = await proc.communicate()

                if proc.returncode != 0:
                    logger.error(
                        "FFmpeg clip export failed",
                        extra={"clip_id": clip_id, "stderr": stderr.decode(errors="replace")[-500:]},
                    )
                    clip.status = "failed"
                    await session.commit()
                    return

                clip.status = "done"
                clip.file_path = str(output_file)
                await session.commit()
                logger.info("Clip export completed", extra={"clip_id": clip_id, "file": str(output_file)})

            finally:
                concat_file.unlink(missing_ok=True)

        except Exception:
            logger.exception("Clip export error", extra={"clip_id": clip_id})
            clip.status = "failed"
            await session.commit()
