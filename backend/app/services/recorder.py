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

# Files that have failed ffprobe — skip on future scans to avoid repeated attempts
_unprobeble_paths: set[str] = set()


def _is_in_progress(seg_path: Path) -> bool:
    """Check if a segment file is still being written by ffmpeg.

    The segment muxer writes to rec_HH-MM-SS.ts files. Our scanner renames
    completed segments to human-readable names. So any file still starting
    with rec_ is the active/in-progress segment.
    """
    return seg_path.stem.startswith("rec_")


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
    """Scan for unregistered recording segments and add them to the database.

    Also updates any previously-registered in-progress segments.
    """
    config = get_config()
    safe_name = sanitize_camera_name(camera_name)
    rec_dir = Path(config.storage.recordings_dir) / safe_name

    if not rec_dir.exists():
        return 0

    # Update existing in-progress segments first (may rename files)
    await _update_in_progress(session, camera_id, config, camera_name)

    # Fetch existing paths AFTER in-progress updates, so renamed files
    # are not mistakenly registered again as new segments
    existing = await _get_existing_paths(session, camera_id)

    new_segments = _find_new_segments(rec_dir, existing)

    registered = 0
    new_recordings = []
    for seg_path in new_segments:
        recording = await _register_segment(session, camera_id, seg_path, config, camera_name)
        if recording:
            registered += 1
            new_recordings.append(recording)

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
    return [s for s in all_segments if str(s) not in existing and str(s) not in _unprobeble_paths]


async def _update_in_progress(
    session: AsyncSession, camera_id: int, config: AppConfig, camera_name: str,
) -> None:
    """Update all in-progress recordings: finalize completed ones, update end_time for ongoing ones."""
    result = await session.execute(
        select(Recording).where(
            Recording.camera_id == camera_id,
            Recording.in_progress == True,
        )
    )
    in_progress_recs = result.scalars().all()
    if not in_progress_recs:
        return

    finalized_recs = []
    for rec in in_progress_recs:
        seg_path = Path(rec.file_path)
        if not seg_path.exists():
            # File gone — ffmpeg rotated to new segment and our path is stale
            # The renamed file should be picked up as a new segment
            await session.delete(rec)
            logger.debug("Removed stale in-progress recording", extra={"id": rec.id, "path": rec.file_path})
            continue

        if _is_in_progress(seg_path):
            # Check file modification time — if the file hasn't been written to
            # recently, the recording process died and we should finalize it
            stat = seg_path.stat()
            mtime = datetime.fromtimestamp(stat.st_mtime)
            stale_seconds = (datetime.now() - mtime).total_seconds()

            if stale_seconds > 120:
                # Recording process died — finalize with actual mtime as end_time
                duration = _probe_duration(seg_path, config)
                if duration:
                    rec.end_time = rec.start_time + timedelta(seconds=duration)
                    rec.duration = duration
                else:
                    rec.end_time = mtime
                    rec.duration = (mtime - rec.start_time).total_seconds()
                rec.file_size = stat.st_size
                rec.in_progress = False
                # Rename to human-readable format
                try:
                    new_path = _rename_segment(seg_path, camera_name, rec.start_time, rec.duration)
                    rec.file_path = str(new_path)
                except (PermissionError, OSError):
                    pass
                finalized_recs.append(rec)
                logger.info("Finalized stale in-progress recording",
                            extra={"id": rec.id, "stale_seconds": stale_seconds})
            else:
                # Still being actively written — update end_time to file mtime
                rec.duration = (mtime - rec.start_time).total_seconds()
                rec.end_time = mtime
                rec.file_size = stat.st_size
        else:
            # File was renamed (no longer rec_*) — should not happen since we
            # only rename in our code, but handle gracefully
            duration = _probe_duration(seg_path, config)
            if duration:
                rec.duration = duration
                rec.end_time = rec.start_time + timedelta(seconds=duration)
            rec.file_size = seg_path.stat().st_size
            rec.in_progress = False

    await session.commit()


async def _register_segment(
    session: AsyncSession, camera_id: int, seg_path: Path, config: AppConfig,
    camera_name: str = "",
) -> Recording | None:
    """Create a Recording entry for a segment file.

    In-progress segments (rec_* prefix) are registered with estimated duration.
    Completed segments are renamed and registered with probed duration.
    """
    try:
        start_time = _parse_segment_time(seg_path)
        file_size = seg_path.stat().st_size

        if _is_in_progress(seg_path):
            # In-progress segment — register with estimated duration
            now = datetime.now()
            elapsed = (now - start_time).total_seconds()
            if elapsed < 5:
                # Too fresh, skip (ffmpeg may not have written data yet)
                return None

            recording = Recording(
                camera_id=camera_id,
                file_path=str(seg_path),
                start_time=start_time,
                end_time=now,
                file_size=file_size,
                duration=elapsed,
                in_progress=True,
            )
        else:
            # Completed segment — probe and register
            duration = _probe_duration(seg_path, config)
            if duration is None:
                return None

            # Rename to human-readable format if still using old naming
            if seg_path.stem.startswith("rec_") and camera_name and duration:
                try:
                    seg_path = _rename_segment(seg_path, camera_name, start_time, duration)
                    file_size = seg_path.stat().st_size
                except PermissionError:
                    return None

            recording = Recording(
                camera_id=camera_id,
                file_path=str(seg_path),
                start_time=start_time,
                end_time=start_time + timedelta(seconds=duration),
                file_size=file_size,
                duration=duration,
                in_progress=False,
            )

        session.add(recording)
        logger.debug(
            "Registered segment",
            extra={
                "camera_id": camera_id,
                "path": str(seg_path),
                "size": file_size,
                "in_progress": recording.in_progress,
            },
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
        _unprobeble_paths.add(str(seg_path))
        logger.debug("Could not probe duration", extra={"path": str(seg_path)})
        return None
