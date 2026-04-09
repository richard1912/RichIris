"""Manages ffmpeg subprocesses for camera streams."""

import asyncio
import logging
import re
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

import httpx

from app.config import AppConfig, get_config
from app.services.ffmpeg import build_recording_command, sanitize_camera_name
from app.services.go2rtc_client import get_go2rtc_client, get_stream_name
from app.services.job_object import assign_to_job

logger = logging.getLogger(__name__)

# Watchdog: kill ffmpeg if no .ts file has been modified in this many seconds
STALE_THRESHOLD_SECONDS = 300  # 5 minutes
# How often the watchdog checks for stale recordings
WATCHDOG_INTERVAL_SECONDS = 120  # 2 minutes


@dataclass
class StreamInfo:
    camera_id: int
    camera_name: str
    rtsp_url: str
    sub_stream_url: str | None = None
    rec_process: asyncio.subprocess.Process | None = None
    started_at: float = 0.0
    restart_count: int = 0
    last_error: str | None = None
    _rec_monitor_task: asyncio.Task | None = field(default=None, repr=False)
    _watchdog_task: asyncio.Task | None = field(default=None, repr=False)
    _sub_keepalive_task: asyncio.Task | None = field(default=None, repr=False)
    _main_keepalive_task: asyncio.Task | None = field(default=None, repr=False)


