"""Manages ffmpeg subprocesses for camera streams."""

import asyncio
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path

from app.config import AppConfig, get_config
from app.services.ffmpeg import build_live_command, build_recording_command, sanitize_camera_name
from app.services.job_object import assign_to_job

logger = logging.getLogger(__name__)

LIVE_IDLE_TIMEOUT = 15  # seconds of no viewer activity before stopping live process


@dataclass
class StreamInfo:
    camera_id: int
    camera_name: str
    rtsp_url: str
    # Recording process (always on)
    rec_process: asyncio.subprocess.Process | None = None
    started_at: float = 0.0
    restart_count: int = 0
    last_error: str | None = None
    _rec_monitor_task: asyncio.Task | None = field(default=None, repr=False)
    # Live process (on-demand)
    live_process: asyncio.subprocess.Process | None = None
    live_started_at: float = 0.0
    last_live_access: float = 0.0
    _live_monitor_task: asyncio.Task | None = field(default=None, repr=False)
    _live_idle_task: asyncio.Task | None = field(default=None, repr=False)


class StreamManager:
    """Manages the lifecycle of ffmpeg processes for all cameras."""

    def __init__(self) -> None:
        self._streams: dict[int, StreamInfo] = {}
        self._running = False

    @property
    def streams(self) -> dict[int, StreamInfo]:
        return self._streams

    async def start_stream(self, camera_id: int, camera_name: str, rtsp_url: str) -> None:
        """Start a recording-only ffmpeg process for a camera."""
        if camera_id in self._streams and self._streams[camera_id].rec_process:
            logger.warning("Stream already running", extra={"camera_id": camera_id})
            return

        config = get_config()
        _ensure_directories(camera_name, config)

        info = StreamInfo(
            camera_id=camera_id,
            camera_name=camera_name,
            rtsp_url=rtsp_url,
        )
        self._streams[camera_id] = info

        await _launch_recording(info, config)

        info._rec_monitor_task = asyncio.create_task(
            self._monitor_recording(camera_id)
        )
        logger.info("Recording stream started", extra={"camera_id": camera_id, "camera": camera_name})

    async def start_live(self, camera_id: int) -> None:
        """Start the live HLS process for a camera (on-demand)."""
        info = self._streams.get(camera_id)
        if not info:
            logger.warning("Cannot start live: no stream info", extra={"camera_id": camera_id})
            return

        info.last_live_access = time.time()

        if info.live_process and info.live_process.returncode is None:
            return  # Already running

        config = get_config()
        _ensure_directories(info.camera_name, config)
        await _launch_live(info, config)

        info._live_monitor_task = asyncio.create_task(
            self._monitor_live(camera_id)
        )
        info._live_idle_task = asyncio.create_task(
            self._idle_checker(camera_id)
        )
        logger.info("Live stream started on-demand", extra={"camera_id": camera_id})

    async def stop_live(self, camera_id: int) -> None:
        """Stop the live HLS process for a camera."""
        info = self._streams.get(camera_id)
        if not info:
            return

        if info._live_idle_task:
            info._live_idle_task.cancel()
            info._live_idle_task = None
        if info._live_monitor_task:
            info._live_monitor_task.cancel()
            info._live_monitor_task = None

        await _terminate_process(info.live_process)
        info.live_process = None

        # Clean up HLS files
        config = get_config()
        safe_name = sanitize_camera_name(info.camera_name)
        live_dir = Path(config.storage.live_dir) / safe_name
        for f in live_dir.glob("*.ts"):
            f.unlink(missing_ok=True)
        for f in live_dir.glob("*.m3u8"):
            f.unlink(missing_ok=True)

        logger.info("Live stream stopped (idle)", extra={"camera_id": camera_id})

    def touch_live(self, camera_id: int) -> None:
        """Update last access time for a live stream (call on each viewer request)."""
        info = self._streams.get(camera_id)
        if info:
            info.last_live_access = time.time()

    def is_live_running(self, camera_id: int) -> bool:
        info = self._streams.get(camera_id)
        if not info or not info.live_process:
            return False
        return info.live_process.returncode is None

    async def stop_stream(self, camera_id: int) -> None:
        """Stop all ffmpeg processes for a camera."""
        info = self._streams.get(camera_id)
        if not info:
            logger.warning("No stream to stop", extra={"camera_id": camera_id})
            return

        # Stop live
        await self.stop_live(camera_id)

        # Stop recording
        if info._rec_monitor_task:
            info._rec_monitor_task.cancel()
            info._rec_monitor_task = None

        await _terminate_process(info.rec_process)
        del self._streams[camera_id]
        logger.info("Stream stopped", extra={"camera_id": camera_id})

    async def stop_all(self) -> None:
        """Stop all running streams."""
        self._running = False
        camera_ids = list(self._streams.keys())
        for cid in camera_ids:
            await self.stop_stream(cid)
        logger.info("All streams stopped")

    async def _monitor_recording(self, camera_id: int) -> None:
        """Watch the recording process and restart on failure."""
        self._running = True

        dir_task = asyncio.create_task(self._ensure_date_dirs(camera_id))

        while self._running:
            info = self._streams.get(camera_id)
            if not info or not info.rec_process:
                break

            returncode = await info.rec_process.wait()
            if not self._running:
                break

            info.last_error = f"Recording process exited with code {returncode}"
            info.restart_count += 1
            logger.error(
                "Recording process died, restarting",
                extra={
                    "camera_id": camera_id,
                    "returncode": returncode,
                    "restart_count": info.restart_count,
                },
            )

            await asyncio.sleep(min(5 * info.restart_count, 30))
            if not self._running:
                break

            config = get_config()
            _ensure_directories(info.camera_name, config)
            await _launch_recording(info, config)

        dir_task.cancel()

    async def _monitor_live(self, camera_id: int) -> None:
        """Watch the live process and restart on failure (only while viewers active)."""
        while self._running:
            info = self._streams.get(camera_id)
            if not info or not info.live_process:
                break

            returncode = await info.live_process.wait()
            if not self._running:
                break

            # If idle timeout already stopped it, don't restart
            elapsed = time.time() - info.last_live_access
            if elapsed > LIVE_IDLE_TIMEOUT:
                break

            logger.warning(
                "Live process died, restarting",
                extra={"camera_id": camera_id, "returncode": returncode},
            )
            await asyncio.sleep(2)
            if not self._running:
                break

            config = get_config()
            await _launch_live(info, config)

    async def _idle_checker(self, camera_id: int) -> None:
        """Periodically check if the live stream is idle and stop it."""
        while True:
            await asyncio.sleep(10)
            info = self._streams.get(camera_id)
            if not info:
                break

            if not info.live_process or info.live_process.returncode is not None:
                break

            elapsed = time.time() - info.last_live_access
            if elapsed > LIVE_IDLE_TIMEOUT:
                logger.info(
                    "Live stream idle, stopping",
                    extra={"camera_id": camera_id, "idle_seconds": elapsed},
                )
                await self.stop_live(camera_id)
                break

    async def _ensure_date_dirs(self, camera_id: int) -> None:
        """Periodically create tomorrow's date directory so midnight rollovers work."""
        from datetime import datetime, timedelta

        while True:
            await asyncio.sleep(3600)
            info = self._streams.get(camera_id)
            if not info:
                break
            config = get_config()
            safe_name = sanitize_camera_name(info.camera_name)
            tomorrow = (datetime.now() + timedelta(days=1)).strftime("%Y-%m-%d")
            tomorrow_dir = Path(config.storage.recordings_dir) / safe_name / tomorrow
            tomorrow_dir.mkdir(parents=True, exist_ok=True)

    def get_status(self, camera_id: int) -> dict:
        """Get status info for a stream."""
        info = self._streams.get(camera_id)
        if not info:
            return {"running": False}

        running = info.rec_process is not None and info.rec_process.returncode is None
        uptime = time.time() - info.started_at if running else None
        live_running = info.live_process is not None and info.live_process.returncode is None
        return {
            "running": running,
            "pid": info.rec_process.pid if info.rec_process else None,
            "uptime_seconds": uptime,
            "restart_count": info.restart_count,
            "error": info.last_error,
            "live_running": live_running,
            "live_pid": info.live_process.pid if info.live_process and live_running else None,
        }


