"""Scans for new recording segments and registers them in the database."""

import json
import logging
import subprocess
from datetime import datetime
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import AppConfig, get_config
from app.models import Camera, Recording
from app.services.ffmpeg import sanitize_camera_name

logger = logging.getLogger(__name__)


async def scan_all_cameras(session: AsyncSession) -> int:
    """Scan all cameras for new recording segments."""
    result = await session.execute(select(Camera).where(Camera.enabled == True))
    cameras = result.scalars().all()
    total = 0
    for cam in cameras:
        count = await scan_new_segments(session, cam.id, cam.name)
        total += count
    return total


async def scan_new_segments(session: AsyncSession, camera_id: int, camera_name: str) -> int:
    """Scan for unregistered recording segments and add them to the database."""
    config = get_config()
    safe_name = sanitize_camera_name(camera_name)
    rec_dir = Path(config.storage.recordings_dir) / safe_name

    if not rec_dir.exists():
        return 0

    existing = await _get_existing_paths(session, camera_id)
    new_segments = _find_new_segments(rec_dir, existing)

    registered = 0
    for seg_path in new_segments:
        recording = await _register_segment(session, camera_id, seg_path, config)
        if recording:
            registered += 1

    if registered > 0:
        await session.commit()
        logger.info(
            "Registered new segments",
            extra={"camera_id": camera_id, "count": registered},
        )
    return registered


async def _get_existing_paths(session: AsyncSession, camera_id: int) -> set[str]:
    """Get all already-registered file paths for a camera."""
    result = await session.execute(
        select(Recording.file_path).where(Recording.camera_id == camera_id)
    )
    return {row[0] for row in result.all()}


def _find_new_segments(rec_dir: Path, existing: set[str]) -> list[Path]:
    """Find .ts files in the recording directory not yet registered."""
    all_segments = sorted(rec_dir.rglob("*.ts"))
    return [s for s in all_segments if str(s) not in existing]


async def _register_segment(
    session: AsyncSession, camera_id: int, seg_path: Path, config: AppConfig
) -> Recording | None:
    """Create a Recording entry for a segment file."""
    try:
        start_time = _parse_segment_time(seg_path)
        file_size = seg_path.stat().st_size
        duration = _probe_duration(seg_path, config)

        recording = Recording(
            camera_id=camera_id,
            file_path=str(seg_path),
            start_time=start_time,
            end_time=start_time if duration is None else None,
            file_size=file_size,
            duration=duration,
        )
        session.add(recording)
        logger.debug(
            "Registered segment",
            extra={"camera_id": camera_id, "path": str(seg_path), "size": file_size},
        )
        return recording
    except Exception:
        logger.exception("Failed to register segment", extra={"path": str(seg_path)})
        return None


def _parse_segment_time(seg_path: Path) -> datetime:
    """Extract start time from segment filename (rec_HH-MM-SS.ts) and parent dir (YYYY-MM-DD)."""
    date_str = seg_path.parent.name
    time_part = seg_path.stem.replace("rec_", "")
    dt_str = f"{date_str} {time_part.replace('-', ':')}"
    return datetime.strptime(dt_str, "%Y-%m-%d %H:%M:%S")


def _probe_duration(seg_path: Path, config: AppConfig) -> float | None:
    """Use ffprobe to get segment duration in seconds."""
    try:
        result = subprocess.run(
            [
                config.ffmpeg.ffprobe_path,
                "-v", "quiet",
                "-print_format", "json",
                "-show_format",
                str(seg_path),
            ],
            capture_output=True,
            text=True,
            timeout=10,
        )
        data = json.loads(result.stdout)
        return float(data["format"]["duration"])
    except Exception:
        logger.debug("Could not probe duration", extra={"path": str(seg_path)})
        return None
