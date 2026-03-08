"""On-demand playback transcoder: HEVC recordings → H.264 HLS for browser playback."""

import asyncio
import logging
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path

from app.config import get_config

logger = logging.getLogger(__name__)

PLAYBACK_DIR = Path("data/playback")
IDLE_TIMEOUT = 120  # seconds before cleanup


@dataclass
class PlaybackSession:
    session_id: str
    output_dir: Path
    process: asyncio.subprocess.Process | None = None
    created_at: float = field(default_factory=time.time)
    last_access: float = field(default_factory=time.time)
    ready: bool = False
    seek_seconds: float = 0.0


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
    ) -> PlaybackSession:
        """Start a playback transcoding session."""
        # Return existing session if still valid
        if session_id in self._sessions:
            session = self._sessions[session_id]
            session.last_access = time.time()
            return session

        self._ensure_cleanup()

        output_dir = PLAYBACK_DIR / session_id
        output_dir.mkdir(parents=True, exist_ok=True)

        session = PlaybackSession(session_id=session_id, output_dir=output_dir, seek_seconds=seek_seconds)
        self._sessions[session_id] = session

        # Launch ffmpeg transcode using concat protocol with fast seek
        config = get_config()
        playlist_path = output_dir / "playback.m3u8"

        # Build concat protocol input (byte-level concatenation of .ts files)
        concat_input = "|".join(p.replace("\\", "/") for p in segment_paths)

        cmd = [
            config.ffmpeg.path, "-y",
            "-ss", str(seek_seconds),
            "-hwaccel", "cuda",
            "-i", f"concat:{concat_input}",
            "-t", str(duration_limit),
            "-c:v", "h264_nvenc",
            "-preset", "p4",
            "-b:v", "4M",
            "-maxrate", "5M",
            "-bufsize", "8M",
            "-vf", "scale=1920:-2",
            "-c:a", "aac",
            "-b:a", "128k",
            "-f", "hls",
            "-hls_time", "4",
            "-hls_list_size", "0",
            "-hls_playlist_type", "event",
            "-hls_flags", "temp_file+append_list",
            "-hls_segment_filename", str(output_dir / "seg_%04d.ts"),
            str(playlist_path),
        ]

        logger.info(
            "Starting playback transcode",
            extra={"session_id": session_id, "segments": len(segment_paths), "seek_seconds": seek_seconds},
        )

        session.process = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )

        # Monitor for readiness in background
        asyncio.create_task(self._wait_ready(session))

        return session

    async def _wait_ready(self, session: PlaybackSession) -> None:
        """Wait for the playlist file to appear, then mark session ready."""
        playlist = session.output_dir / "playback.m3u8"
        for _ in range(120):  # up to 60 seconds
            await asyncio.sleep(0.5)
            if playlist.exists() and playlist.stat().st_size > 0:
                session.ready = True
                logger.info("Playback session ready", extra={"session_id": session.session_id})
                return

        # If we get here, transcode likely failed
        if session.process:
            stderr = await session.process.stderr.read() if session.process.stderr else b""
            logger.error(
                "Playback transcode failed to produce playlist",
                extra={
                    "session_id": session.session_id,
                    "stderr": stderr.decode("utf-8", errors="replace")[-500:],
                },
            )

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