async def _launch_recording(info: StreamInfo, config: AppConfig) -> None:
    """Launch the recording-only ffmpeg subprocess."""
    cmd = build_recording_command(info.camera_name, info.rtsp_url, config)
    logger.debug("Launching recording ffmpeg", extra={"cmd": " ".join(cmd), "camera_id": info.camera_id})

    info.rec_process = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    info.started_at = time.time()
    info.last_error = None
    assign_to_job(info.rec_process.pid)

    asyncio.create_task(_read_stderr(info, "rec"))


async def _launch_live(info: StreamInfo, config: AppConfig) -> None:
    """Launch the live HLS ffmpeg subprocess."""
    cmd = build_live_command(info.camera_name, info.rtsp_url, config)
    logger.debug("Launching live ffmpeg", extra={"cmd": " ".join(cmd), "camera_id": info.camera_id})

    info.live_process = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    info.live_started_at = time.time()
    info.last_live_access = time.time()
    assign_to_job(info.live_process.pid)

    asyncio.create_task(_read_stderr(info, "live"))


async def _read_stderr(info: StreamInfo, label: str) -> None:
    """Read ffmpeg stderr output for logging.

    Uses a large read buffer and chunked reads to avoid LimitOverrunError
    when ffmpeg outputs long progress lines without newlines.
    """
    proc = info.rec_process if label == "rec" else info.live_process
    if not proc or not proc.stderr:
        return
    try:
        while True:
            # Use raw read instead of readline to avoid LimitOverrunError
            # on ffmpeg's long progress lines (no newline separators)
            chunk = await proc.stderr.read(8192)
            if not chunk:
                break
            decoded = chunk.decode("utf-8", errors="replace").strip()
            if decoded:
                # Only log first 500 chars to avoid log spam from progress lines
                if len(decoded) > 500:
                    decoded = decoded[:500] + "..."
                logger.debug(f"ffmpeg-{label}", extra={"camera_id": info.camera_id, "output": decoded})
    except Exception:
        logger.exception(f"ffmpeg-{label} stderr reader failed", extra={"camera_id": info.camera_id})


