"""FFmpeg command builder - composable functions for building ffmpeg commands."""

import asyncio
import json
import logging
from pathlib import Path

from app.config import AppConfig

logger = logging.getLogger(__name__)


def build_input_args(rtsp_url: str, config: AppConfig, hwaccel: bool = True) -> list[str]:
    """Build ffmpeg input arguments for an RTSP source."""
    args = [config.ffmpeg.path]
    if hwaccel and config.ffmpeg.hwaccel:
        args.extend(["-hwaccel", config.ffmpeg.hwaccel])
    # timeout: socket I/O timeout in microseconds — forces ffmpeg to exit
    # if the camera stops sending data, so the process monitor can restart it.
    timeout = str(config.ffmpeg.rtsp_timeout_us)
    args.extend([
        "-rtsp_transport", config.ffmpeg.rtsp_transport,
        "-timeout", timeout,
        "-i", rtsp_url,
    ])
    logger.debug("Built input args", extra={"rtsp_url": rtsp_url, "timeout_us": timeout})
    return args


def build_recording_output(camera_name: str, config: AppConfig) -> list[str]:
    """Build ffmpeg output args for segment-based recording (codec passthrough)."""
    safe_name = sanitize_camera_name(camera_name)
    rec_dir = Path(config.storage.recordings_dir) / safe_name
    segment_pattern = str(rec_dir / "%Y-%m-%d" / "rec_%H-%M-%S.ts")

    args = [
        "-map", "0:v",
        "-map", "0:a?",
        "-c:v", "copy",
        "-c:a", "copy",
        "-f", "segment",
        "-segment_time", str(config.ffmpeg.segment_duration),
        "-segment_atclocktime", "1",
        "-reset_timestamps", "1",
        "-strftime", "1",
        segment_pattern,
    ]
    logger.debug("Built recording output", extra={"camera": camera_name, "pattern": segment_pattern})
    return args


def build_recording_command(camera_name: str, rtsp_url: str, config: AppConfig) -> list[str]:
    """Build ffmpeg command for recording only (passthrough, no transcode)."""
    cmd = build_input_args(rtsp_url, config, hwaccel=False)
    cmd.extend(build_recording_output(camera_name, config))
    logger.info("Built recording-only ffmpeg command", extra={"camera": camera_name})
    return cmd


def sanitize_camera_name(name: str) -> str:
    """Convert camera name to a filesystem-safe directory name (preserves spaces and case)."""
    return name.replace("/", "_").replace("\\", "_").replace(":", "_")


def build_probe_command(rtsp_url: str, config: AppConfig) -> list[str]:
    """Build ffprobe command to detect stream properties."""
    return [
        config.ffmpeg.ffprobe_path,
        "-v", "quiet",
        "-print_format", "json",
        "-show_streams",
        "-rtsp_transport", config.ffmpeg.rtsp_transport,
        rtsp_url,
    ]


async def probe_video_codec(rtsp_url: str, config: AppConfig) -> str | None:
    """Probe an RTSP URL and return the video codec name (e.g. 'h264', 'hevc')."""
    cmd = build_probe_command(rtsp_url, config)
    logger.debug("Probing video codec", extra={"url": rtsp_url})
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=15)
        data = json.loads(stdout.decode())
        for stream in data.get("streams", []):
            if stream.get("codec_type") == "video":
                codec = stream.get("codec_name", "").lower()
                logger.info("Probed video codec", extra={"url": rtsp_url, "codec": codec})
                return codec
    except Exception:
        logger.exception("Failed to probe video codec", extra={"url": rtsp_url})
    return None
