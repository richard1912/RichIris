"""On-demand playback: HEVC recordings → MP4 remux/transcode for browser playback."""

import asyncio
import logging
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path

from app.config import get_config
from app.services.job_object import assign_to_job

logger = logging.getLogger(__name__)

PLAYBACK_DIR = Path("data/playback")
IDLE_TIMEOUT = 30  # seconds before cleanup

# Default recording bitrate (kbps) if probing fails.
DEFAULT_RECORDING_BITRATE_KBPS = 4000


async def _probe_file(path: str) -> tuple[int | None, str | None]:
    """Probe a local .ts file's video bitrate (kbps) and codec via ffprobe."""
    config = get_config()
    cmd = [
        config.ffmpeg.ffprobe_path,
        "-v", "quiet", "-print_format", "json",
        "-show_format", "-show_streams", path,
    ]
    try:
        import json as _json
        proc = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=10)
        data = _json.loads(stdout.decode())
        bps = data.get("format", {}).get("bit_rate")
        kbps = int(bps) // 1000 if bps is not None else None
        codec = None
        for stream in data.get("streams", []):
            if stream.get("codec_type") == "video":
                codec = stream.get("codec_name", "").lower() or None
                break
        return kbps, codec
    except Exception:
        logger.exception("Failed to probe file", extra={"path": path})
    return None, None


def _build_playback_preset(quality: str, source_kbps: int, source_codec: str = "hevc") -> dict:
    """Build a playback quality preset with source-matched visual quality.

    Recordings are typically HEVC, so H.264 target needs ~2x bitrate for
    equivalent visual quality.
    """
    if quality == "direct":
        return {
            "pre_input": [],
            "codec": ["-c", "copy"],
            "movflags": "frag_keyframe+empty_moov",
            "streaming": True,
        }
    mult = 2 if source_codec == "hevc" else 1
    high_kbps = source_kbps * mult
    high_br = f"{high_kbps}k"
    low_br = f"{max(high_kbps // 4, 500)}k"
    bitrate = high_br if quality == "high" else low_br
    return {
        "pre_input": ["-hwaccel", "cuda"],
        "codec": ["-c:v", "h264_nvenc", "-preset", "p4",
                  "-b:v", bitrate, "-c:a", "copy"],
        "movflags": "frag_keyframe+empty_moov",
        "streaming": True,
    }


@dataclass
class PlaybackSession:
    session_id: str
    output_dir: Path
    camera_id: int | None = None
    process: asyncio.subprocess.Process | None = None
    created_at: float = field(default_factory=time.time)
    last_access: float = field(default_factory=time.time)
    ready: bool = False
    streaming: bool = False
    _ready_event: asyncio.Event = field(default_factory=asyncio.Event)


