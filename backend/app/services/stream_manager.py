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
from app.services.ffmpeg import build_recording_command, probe_audio_codec, sanitize_camera_name
from app.services.go2rtc_client import get_go2rtc_client, get_stream_name
from app.services.job_object import assign_to_job

logger = logging.getLogger(__name__)

# Watchdog: kill ffmpeg if no .ts file has been modified in this many seconds
STALE_THRESHOLD_SECONDS = 300  # 5 minutes
# How often the watchdog checks for stale recordings
WATCHDOG_INTERVAL_SECONDS = 120  # 2 minutes
# A recorder is a stream copy and sits at ~60 MB. One whose RAM + swap passes this has run
# away (12 GB twice: Garage 2026-09-23, Front Door 2026-11-19, each filling swap and stalling
# the whole box) and is killed so the monitor relaunches it.
RECORDER_MAX_MEMORY_MB = 1024
# A segment still being written this long past the segment length means the recorder has
# stopped rolling. Seen when a camera reconnects and the relay then carries audio only: with
# no video keyframe the segment muxer can never cut, the file still grows (so it is not
# "stale"), and hours of video are silently missing (Front Door, 2026-11-19 00:57 to 08:16).
SEGMENT_OVERRUN_SECONDS = 180
RIV_VIDEO_STALL_SECONDS = 60
# A condition that keeps failing (unreachable camera) is logged this often, not every retry
REPEAT_LOG_INTERVAL_SECONDS = 300  # 5 minutes


def _process_memory_mb(pid: int) -> float | None:
    """Resident + swapped memory of a process in MB. None where /proc is unavailable (Windows)."""
    try:
        kb = 0
        with open(f"/proc/{pid}/status") as fh:
            for line in fh:
                if line.startswith(("VmRSS:", "VmSwap:")):
                    kb += int(line.split()[1])
        return kb / 1024
    except (OSError, ValueError, IndexError):
        return None


