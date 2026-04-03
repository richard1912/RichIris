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

# Quality presets for playback transcoding.
# direct = HEVC passthrough (instant remux), high = native res H.264 re-encode,
# low = native res H.264 reduced bitrate.
PLAYBACK_QUALITY: dict[str, dict] = {
    "direct": {
        "pre_input": [],
        "codec": ["-c", "copy"],
        "movflags": "frag_keyframe+empty_moov",
        "streaming": True,
    },
    "high": {
        "pre_input": ["-hwaccel", "cuda"],
        "codec": ["-c:v", "h264_nvenc", "-preset", "p4", "-c:a", "copy"],
        "movflags": "frag_keyframe+empty_moov",
        "streaming": True,
    },
    "low": {
        "pre_input": ["-hwaccel", "cuda"],
        "codec": ["-c:v", "h264_nvenc", "-preset", "p4", "-b:v", "2M",
                  "-c:a", "copy"],
        "movflags": "frag_keyframe+empty_moov",
        "streaming": True,
    },
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

        preset = PLAYBACK_QUALITY.get(quality, PLAYBACK_QUALITY["high"])
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