class PlaybackManager:
    def __init__(self) -> None:
        self._sessions: dict[str, PlaybackSession] = {}
        self._cleanup_task: asyncio.Task | None = None

    def _ensure_cleanup(self) -> None:
        if self._cleanup_task is None or self._cleanup_task.done():
            self._cleanup_task = asyncio.create_task(self._periodic_cleanup())

    async def start_session(
        self, session_id: str, segment_paths: list[str],
        seek_seconds: float = 0.0, duration_limit: float = 1800.0,
        quality: str = "high", camera_id: int | None = None,
    ) -> PlaybackSession:
        """Start a playback session. High = instant remux, medium/low = NVENC transcode."""
        # Return existing session if still valid
        if session_id in self._sessions:
            session = self._sessions[session_id]
            session.last_access = time.time()
            return session

        # Kill old sessions for the same camera to prevent ffmpeg accumulation
        if camera_id is not None:
            stale = [
                sid for sid, s in self._sessions.items()
                if s.camera_id == camera_id and sid != session_id
            ]
            for sid in stale:
                logger.info("Evicting old playback session", extra={"session_id": sid, "camera_id": camera_id})
                await self.stop_session(sid)

        self._ensure_cleanup()

        output_dir = PLAYBACK_DIR / session_id
        output_dir.mkdir(parents=True, exist_ok=True)

        session = PlaybackSession(session_id=session_id, output_dir=output_dir, camera_id=camera_id)
        self._sessions[session_id] = session

        config = get_config()
        output_path = output_dir / "playback.mp4"

        # Probe source bitrate and codec from first segment
        source_kbps = DEFAULT_RECORDING_BITRATE_KBPS
        source_codec = "hevc"
        if quality != "direct" and segment_paths:
            probed_kbps, probed_codec = await _probe_file(segment_paths[0])
            if probed_kbps:
                source_kbps = probed_kbps
            if probed_codec:
                source_codec = probed_codec
            logger.info("Probed recording", extra={"kbps": source_kbps, "codec": source_codec, "quality": quality})

        preset = _build_playback_preset(quality, source_kbps, source_codec)
        pre_input = preset["pre_input"]
        codec_args = preset["codec"]
        movflags = preset.get("movflags", "+faststart")
        session.streaming = preset.get("streaming", False)

        # For single file: direct input with fast seek (-ss before -i)
        # For multiple files: use concat demuxer (no seek support, use -ss after -i)
        if len(segment_paths) == 1:
            cmd = [
                config.ffmpeg.path, "-y",
                *pre_input,
                "-ss", str(seek_seconds),
                "-i", segment_paths[0],
                "-t", str(duration_limit),
                *codec_args,
                "-movflags", movflags,
                str(output_path),
            ]
        else:
            # Write concat demuxer list file
            concat_list_path = output_dir / "concat_list.txt"
            with open(concat_list_path, "w") as f:
                f.write("ffconcat version 1.0\n")
                for p in segment_paths:
                    escaped = p.replace("\\", "/").replace("'", "'\\''")
                    f.write(f"file '{escaped}'\n")

            cmd = [
                config.ffmpeg.path, "-y",
                *pre_input,
                "-f", "concat", "-safe", "0",
                "-i", str(concat_list_path),
                "-ss", str(seek_seconds),
                "-t", str(duration_limit),
                *codec_args,
                "-movflags", movflags,
                str(output_path),
            ]

        logger.info(
            "Starting playback remux",
            extra={"session_id": session_id, "segments": len(segment_paths), "seek_seconds": seek_seconds},
        )

        session.process = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        assign_to_job(session.process.pid)

        # Wait for completion (remux is near-instant, typically < 1 second)
        asyncio.create_task(self._wait_ready(session))

        return session

    async def _wait_ready(self, session: PlaybackSession) -> None:
        """Wait for the remux/transcode to be ready for serving.

        For non-streaming (direct/high): wait for ffmpeg to finish.
        For streaming (medium/low): mark ready as soon as the file has initial data.
        """
        if not session.process:
            session._ready_event.set()
            return

        output_path = session.output_dir / "playback.mp4"

        if session.streaming:
            # Fragmented MP4: ready once initial fMP4 header is written
            for _ in range(300):  # up to 30 seconds
                await asyncio.sleep(0.1)
                if output_path.exists() and output_path.stat().st_size > 4096:
                    session.ready = True
                    logger.info("Streaming playback ready", extra={"session_id": session.session_id})
                    session._ready_event.set()
                    return
                # If process already exited, check file
                if session.process.returncode is not None:
                    break
            # Fallback: check if file was created
            if output_path.exists() and output_path.stat().st_size > 0:
                session.ready = True
            else:
                stderr = await session.process.stderr.read() if session.process.stderr else b""
                logger.error(
                    "Streaming playback failed to start",
                    extra={
                        "session_id": session.session_id,
                        "returncode": session.process.returncode,
                        "stderr": stderr.decode("utf-8", errors="replace")[-500:],
                    },
                )
            session._ready_event.set()
            return

        # Non-streaming: wait for full completion
        try:
            await asyncio.wait_for(session.process.wait(), timeout=60)
        except asyncio.TimeoutError:
            logger.error("Playback remux timed out", extra={"session_id": session.session_id})
            session._ready_event.set()
            return

        if output_path.exists() and output_path.stat().st_size > 0:
            session.ready = True
            logger.info("Playback session ready", extra={"session_id": session.session_id})
        else:
            stderr = await session.process.stderr.read() if session.process.stderr else b""
            logger.error(
                "Playback remux failed",
                extra={
                    "session_id": session.session_id,
                    "returncode": session.process.returncode,
                    "stderr": stderr.decode("utf-8", errors="replace")[-500:],
                },
            )
        session._ready_event.set()

    def touch(self, session_id: str) -> None:
        session = self._sessions.get(session_id)
        if session:
            session.last_access = time.time()

    def get_session(self, session_id: str) -> PlaybackSession | None:
        session = self._sessions.get(session_id)
        if session:
            session.last_access = time.time()
        return session

    async def stop_session(self, session_id: str) -> None:
        session = self._sessions.pop(session_id, None)
        if not session:
            return
        if session.process and session.process.returncode is None:
            session.process.terminate()
            try:
                await asyncio.wait_for(session.process.wait(), timeout=5)
            except asyncio.TimeoutError:
                session.process.kill()
        if session.output_dir.exists():
            shutil.rmtree(session.output_dir, ignore_errors=True)
        logger.info("Playback session cleaned up", extra={"session_id": session_id})

    async def stop_all(self) -> None:
        for sid in list(self._sessions.keys()):
            await self.stop_session(sid)
        if self._cleanup_task:
            self._cleanup_task.cancel()

    async def _periodic_cleanup(self) -> None:
        while True:
            await asyncio.sleep(30)
            now = time.time()
            expired = [
                sid
                for sid, s in self._sessions.items()
                if now - s.last_access > IDLE_TIMEOUT
            ]
            for sid in expired:
                logger.info("Cleaning up idle playback session", extra={"session_id": sid})
                await self.stop_session(sid)


_manager: PlaybackManager | None = None


def get_playback_manager() -> PlaybackManager:
    global _manager
    if _manager is None:
        _manager = PlaybackManager()
    return _manager