def _segment_overrun_seconds(day_dir: Path, segment_seconds: int, now: float) -> float | None:
    """How far past its due roll the segment being written is, or None if not known.

    The file being written is the newest `rec_HH-MM-SS.ts` (finished ones are renamed).
    Segments cut on the wall clock, so it is due to roll at the next multiple of the
    segment length after it started.
    """
    try:
        live = [f for f in day_dir.iterdir() if f.name.startswith("rec_") and f.suffix == ".ts"]
        if not live:
            return None
        newest = max(live, key=lambda f: f.stat().st_mtime)
        if now - newest.stat().st_mtime > 60:
            return None  # not being written; the staleness check owns that case
        started = datetime.strptime(f"{day_dir.name} {newest.stem[4:]}", "%Y-%m-%d %H-%M-%S").timestamp()
    except (OSError, ValueError):
        return None
    due = (int(started // segment_seconds) + 1) * segment_seconds
    return now - due


class _RepeatLogSuppressor:
    """Collapse a repeating failure into one log line per interval.

    A camera that stays unreachable re-fails every retry, and logging each one
    buries real errors and churns the log files. The first failure is logged
    immediately, then at most one line per REPEAT_LOG_INTERVAL_SECONDS, carrying
    a count of what was suppressed in between. ``reset`` on recovery so the next
    outage logs loudly again.
    """

    def __init__(self, interval_seconds: float = REPEAT_LOG_INTERVAL_SECONDS) -> None:
        self._interval = interval_seconds
        self._state: dict[str, tuple[float, int]] = {}

    def should_log(self, key: str) -> tuple[bool, int]:
        """Return (log_this_one, suppressed_since_last_logged)."""
        now = time.monotonic()
        last, suppressed = self._state.get(key, (0.0, 0))
        if not last or now - last >= self._interval:
            self._state[key] = (now, 0)
            return True, suppressed
        self._state[key] = (last, suppressed + 1)
        return False, 0

    def reset(self, key: str) -> None:
        self._state.pop(key, None)


_repeat_log = _RepeatLogSuppressor()


@dataclass
class StreamInfo:
    camera_id: int
    camera_name: str
    rtsp_url: str
    sub_stream_url: str | None = None
    record_audio: bool = False
    # "ts" | "shadow" | "riv". With "riv" no ffmpeg recorder runs at all: the
    # keepalive's fMP4 feeds the RIV writer (services/riv).
    rec_format: str = "ts"
    # Source audio codec, probed once per stream when record_audio is on
    # (None = not known yet, "" = the camera sends no audio).
    audio_codec: str | None = None
    rec_process: asyncio.subprocess.Process | None = None
    started_at: float = 0.0
    restart_count: int = 0
    last_error: str | None = None
    banner_logged: bool = False
    _rec_monitor_task: asyncio.Task | None = field(default=None, repr=False)
    _watchdog_task: asyncio.Task | None = field(default=None, repr=False)
    _main_keepalive_task: asyncio.Task | None = field(default=None, repr=False)
    _sub_keepalive_task: asyncio.Task | None = field(default=None, repr=False)


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
        self, camera_id: int, camera_name: str, rtsp_url: str, sub_stream_url: str | None = None,
        record_audio: bool = False, rec_format: str = "ts",
    ) -> None:
        """Start a recording ffmpeg process for a camera.

        Recording reads go2rtc's relay of the main stream (see _recording_source),
        falling back to the camera directly when go2rtc is down. Live view goes
        through go2rtc with keepalive consumers.
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
            record_audio=bool(record_audio),
            rec_format=rec_format or "ts",
        )
        self._streams[camera_id] = info

        if info.rec_format == "riv":
            info.started_at = time.time()
            info._watchdog_task = asyncio.create_task(self._riv_watchdog(camera_id))
        else:
            await _launch_recording(info, config)
            info._rec_monitor_task = asyncio.create_task(
                self._monitor_process(camera_id)
            )
            info._watchdog_task = asyncio.create_task(
                self._watchdog(camera_id)
            )
        # Keep go2rtc's streams alive AND feed the live GOP cache, which is
        # what makes a joining client's first frame instant instead of one
        # keyframe interval away (see services/live_cache.py). The sub stream
        # gets its own consumer now: the FrameBroker already keeps that
        # producer warm, but it reads MJPEG, so it yields nothing this cache
        # can hand to a client. A second fMP4 consumer on an already-running
        # producer costs the camera nothing and the box ~0.5 Mbit/s.
        main_delay = 3 + self._keepalive_index
        self._keepalive_index += 1
        info._main_keepalive_task = asyncio.create_task(
            self._go2rtc_keepalive(camera_id, "s1_direct", startup_delay=main_delay)
        )
        info._sub_keepalive_task = asyncio.create_task(
            self._go2rtc_keepalive(camera_id, "s2_direct", startup_delay=main_delay + 0.5)
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
        if info._main_keepalive_task:
            info._main_keepalive_task.cancel()
            info._main_keepalive_task = None
        if info._sub_keepalive_task:
            info._sub_keepalive_task.cancel()
            info._sub_keepalive_task = None

        from app.services.live_cache import get_live_cache
        cache = get_live_cache()
        base = get_stream_name(info.camera_name)
        for suffix in ("s1_direct", "s2_direct"):
            cache.drop(f"{base}_{suffix}")

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
            should_log, suppressed = _repeat_log.should_log(f"rec_died:{camera_id}")
            if should_log:
                logger.error(
                    "Recording process died, restarting",
                    extra={
                        "camera_id": camera_id,
                        "returncode": returncode,
                        "restart_count": info.restart_count,
                        "suppressed_since_last": suppressed,
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
                # Already dead, the monitor relaunches it. Keep watching: this
                # used to `break`, so one death left the camera's recorder
                # unwatched until the service restarted.
                continue

            # A runaway recorder keeps writing its file, so the staleness check
            # below never sees it. Check its memory first.
            mem_mb = _process_memory_mb(info.rec_process.pid)
            if mem_mb is not None and mem_mb > RECORDER_MAX_MEMORY_MB:
                logger.error(
                    "Watchdog: recorder memory runaway, killing ffmpeg",
                    extra={
                        "camera_id": camera_id,
                        "camera_name": info.camera_name,
                        "memory_mb": round(mem_mb),
                        "limit_mb": RECORDER_MAX_MEMORY_MB,
                    },
                )
                info.last_error = f"Watchdog: recorder used {round(mem_mb)} MB"
                info.rec_process.kill()
                continue  # _monitor_process relaunches it

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

            overrun = _segment_overrun_seconds(today_dir, config.ffmpeg.segment_duration, now)
            if overrun is not None and overrun > SEGMENT_OVERRUN_SECONDS:
                logger.error(
                    "Watchdog: recorder stopped rolling segments, killing ffmpeg",
                    extra={
                        "camera_id": camera_id,
                        "camera_name": info.camera_name,
                        "overrun_seconds": round(overrun),
                    },
                )
                info.last_error = "Watchdog: segment never rolled (no video?)"
                info.rec_process.kill()
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

        Opens an HTTP streaming connection to go2rtc's fMP4 API, keeping the
        camera RTSP connection alive for instant live view. In-process (no
        ffmpeg subprocess), uses HTTP (avoids RTSP concurrent map bug).

        The bytes used to be discarded. They now feed the live GOP cache
        (services/live_cache.py), which holds the init segment plus the
        fragments since the most recent keyframe and replays them to a
        joining client. That is the difference between a client's first
        decodable frame arriving in ~50 ms and it arriving one keyframe
        interval later (2.0-4.0 s on these cameras, measured). It also means
        every viewer of a camera is served from THIS one connection rather
        than opening its own go2rtc consumer.
        """
        from app.services.live_cache import get_live_cache
        live_cache = get_live_cache()
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
                # A reconnect means a fresh init segment; anything cached from
                # the old session is undecodable against it.
                live_cache.reset(full_stream)
                t_connect = _time.monotonic()
                async with httpx.AsyncClient(timeout=httpx.Timeout(
                    connect=10, read=60, write=10, pool=10
                )) as client:
                    async with client.stream("GET", fmp4_url) as resp:
                        if resp.status_code != 200:
                            should_log, suppressed = _repeat_log.should_log(
                                f"keepalive:{full_stream}")
                            if should_log:
                                logger.warning("Keepalive HTTP error",
                                               extra={"camera_id": camera_id,
                                                      "stream": full_stream,
                                                      "status": resp.status_code,
                                                      "suppressed_since_last": suppressed})
                            await asyncio.sleep(5)
                            continue

                        connect_ms = round((_time.monotonic() - t_connect) * 1000, 1)
                        # Read first chunk to confirm data is flowing
                        first_chunk = True
                        # No chunk_size: httpx then holds bytes back until a full
                        # chunk has arrived, which released a 2.8 Mbit/s stream to
                        # viewers in lumps up to 1 s apart (found 2026-11-19).
                        async for _chunk in resp.aiter_bytes():
                            live_cache.feed(full_stream, _chunk)
                            if first_chunk:
                                first_chunk_ms = round((_time.monotonic() - t_connect) * 1000, 1)
                                _repeat_log.reset(f"keepalive:{full_stream}")
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
                live_cache.reset(full_stream)
                return
            except Exception:
                if not self._running:
                    live_cache.reset(full_stream)
                    return
                logger.debug("Keepalive error, retrying in 5s",
                             extra={"camera_id": camera_id, "stream": full_stream})
            await asyncio.sleep(5)

    async def _riv_watchdog(self, camera_id: int) -> None:
        """RIV cameras: no video written for RIV_VIDEO_STALL_SECONDS while the
        keepalive is up means a connection delivering nothing (or audio only,
        the Front Door failure of 2026-11-19), which its 60 s read timeout never
        sees. Restart the main keepalive so go2rtc opens a fresh session."""
        from app.services.riv.manager import get_riv_manager
        await asyncio.sleep(120)
        while self._running and camera_id in self._streams:
            await asyncio.sleep(15)
            info = self._streams.get(camera_id)
            if info is None:
                return
            idle = get_riv_manager().video_idle_seconds(camera_id, "s1")
            if idle is None or idle < RIV_VIDEO_STALL_SECONDS:
                continue
            logger.warning("RIV: no video written, restarting the main keepalive",
                           extra={"camera_id": camera_id, "camera": info.camera_name,
                                  "idle_s": round(idle)})
            if info._main_keepalive_task:
                info._main_keepalive_task.cancel()
            info._main_keepalive_task = asyncio.create_task(
                self._go2rtc_keepalive(camera_id, "s1_direct"))
            info.restart_count += 1
            await asyncio.sleep(RIV_VIDEO_STALL_SECONDS)

    def get_status(self, camera_id: int) -> dict:
        """Get status info for a stream."""
        info = self._streams.get(camera_id)
        if not info:
            return {"running": False}
        if info.rec_format == "riv":
            from app.services.riv.manager import get_riv_manager
            idle = get_riv_manager().video_idle_seconds(camera_id, "s1")
            running = idle is not None and idle < 30
            return {"running": running, "pid": None,
                    "uptime_seconds": time.time() - info.started_at if running else None,
                    "restart_count": info.restart_count,
                    "error": None if running else "RIV: no video written recently",
                    "format": "riv"}

        running = info.rec_process is not None and info.rec_process.returncode is None
        uptime = time.time() - info.started_at if running else None

        return {
            "running": running,
            "pid": info.rec_process.pid if info.rec_process else None,
            "uptime_seconds": uptime,
            "restart_count": info.restart_count,
            "error": info.last_error,
        }


def _recording_source(info: StreamInfo) -> str:
    """Pick the RTSP URL the recorder reads from.

    Normally go2rtc's local relay of the main stream, so each camera serves ONE
    main-stream session (go2rtc's) instead of two. Recording directly from the
    camera alongside go2rtc doubled the load on the camera uplink: measured
    2026-09-17 at ~29 Mbit/s of duplicate traffic across 7 cameras sharing a
    100 Mbps link. Falls back to the camera when go2rtc is not running, so a
    go2rtc outage still does not stop recording; the next recorder restart after
    go2rtc is back returns to the relay.
    """
    from app.services.go2rtc_manager import get_rtsp_port, is_managed

    if not is_managed():
        logger.warning("go2rtc not running, recording direct from camera",
                       extra={"camera_id": info.camera_id})
        return info.rtsp_url
    stream_name = get_stream_name(info.camera_name)
    return f"rtsp://127.0.0.1:{get_rtsp_port()}/{stream_name}_s1_direct"


async def _launch_recording(info: StreamInfo, config: AppConfig) -> None:
    """Launch the recording-only ffmpeg subprocess."""
    source = _recording_source(info)
    if info.record_audio and info.audio_codec is None:
        info.audio_codec = await probe_audio_codec(source, config)
        logger.info("Probed recording audio",
                    extra={"camera_id": info.camera_id, "audio_codec": info.audio_codec})
    cmd = build_recording_command(info.camera_name, source, config,
                                  record_audio=info.record_audio,
                                  audio_codec=info.audio_codec)
    logger.debug("Launching recording ffmpeg", extra={"cmd": _redact_rtsp(" ".join(cmd)), "camera_id": info.camera_id})

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

_RTSP_CREDS_RE = re.compile(r"(rtsp://)[^@]+@", re.IGNORECASE)


def _redact_rtsp(text: str) -> str:
    """Replace rtsp://user:pass@host with rtsp://***@host in log output."""
    return _RTSP_CREDS_RE.sub(r"\1***@", text)


async def _read_stderr(info: StreamInfo, label: str) -> None:
    """Read ffmpeg stderr: log the banner once per camera, then only warnings/errors.

    ``info.banner_logged`` is deliberately on the camera, not on this process. An
    unreachable camera never reaches ffmpeg's progress output, so the banner phase
    never ends and every relaunch would otherwise re-log the whole build banner -
    the bulk of the log churn during an outage.
    """
    proc = info.rec_process
    if not proc or not proc.stderr:
        return
    in_banner = True
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

                # Banner ends when ffmpeg starts outputting progress (frame= or size=).
                # Reaching it means the camera is actually streaming, so the outage
                # suppressors reset and the next failure logs loudly again.
                if in_banner and (line.startswith("frame=") or line.startswith("size=")):
                    in_banner = False
                    info.banner_logged = True
                    _repeat_log.reset(f"rec_died:{info.camera_id}")
                    continue

                # Codec/stream info banner: only worth logging the first time a
                # camera starts. Error lines still fall through to the check below.
                if in_banner and not info.banner_logged:
                    if not _FFMPEG_ERROR_KEYWORDS.search(line):
                        logger.info(
                            f"ffmpeg-{label} init",
                            extra={"camera_id": info.camera_id,
                                   "output": _redact_rtsp(line[:500])},
                        )
                        continue

                # Only log lines with warning/error indicators, throttled so a
                # persistently failing camera does not flood the log every retry
                if _FFMPEG_ERROR_KEYWORDS.search(line):
                    should_log, suppressed = _repeat_log.should_log(
                        f"ffmpeg_err:{info.camera_id}:{label}")
                    if should_log:
                        logger.warning(
                            f"ffmpeg-{label}",
                            extra={"camera_id": info.camera_id,
                                   "output": _redact_rtsp(line[:500]),
                                   "suppressed_since_last": suppressed},
                        )
    except Exception:
        logger.exception(f"ffmpeg-{label} stderr reader failed", extra={"camera_id": info.camera_id})
    finally:
        # At most one banner per camera per backend run. A camera that is down never
        # reaches the frame=/size= progress output, so without this its banner would
        # be re-logged on every relaunch for as long as the outage lasts.
        info.banner_logged = True


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
