"""FFmpeg command builder - composable functions for building ffmpeg commands."""

import asyncio
import json
import logging
import re
from pathlib import Path

from app.config import AppConfig

logger = logging.getLogger(__name__)

_RTSP_CREDS_RE = re.compile(r"(rtsp://)[^@]+@", re.IGNORECASE)


def _redact_rtsp(text: str) -> str:
    """Replace rtsp://user:pass@host with rtsp://***@host in log output."""
    return _RTSP_CREDS_RE.sub(r"\1***@", text)


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
    logger.debug("Built input args", extra={"rtsp_url": _redact_rtsp(rtsp_url), "timeout_us": timeout})
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
        "-muxdelay", "0",
        "-muxpreload", "0",
        "-strftime", "1",
        segment_pattern,
    ]
    logger.debug("Built recording output", extra={"camera": camera_name, "pattern": segment_pattern})
    return args


def build_recording_command(camera_name: str, rtsp_url: str, config: AppConfig) -> list[str]:
    """Build ffmpeg command for recording only (passthrough, no transcode).

    rtsp_url can be a camera RTSP URL or a go2rtc local RTSP URL
    (e.g. rtsp://127.0.0.1:8554/stream_name_s1_direct).
    """
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
    logger.debug("Probing video codec", extra={"url": _redact_rtsp(rtsp_url)})
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
                logger.info("Probed video codec", extra={"url": _redact_rtsp(rtsp_url), "codec": codec})
                return codec
    except Exception:
        logger.exception("Failed to probe video codec", extra={"url": _redact_rtsp(rtsp_url)})
    return None


async def probe_video_bitrate(rtsp_url: str, config: AppConfig, sample_seconds: int = 5) -> int | None:
    """Probe an RTSP stream's actual bitrate by sampling a few seconds of data.

    RTSP streams don't declare bitrate in metadata, so we capture a short sample
    and calculate bitrate from the data size. Returns bitrate in kbps.
    """
    import tempfile
    import os

    logger.debug("Probing video bitrate via sampling", extra={"url": _redact_rtsp(rtsp_url), "seconds": sample_seconds})
    tmp_path = None
    try:
        tmp_fd, tmp_path = tempfile.mkstemp(suffix=".ts")
        os.close(tmp_fd)

        cmd = [
            config.ffmpeg.path,
            "-y", "-rtsp_transport", config.ffmpeg.rtsp_transport,
            "-t", str(sample_seconds),
            "-i", rtsp_url,
            "-c", "copy",
            tmp_path,
        ]
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        await asyncio.wait_for(proc.communicate(), timeout=sample_seconds + 15)

        size_bytes = os.path.getsize(tmp_path)
        if size_bytes > 0:
            kbps = (size_bytes * 8) // (sample_seconds * 1000)
            logger.info("Probed video bitrate", extra={"url": _redact_rtsp(rtsp_url), "kbps": kbps})
            return kbps
    except Exception:
        logger.exception("Failed to probe video bitrate", extra={"url": _redact_rtsp(rtsp_url)})
    finally:
        if tmp_path:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
    return None
