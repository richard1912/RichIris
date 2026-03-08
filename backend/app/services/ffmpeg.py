"""FFmpeg command builder - composable functions for building ffmpeg commands."""

import logging
from pathlib import Path

from app.config import AppConfig

logger = logging.getLogger(__name__)


def build_input_args(rtsp_url: str, config: AppConfig, hwaccel: bool = True) -> list[str]:
    """Build ffmpeg input arguments for an RTSP source."""
    args = [config.ffmpeg.path]
    if hwaccel and config.ffmpeg.hwaccel:
        args.extend(["-hwaccel", config.ffmpeg.hwaccel])
    args.extend([
        "-rtsp_transport", config.ffmpeg.rtsp_transport,
        "-i", rtsp_url,
    ])
    logger.debug("Built input args", extra={"rtsp_url": rtsp_url, "hwaccel": config.ffmpeg.hwaccel if hwaccel else None})
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


def build_live_output(camera_name: str, config: AppConfig) -> list[str]:
    """Build ffmpeg output args for HLS live streaming (transcode to H.264 for browser compatibility)."""
    safe_name = sanitize_camera_name(camera_name)
    live_dir = Path(config.storage.live_dir) / safe_name
    playlist_path = str(live_dir / "stream.m3u8")

    args = [
        "-map", "0:v",
        "-map", "0:a?",
        "-vf", "scale=1920:-2",
        "-c:v", "libx264",
        "-preset", "veryfast",
        "-tune", "zerolatency",
        "-b:v", "2M",
        "-maxrate", "2.5M",
        "-bufsize", "4M",
        "-g", "48",
        "-c:a", "aac",
        "-b:a", "128k",
        "-f", "hls",
        "-hls_time", str(config.ffmpeg.hls_time),
        "-hls_list_size", str(config.ffmpeg.hls_list_size),
        "-hls_flags", "delete_segments+temp_file",
        "-hls_segment_filename", str(live_dir / "segment_%03d.ts"),
        playlist_path,
    ]
    logger.debug("Built live output", extra={"camera": camera_name, "playlist": playlist_path})
    return args


def build_recording_command(camera_name: str, rtsp_url: str, config: AppConfig) -> list[str]:
    """Build ffmpeg command for recording only (passthrough, no transcode)."""
    cmd = build_input_args(rtsp_url, config, hwaccel=False)
    cmd.extend(build_recording_output(camera_name, config))
    logger.info("Built recording-only ffmpeg command", extra={"camera": camera_name})
    return cmd


def build_live_command(camera_name: str, rtsp_url: str, config: AppConfig) -> list[str]:
    """Build ffmpeg command for live HLS output only (transcode)."""
    cmd = build_input_args(rtsp_url, config)
    cmd.extend(build_live_output(camera_name, config))
    logger.info("Built live-only ffmpeg command", extra={"camera": camera_name})
    return cmd


def build_full_command(camera_name: str, rtsp_url: str, config: AppConfig) -> list[str]:
    """Build the complete ffmpeg command for a camera (recording + live)."""
    cmd = build_input_args(rtsp_url, config)
    cmd.extend(build_recording_output(camera_name, config))
    cmd.extend(build_live_output(camera_name, config))
    logger.info("Built full ffmpeg command", extra={"camera": camera_name})
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
