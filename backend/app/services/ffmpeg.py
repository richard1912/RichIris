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


# Input options for reading a recorded .ts. These cameras mux a private data
# stream (PID 0x101, ~25 packets/s) next to the video, and at night the video
# packets can land more than 5 s of that stream's timestamps behind it in the
# file, so ffmpeg's default 5 s analysis window closes with no SPS seen:
# "Could not find codec parameters ... unspecified size", and every playback
# of that segment 500s ("dimensions not set"). Seen 2026-10-11 from 18:00 on
# Front North 42 and Alfresco 44. A longer analysis window fixes it and costs
# nothing on a normal file (ffmpeg stops as soon as it has the parameters).
# -probesize does NOT help; it is the time window, not the byte budget.
TS_INPUT_ARGS = ["-analyzeduration", "20M"]

# Stream type of ADTS AAC in an MPEG-TS program map.
_TS_STREAM_TYPE_ADTS_AAC = 0x0F


def _ts_stream_types(path: str, scan_bytes: int = 65536) -> set[int]:
    """Stream types declared in the first program map of a .ts file.

    Reads only the head of the file (ffmpeg writes PAT and PMT first), so it
    is far cheaper than an ffprobe. Any parse problem yields an empty set.
    """
    try:
        with open(path, "rb") as fh:
            data = fh.read(scan_bytes)
    except OSError:
        return set()
    pmt_pid: int | None = None
    for off in range(0, len(data) - 187, 188):
        pkt = data[off:off + 188]
        if pkt[0] != 0x47 or not pkt[1] & 0x40:  # sync byte, payload unit start
            continue
        pid = ((pkt[1] & 0x1F) << 8) | pkt[2]
        if pid != 0 and pid != pmt_pid:
            continue
        pos = 4
        if pkt[3] & 0x20:  # adaptation field present
            pos += 1 + pkt[4]
        if not pkt[3] & 0x10 or pos >= 188:
            continue
        sec = pkt[pos + 1 + pkt[pos]:]  # skip pointer_field
        if len(sec) < 12:
            continue
        end = min(len(sec), 3 + (((sec[1] & 0x0F) << 8) | sec[2]) - 4)  # minus CRC
        if pid == 0 and sec[0] == 0x00:
            for i in range(8, end - 3, 4):
                if (sec[i] << 8) | sec[i + 1]:  # program_number 0 is the NIT
                    pmt_pid = ((sec[i + 2] & 0x1F) << 8) | sec[i + 3]
                    break
        elif pid == pmt_pid and sec[0] == 0x02:
            types: set[int] = set()
            i = 12 + (((sec[10] & 0x0F) << 8) | sec[11])
            while i + 5 <= end:
                types.add(sec[i])
                i += 5 + (((sec[i + 3] & 0x0F) << 8) | sec[i + 4])
            return types
    return set()


def ts_audio_bsf_args(paths: list[str]) -> list[str]:
    """Output args needed to copy a recording's audio into MP4.

    AAC sits in MPEG-TS as ADTS and MP4 wants it raw. ffmpeg 7.1 does not
    insert `aac_adtstoasc` by itself on this path: the mux dies with "Malformed
    AAC bitstream" a few frames in, taking the video with it. The filter is
    only valid on AAC, so it is added only when a segment declares AAC (it is
    harmless when that stream turns out to be empty).
    """
    if any(_TS_STREAM_TYPE_ADTS_AAC in _ts_stream_types(p) for p in paths):
        return ["-bsf:a", "aac_adtstoasc"]
    return []


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


async def probe_audio_codec(rtsp_url: str, config: AppConfig) -> str | None:
    """Audio codec of a live RTSP source: its name, "" for no audio track, None if unreachable."""
    cmd = [
        config.ffmpeg.ffprobe_path, "-v", "error",
        "-rtsp_transport", config.ffmpeg.rtsp_transport, "-timeout", "8000000",
        "-show_entries", "stream=codec_type,codec_name", "-of", "json", rtsp_url,
    ]
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
        )
        try:
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=15)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            return None
        streams = json.loads(stdout.decode() or "{}").get("streams", [])
    except Exception:  # noqa: BLE001
        logger.warning("Audio probe failed", extra={"rtsp_url": _redact_rtsp(rtsp_url)})
        return None
    if not streams:
        return None
    for stream in streams:
        if stream.get("codec_type") == "audio":
            return (stream.get("codec_name") or "").lower()
    return ""


def build_recording_output(camera_name: str, config: AppConfig, record_audio: bool = False,
                           audio_codec: str | None = None) -> list[str]:
    """Build ffmpeg output args for segment-based recording (video passthrough).

    audio_codec is the source's audio codec when known (see probe_audio_codec).
    With record_audio, anything that is not AAC (the G.711 u-law most of the
    cameras send) is encoded to AAC: mpegts cannot carry G.711 as audio, a
    stream copy lands it in the file as an unplayable private data stream.
    """
    if record_audio and audio_codec and audio_codec != "aac":
        audio_args = ["-c:a", "aac", "-b:a", "32k"]
    else:
        audio_args = ["-c:a", "copy", *(["-copyinkf:a"] if record_audio else [])]
    safe_name = sanitize_camera_name(camera_name)
    rec_dir = Path(config.storage.recordings_dir) / safe_name
    segment_pattern = str(rec_dir / "%Y-%m-%d" / "rec_%H-%M-%S.ts")

    args = [
        "-map", "0:v",
        "-map", "0:a?",
        "-c:v", "copy",
        # Copy path: ffmpeg 7.1 no longer treats AAC as all-keyframes and RTSP AAC packets
        # arrive without the key flag, so a stream copy waits forever for a
        # "keyframe" and writes an audio stream with no packets in it.
        # -copyinkf:a lets them through. Opt-in per camera (cameras.record_audio)
        # so cameras that never recorded sound do not start to.
        *audio_args,
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


def build_recording_command(camera_name: str, rtsp_url: str, config: AppConfig,
                            record_audio: bool = False,
                            audio_codec: str | None = None) -> list[str]:
    """Build ffmpeg command for recording only (passthrough, no transcode).

    rtsp_url can be a camera RTSP URL or a go2rtc local RTSP URL
    (e.g. rtsp://127.0.0.1:8554/stream_name_s1_direct).
    """
    cmd = build_input_args(rtsp_url, config, hwaccel=False)
    cmd.extend(build_recording_output(camera_name, config, record_audio=record_audio,
                                      audio_codec=audio_codec))
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
