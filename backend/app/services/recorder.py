"""Scans for new recording segments and registers them in the database."""

import json
import logging
import re
import subprocess
from datetime import datetime, timedelta
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import AppConfig, get_config
from app.models import Camera, Recording
from app.services.ffmpeg import sanitize_camera_name

logger = logging.getLogger(__name__)


async def cleanup_missing_recordings(session: AsyncSession) -> int:
    """Remove DB recordings whose files no longer exist on disk."""
    result = await session.execute(select(Recording))
    recordings = result.scalars().all()
    deleted = 0
    for rec in recordings:
        if not Path(rec.file_path).exists():
            await session.delete(rec)
            deleted += 1
            logger.debug("Removed orphan recording", extra={"id": rec.id, "path": rec.file_path})
    if deleted > 0:
        await session.commit()
        logger.info("Cleaned up missing recordings", extra={"deleted": deleted})
    return deleted


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
        recording = await _register_segment(session, camera_id, seg_path, config, camera_name)
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
    session: AsyncSession, camera_id: int, seg_path: Path, config: AppConfig,
    camera_name: str = "",
) -> Recording | None:
    """Create a Recording entry for a segment file. Renames completed segments to human-readable format."""
    try:
        start_time = _parse_segment_time(seg_path)
        file_size = seg_path.stat().st_size
        duration = _probe_duration(seg_path, config)

        # Skip in-progress segments (no duration means ffmpeg is still writing)
        if duration is None:
            return None

        # Rename to human-readable format if still using old naming
        if seg_path.stem.startswith("rec_") and camera_name and duration:
            seg_path = _rename_segment(seg_path, camera_name, start_time, duration)
            file_size = seg_path.stat().st_size

        recording = Recording(
            camera_id=camera_id,
            file_path=str(seg_path),
            start_time=start_time,
            end_time=start_time + timedelta(seconds=duration),
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


def _rename_segment(seg_path: Path, camera_name: str, start_time: datetime, duration: float) -> Path:
    """Rename a segment file to the human-readable format: Camera Name YYYY-MM-DD HH.MM - HH.MM.ts"""
    end_time = start_time + timedelta(seconds=duration)
    date_str = start_time.strftime("%Y-%m-%d")
    start_str = start_time.strftime("%H.%M")
    end_str = end_time.strftime("%H.%M")
    new_name = f"{camera_name} {date_str} {start_str} - {end_str}.ts"
    new_path = seg_path.parent / new_name
    seg_path.rename(new_path)
    logger.debug("Renamed segment", extra={"old": str(seg_path), "new": str(new_path)})
    return new_path


def _parse_segment_time(seg_path: Path) -> datetime:
    """Extract start time from segment filename.

    Supports two formats:
    - Old: rec_HH-MM-SS.ts in directory YYYY-MM-DD
    - New: Camera Name YYYY-MM-DD HH.MM - HH.MM.ts
    """
    filename = seg_path.stem
    if filename.startswith("rec_"):
        # Old format
        date_str = seg_path.parent.name
        time_part = filename.replace("rec_", "")
        dt_str = f"{date_str} {time_part.replace('-', ':')}"
        return datetime.strptime(dt_str, "%Y-%m-%d %H:%M:%S")

    # New format: extract YYYY-MM-DD HH.MM from filename
    match = re.search(r"(\d{4}-\d{2}-\d{2}) (\d{2})\.(\d{2})", filename)
    if match:
        date_str = match.group(1)
        hour = match.group(2)
        minute = match.group(3)
        return datetime.strptime(f"{date_str} {hour}:{minute}:00", "%Y-%m-%d %H:%M:%S")

    raise ValueError(f"Cannot parse segment time from: {filename}")


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