async def _terminate_process(process: asyncio.subprocess.Process | None) -> None:
    """Gracefully terminate an ffmpeg process."""
    if not process or process.returncode is not None:
        return

    logger.debug("Terminating ffmpeg process", extra={"pid": process.pid})
    process.terminate()
    try:
        await asyncio.wait_for(process.wait(), timeout=10)
    except asyncio.TimeoutError:
        logger.warning("Force killing ffmpeg", extra={"pid": process.pid})
        process.kill()
        await process.wait()


def _ensure_directories(camera_name: str, config: AppConfig) -> None:
    """Create recording and live directories for a camera, including today's date subfolder."""
    from datetime import datetime

    safe_name = sanitize_camera_name(camera_name)
    rec_dir = Path(config.storage.recordings_dir) / safe_name
    today_dir = rec_dir / datetime.now().strftime("%Y-%m-%d")
    live_dir = Path(config.storage.live_dir) / safe_name
    today_dir.mkdir(parents=True, exist_ok=True)
    live_dir.mkdir(parents=True, exist_ok=True)
    logger.debug("Ensured camera directories", extra={"camera": camera_name})


# Singleton
_manager: StreamManager | None = None


def get_stream_manager() -> StreamManager:
    """Return the singleton StreamManager instance."""
    global _manager
    if _manager is None:
        _manager = StreamManager()
    return _manager