class StreamManager:
    """Manages the lifecycle of ffmpeg processes for all cameras."""

    def __init__(self) -> None:
        self._streams: dict[int, StreamInfo] = {}
        self._running = False
        self._keepalive_index = 0  # Stagger keepalive startups

    @property
    def streams(self) -> dict[int, StreamInfo]:
        return self._streams

    async def start_stream(
        self, camera_id: int, camera_name: str, rtsp_url: str, sub_stream_url: str | None = None
    ) -> None:
        """Start a recording ffmpeg process connected directly to the camera.

        Recording uses direct camera RTSP for maximum reliability (independent
        of go2rtc). Live view goes through go2rtc with keepalive consumers.
        """
        if camera_id in self._streams and self._streams[camera_id].rec_process:
            logger.warning("Stream already running", extra={"camera_id": camera_id})
            return

        config = get_config()
        _ensure_directories(camera_name, config)

        info = StreamInfo(
            camera_id=camera_id,
            camera_name=camera_name,
            rtsp_url=rtsp_url,
            sub_stream_url=sub_stream_url or None,
        )
        self._streams[camera_id] = info

        await _launch_recording(info, config)

        info._rec_monitor_task = asyncio.create_task(
            self._monitor_process(camera_id)
        )
        info._watchdog_task = asyncio.create_task(
            self._watchdog(camera_id)
        )
        # Keep go2rtc connections alive for instant live view via HTTP fMP4.
        # Stagger startups by 1s each to avoid overwhelming go2rtc.
        main_delay = 3 + self._keepalive_index
        sub_delay = main_delay + 0.5
        self._keepalive_index += 1
        info._main_keepalive_task = asyncio.create_task(
            self._go2rtc_keepalive(camera_id, "s1_direct", startup_delay=main_delay)
        )
        info._sub_keepalive_task = asyncio.create_task(
            self._go2rtc_keepalive(camera_id, "s2_direct", startup_delay=sub_delay)
        )

        logger.info("Recording stream started", extra={"camera_id": camera_id, "camera": camera_name})

    async def stop_stream(self, camera_id: int) -> None:
        """Stop all ffmpeg processes for a camera."""
        info = self._streams.get(camera_id)
        if not info:
            logger.warning("No stream to stop", extra={"camera_id": camera_id})
            return

        if info._rec_monitor_task:
            info._rec_monitor_task.cancel()
            info._rec_monitor_task = None
        if info._watchdog_task:
            info._watchdog_task.cancel()
            info._watchdog_task = None
        if info._sub_keepalive_task:
            info._sub_keepalive_task.cancel()
            info._sub_keepalive_task = None
        if info._main_keepalive_task:
            info._main_keepalive_task.cancel()
            info._main_keepalive_task = None

        # Remove from go2rtc
        client = get_go2rtc_client()
        await client.remove_stream(info.camera_name)

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

    async def _monitor_process(self, camera_id: int) -> None:
        """Watch the ffmpeg recording process and restart on failure."""
        self._running = True

        dir_task = asyncio.create_task(self._ensure_date_dirs(camera_id))

        while self._running:
            info = self._streams.get(camera_id)
            if not info or not info.rec_process:
                break

            returncode = await info.rec_process.wait()
            if not self._running:
                break

            info.last_error = f"Process exited with code {returncode}"
            info.restart_count += 1
            logger.error(
                "Recording process died, restarting",
                extra={
                    "camera_id": camera_id,
                    "returncode": returncode,
                    "restart_count": info.restart_count,
                },
            )

            # Exponential backoff: 5s, 10s, 20s, 40s, max 60s
            await asyncio.sleep(min(5 * (2 ** min(info.restart_count - 1, 4)), 60))
            if not self._running:
                break

            config = get_config()
            _ensure_directories(info.camera_name, config)
            await _launch_recording(info, config)

        dir_task.cancel()

    async def _watchdog(self, camera_id: int) -> None:
        """Kill ffmpeg if it stops producing recording files (stale stream detection)."""
        # Grace period: don't check until the process has been running long enough
        # to have produced at least one segment update
        await asyncio.sleep(STALE_THRESHOLD_SECONDS)

        while self._running:
            await asyncio.sleep(WATCHDOG_INTERVAL_SECONDS)
            info = self._streams.get(camera_id)
            if not info or not info.rec_process:
                break
            if info.rec_process.returncode is not None:
                break  # Already dead, monitor will handle restart

            config = get_config()
            safe_name = sanitize_camera_name(info.camera_name)
            rec_dir = Path(config.storage.recordings_dir) / safe_name
            today = datetime.now().strftime("%Y-%m-%d")
            today_dir = rec_dir / today

            if not today_dir.exists():
                continue

            # Check for any .ts file modified within the stale threshold
            now = time.time()
            has_fresh_file = False
            try:
                for f in today_dir.iterdir():
                    if f.suffix == ".ts" and (now - f.stat().st_mtime) < STALE_THRESHOLD_SECONDS:
                        has_fresh_file = True
                        break
            except OSError:
                continue

            if has_fresh_file:
                continue

            # No recent recording output — stream is stale
            logger.error(
                "Watchdog: no recording output detected, killing stale ffmpeg",
                extra={
                    "camera_id": camera_id,
                    "camera_name": info.camera_name,
                    "threshold_seconds": STALE_THRESHOLD_SECONDS,
                },
            )
            info.last_error = "Watchdog: stale recording — no output files"
            info.rec_process.kill()
            # _monitor_process will detect the exit and restart

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

    async def _go2rtc_keepalive(self, camera_id: int, stream_suffix: str, startup_delay: float = 0) -> None:
        """Keep a go2rtc stream alive via a persistent HTTP fMP4 consumer.

        Opens an HTTP streaming connection to go2rtc's fMP4 API and reads/discards
        data, keeping the camera RTSP connection alive for instant live view.
        In-process (no ffmpeg subprocess), uses HTTP (avoids RTSP concurrent map bug).
        """
        info = self._streams.get(camera_id)
        if not info:
            return

        from app.services.go2rtc_manager import get_api_port
        stream_name = get_stream_name(info.camera_name)
        full_stream = f"{stream_name}_{stream_suffix}"
        fmp4_url = f"http://127.0.0.1:{get_api_port()}/api/stream.mp4?src={full_stream}"

        import time as _time
        from app.services.go2rtc_client import wait_for_go2rtc_ready
        await wait_for_go2rtc_ready()
        if startup_delay > 0:
            logger.debug("Keepalive waiting for staggered start",
                         extra={"camera_id": camera_id, "stream": full_stream,
                                "delay_s": startup_delay})
            await asyncio.sleep(startup_delay)

        while self._running:
            try:
                t_connect = _time.monotonic()
                async with httpx.AsyncClient(timeout=httpx.Timeout(
                    connect=10, read=60, write=10, pool=10
                )) as client:
                    async with client.stream("GET", fmp4_url) as resp:
                        if resp.status_code != 200:
                            logger.warning("Keepalive HTTP error",
                                           extra={"camera_id": camera_id, "stream": full_stream,
                                                  "status": resp.status_code})
                            await asyncio.sleep(5)
                            continue

                        connect_ms = round((_time.monotonic() - t_connect) * 1000, 1)
                        # Read first chunk to confirm data is flowing
                        first_chunk = True
                        async for _chunk in resp.aiter_bytes(chunk_size=65536):
                            if first_chunk:
                                first_chunk_ms = round((_time.monotonic() - t_connect) * 1000, 1)
                                logger.info("Keepalive connected",
                                            extra={"camera_id": camera_id, "stream": full_stream,
                                                   "connect_ms": connect_ms,
                                                   "first_chunk_ms": first_chunk_ms})
                                first_chunk = False
                            if not self._running:
                                return

                if not self._running:
                    return
                logger.debug("Keepalive stream ended, reconnecting",
                             extra={"camera_id": camera_id, "stream": full_stream})
            except asyncio.CancelledError:
                return
            except Exception:
                if not self._running:
                    return
                logger.debug("Keepalive error, retrying in 5s",
                             extra={"camera_id": camera_id, "stream": full_stream})
            await asyncio.sleep(5)

    def get_status(self, camera_id: int) -> dict:
        """Get status info for a stream."""
        info = self._streams.get(camera_id)
        if not info:
            return {"running": False}

        running = info.rec_process is not None and info.rec_process.returncode is None
        uptime = time.time() - info.started_at if running else None

        return {
            "running": running,
            "pid": info.rec_process.pid if info.rec_process else None,
            "uptime_seconds": uptime,
            "restart_count": info.restart_count,
            "error": info.last_error,
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


# ffmpeg stderr lines matching these (case-insensitive) are worth logging
_FFMPEG_ERROR_KEYWORDS = re.compile(
    r"error|warning|fatal|failed|dropping|disconnected|timeout|broken.pipe|killed|exceeded",
    re.IGNORECASE,
)


async def _read_stderr(info: StreamInfo, label: str) -> None:
    """Read ffmpeg stderr, log banner once at INFO, then only warnings/errors."""
    proc = info.rec_process
    if not proc or not proc.stderr:
        return
    banner_logged = False
    buf = ""
    try:
        while True:
            chunk = await proc.stderr.read(8192)
            if not chunk:
                break
            buf += chunk.decode("utf-8", errors="replace")
            while "\n" in buf:
                line, buf = buf.split("\n", 1)
                line = line.strip()
                if not line:
                    continue

                # Log the first batch of lines (codec/stream info banner) once
                if not banner_logged:
                    # Banner ends when ffmpeg starts outputting progress (frame= or size=)
                    if line.startswith("frame=") or line.startswith("size="):
                        banner_logged = True
                        continue
                    logger.info(
                        f"ffmpeg-{label} init",
                        extra={"camera_id": info.camera_id, "output": line[:500]},
                    )
                    continue

                # After banner, only log lines with warning/error indicators
                if _FFMPEG_ERROR_KEYWORDS.search(line):
                    logger.warning(
                        f"ffmpeg-{label}",
                        extra={"camera_id": info.camera_id, "output": line[:500]},
                    )
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
    """Create recording directories for a camera, including today's date subfolder."""
    from datetime import datetime

    safe_name = sanitize_camera_name(camera_name)
    rec_dir = Path(config.storage.recordings_dir) / safe_name
    today_dir = rec_dir / datetime.now().strftime("%Y-%m-%d")
    today_dir.mkdir(parents=True, exist_ok=True)
    logger.debug("Ensured camera directories", extra={"camera": camera_name})


# Singleton
_manager: StreamManager | None = None


def get_stream_manager() -> StreamManager:
    """Return the singleton StreamManager instance."""
    global _manager
    if _manager is None:
        _manager = StreamManager()
    return _manager
