"""On-demand playback: HEVC recordings → MP4 remux/transcode for browser playback."""

import asyncio
import logging
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path

from app.config import get_bootstrap, get_config
from app.services.ffmpeg import TS_INPUT_ARGS, ts_audio_bsf_args
from app.services.benchmark import BenchmarkTrace
from app.services.job_object import assign_to_job
from app.services.reverse_playback import ReverseRenderer

logger = logging.getLogger(__name__)


def _get_playback_dir() -> Path:
    """Return the playback cache directory under data_dir."""
    return Path(get_bootstrap().data_dir) / "playback"
IDLE_TIMEOUT = 30  # seconds before cleanup

# Default recording bitrate (kbps) if probing fails.
DEFAULT_RECORDING_BITRATE_KBPS = 4000

# Largest frame the box's iGPU (UHD 630, iHD driver) transcodes at native size.
# Measured 2026-11-16: 5376x1520 fails in the HEVC decoder and 4512x2512 in the
# encoder, both exiting 0 with an empty output. Bigger sources skip straight to
# the software-decode + downscale variant; anything else that fails at runtime
# walks the same chain (see PlaybackSession.fallback_cmds).
VAAPI_MAX_WIDTH = 4096
VAAPI_MAX_HEIGHT = 2176
FALLBACK_MAX_WIDTH = 1920

# A streaming session counts as started once ffmpeg has written this much
# (the fMP4 header plus a first fragment).
_STREAM_READY_BYTES = 4096


async def probe_duration(path: str) -> float | None:
    """Duration in seconds of a local media file via ffprobe (None on failure).

    Cheap on a .ts even while it is still being written: ffprobe reads the tail
    for the last timestamp rather than scanning the file.
    """
    config = get_config()
    cmd = [
        config.ffmpeg.ffprobe_path, "-v", "quiet", *TS_INPUT_ARGS,
        "-show_entries", "format=duration", "-of", "csv=p=0", path,
    ]
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=10)
        return float(stdout.decode().strip())
    except Exception:  # noqa: BLE001
        logger.warning("Failed to probe duration", extra={"path": path})
        return None


async def _probe_file(path: str) -> tuple[int | None, str | None, int, int]:
    """Probe a local .ts file's video bitrate (kbps), codec and frame size via ffprobe."""
    config = get_config()
    cmd = [
        config.ffmpeg.ffprobe_path, *TS_INPUT_ARGS,
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
        width = height = 0
        for stream in data.get("streams", []):
            if stream.get("codec_type") == "video":
                codec = stream.get("codec_name", "").lower() or None
                width = int(stream.get("width") or 0)
                height = int(stream.get("height") or 0)
                break
        return kbps, codec, width, height
    except Exception:
        logger.exception("Failed to probe file", extra={"path": path})
    return None, None, 0, 0


def _build_playback_preset(
    quality: str, source_kbps: int, source_codec: str = "hevc",
    hwaccel: str = "cuda", sw_downscale: bool = False,
) -> dict:
    """Build a playback quality preset.

    sw_downscale (vaapi only): decode on the CPU, shrink to at most
    FALLBACK_MAX_WIDTH wide, then upload to the GPU for the encode. For frames
    the iGPU cannot take at native size (see VAAPI_MAX_WIDTH).

    High = HEVC re-encode at source bitrate.
    Low = 1/8 of source bitrate.
    Ultra-low = 1/16 of source bitrate, 15fps, no B-frames, short GOP.

    hwaccel: "cuda" (NVENC), "vaapi" (Intel iGPU), anything else = software x264.
    """
    if quality == "direct":
        return {
            "pre_input": [],
            "codec": ["-c", "copy"],
            "movflags": "frag_keyframe+empty_moov",
            "streaming": True,
        }
    high_br = f"{source_kbps}k"
    low_br = f"{max(source_kbps // 8, 500)}k"
    ultralow_br = f"{max(source_kbps // 16, 300)}k"

    bitrate = {"high": high_br, "low": low_br, "ultralow": ultralow_br}.get(quality, low_br)
    rate_extra = ["-r", "15", "-bf", "0", "-g", "30"] if quality == "ultralow" else []

    if hwaccel == "cuda":
        pre_input = ["-hwaccel", "cuda"]
        codec = ["-c:v", "hevc_nvenc", "-preset", "p4",
                 "-b:v", bitrate, *rate_extra, "-c:a", "copy"]
    elif hwaccel == "vaapi" and sw_downscale:
        pre_input = ["-vaapi_device", "/dev/dri/renderD128"]
        codec = ["-vf", f"scale='min({FALLBACK_MAX_WIDTH},iw)':-2,format=nv12,hwupload",
                 "-c:v", "hevc_vaapi", "-rc_mode", "VBR",
                 "-b:v", bitrate, "-maxrate", bitrate,
                 "-bufsize", f"{2 * int(bitrate[:-1])}k",
                 *rate_extra, "-c:a", "copy"]
    elif hwaccel == "vaapi":
        # Full-GPU path: decode output stays as vaapi surfaces, scale_vaapi
        # normalizes the surface format for the encoder without a download.
        pre_input = ["-hwaccel", "vaapi", "-hwaccel_device", "/dev/dri/renderD128",
                     "-hwaccel_output_format", "vaapi"]
        codec = ["-vf", "scale_vaapi=format=nv12",
                 "-c:v", "hevc_vaapi", "-rc_mode", "VBR",
                 "-b:v", bitrate, "-maxrate", bitrate,
                 "-bufsize", f"{2 * int(bitrate[:-1])}k",
                 *rate_extra, "-c:a", "copy"]
    else:
        pre_input = []
        codec = ["-c:v", "libx264", "-preset", "veryfast",
                 "-b:v", bitrate, "-pix_fmt", "yuv420p",
                 *rate_extra, "-c:a", "copy"]

    return {
        "pre_input": pre_input,
        "codec": codec,
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
    direction: str = "forward"
    renderer: ReverseRenderer | None = None  # backward sessions only
    # Forward transcodes only: (label, ffmpeg command) to try, in order, when
    # the running one produces nothing. Always ends in a plain stream copy.
    fallback_cmds: list[tuple[str, list[str]]] = field(default_factory=list)
    variant: str = ""  # label of the command currently running
    _ready_event: asyncio.Event = field(default_factory=asyncio.Event)
    _bench: BenchmarkTrace | None = None


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
        bench: BenchmarkTrace | None = None, direction: str = "forward",
        reverse_plan: list[tuple[float, float]] | None = None, reverse_input=None,
    ) -> PlaybackSession:
        """Start a playback session. High = instant remux, medium/low = NVENC transcode.

        direction="backward" ignores quality and renders the segment in reverse
        from seek_seconds back to its start (see reverse_playback.py); the
        client plays the result forward.
        """
        # Return existing session if still valid
        if session_id in self._sessions:
            session = self._sessions[session_id]
            session.last_access = time.time()
            if bench:
                bench.mark("session_cache_hit", session_id=session_id)
            return session

        # Kill old sessions for the same camera to prevent ffmpeg accumulation.
        # The new session has its own session_id (and output dir), so the old
        # one's teardown — process.wait + shutil.rmtree — can happen in the
        # background. Awaiting it here would block the event loop for ~1s when
        # multiple cameras evict at once (stop_session is partly synchronous).
        if camera_id is not None:
            stale = [
                sid for sid, s in self._sessions.items()
                if s.camera_id == camera_id and sid != session_id
            ]
            # Detach stale sessions from the registry immediately so they don't
            # match future lookups; cleanup runs as a background task.
            for sid in stale:
                logger.info("Evicting old playback session", extra={"session_id": sid, "camera_id": camera_id})
                detached = self._sessions.pop(sid, None)
                if detached:
                    asyncio.create_task(self._teardown(detached))
            if bench and stale:
                bench.mark("evicted_old_sessions", count=len(stale))

        self._ensure_cleanup()

        output_dir = _get_playback_dir() / session_id
        output_dir.mkdir(parents=True, exist_ok=True)

        session = PlaybackSession(session_id=session_id, output_dir=output_dir, camera_id=camera_id)
        session._bench = bench
        # NOTE: intentionally NOT registered in self._sessions yet. A concurrent
        # start_session for the same camera would otherwise find this entry via
        # the stale-scan below and schedule its teardown (which rmtree's the
        # output dir), breaking our own ffmpeg spawn with "No such file or
        # directory". Registration happens after ffmpeg is running.

        config = get_config()
        output_path = output_dir / "playback.mp4"

        if direction == "backward":
            session.direction = "backward"
            session.streaming = True
            session.renderer = ReverseRenderer(
                ffmpeg=config.ffmpeg.path, hwaccel=config.ffmpeg.hwaccel,
                source=segment_paths[0] if segment_paths else "riv", end_offset=seek_seconds,
                output_path=output_path,
                plan=reverse_plan, input_for=reverse_input,
            )
            logger.info(
                "Starting reverse playback render",
                extra={"session_id": session_id, "end_offset": round(seek_seconds, 2),
                       "chunks": session.renderer.chunk_count},
            )
            session.process = await session.renderer.start()
            self._sessions[session_id] = session
            if bench:
                bench.mark("reverse_render_started", pid=session.process.pid)
            asyncio.create_task(self._wait_ready(session))
            return session

        # Probe source bitrate, codec and frame size from first segment
        source_kbps = DEFAULT_RECORDING_BITRATE_KBPS
        source_codec = "hevc"
        width = height = 0
        if quality != "direct" and segment_paths:
            probed_kbps, probed_codec, width, height = await _probe_file(segment_paths[0])
            if probed_kbps:
                source_kbps = probed_kbps
            if probed_codec:
                source_codec = probed_codec
            logger.info("Probed recording", extra={"kbps": source_kbps, "codec": source_codec,
                                                   "quality": quality, "size": f"{width}x{height}"})
            if bench:
                bench.mark("probe_done", kbps=source_kbps, codec=source_codec)

        # Variants to try in order: the requested quality, then (vaapi) the same
        # bitrate through a software decode + downscale, then a stream copy.
        hwaccel = config.ffmpeg.hwaccel
        presets: list[tuple[str, dict]] = []
        if quality == "direct":
            presets.append(("direct", _build_playback_preset("direct", source_kbps)))
        else:
            oversize = width > VAAPI_MAX_WIDTH or height > VAAPI_MAX_HEIGHT
            if not (hwaccel == "vaapi" and oversize):
                presets.append((quality, _build_playback_preset(
                    quality, source_kbps, source_codec, hwaccel=hwaccel)))
            if hwaccel == "vaapi":
                presets.append((f"{quality}-downscaled", _build_playback_preset(
                    quality, source_kbps, source_codec, hwaccel=hwaccel, sw_downscale=True)))
            presets.append(("direct", _build_playback_preset("direct", source_kbps)))

        audio_bsf = ts_audio_bsf_args(segment_paths)
        concat_list_path = output_dir / "concat_list.txt"
        if len(segment_paths) > 1:
            # Write concat demuxer list file
            with open(concat_list_path, "w") as f:
                f.write("ffconcat version 1.0\n")
                for p in segment_paths:
                    escaped = p.replace("\\", "/").replace("'", "'\\''")
                    f.write(f"file '{escaped}'\n")

        def build_cmd(preset: dict) -> list[str]:
            pre_input = preset["pre_input"]
            codec_args = [*preset["codec"], *audio_bsf]
            movflags = preset.get("movflags", "+faststart")
            # For single file: direct input with fast seek (-ss before -i)
            # For multiple files: use concat demuxer (no seek support, use -ss after -i)
            # -noaccurate_seek: lands on the nearest keyframe instead of decoding to
            # the exact frame. Safe because we're using -c copy (no re-encode anyway)
            # and saves the keyframe-search work that dominates startup.
            # -avoid_negative_ts make_zero: shifts timestamps so first frame starts at
            # pts=0. Fixes green frames caused by .ts segments with initial timestamp
            # offset (e.g., first keyframe at pts=1.4s instead of 0).
            if len(segment_paths) == 1:
                return [
                    config.ffmpeg.path, "-y",
                    *pre_input, *TS_INPUT_ARGS,
                    "-noaccurate_seek",
                    "-ss", str(seek_seconds),
                    "-i", segment_paths[0],
                    "-t", str(duration_limit),
                    *codec_args,
                    "-avoid_negative_ts", "make_zero",
                    "-movflags", movflags,
                    str(output_path),
                ]
            return [
                config.ffmpeg.path, "-y",
                *pre_input, *TS_INPUT_ARGS,
                "-f", "concat", "-safe", "0",
                "-i", str(concat_list_path),
                "-noaccurate_seek",
                "-ss", str(seek_seconds),
                "-t", str(duration_limit),
                *codec_args,
                "-avoid_negative_ts", "make_zero",
                "-movflags", movflags,
                str(output_path),
            ]

        cmds = [(label, build_cmd(preset)) for label, preset in presets]
        session.streaming = presets[0][1].get("streaming", False)
        session.variant, cmd = cmds[0]
        session.fallback_cmds = cmds[1:]

        logger.info(
            "Starting playback remux",
            extra={"session_id": session_id, "segments": len(segment_paths),
                   "seek_seconds": seek_seconds, "variant": session.variant},
        )

        session.process = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        assign_to_job(session.process.pid)
        # ffmpeg is live and owns the output dir — safe to publish the session
        # to the registry where future eviction scans can find it.
        self._sessions[session_id] = session
        if bench:
            bench.mark("ffmpeg_spawned", pid=session.process.pid, streaming=session.streaming)

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
            while True:
                if await self._wait_stream_output(session, output_path):
                    session.ready = True
                    logger.info("Streaming playback ready",
                                extra={"session_id": session.session_id, "variant": session.variant})
                    if session._bench:
                        session._bench.mark("fmp4_header_written",
                                            bytes=output_path.stat().st_size)
                    break
                stderr = await self._drain_stderr(session)
                live = self._sessions.get(session.session_id) is session
                if not (session.fallback_cmds and live):
                    # Nothing left to try: serve whatever exists, as before.
                    if output_path.exists() and output_path.stat().st_size > 0:
                        session.ready = True
                    else:
                        logger.error(
                            "Streaming playback failed to start",
                            extra={"session_id": session.session_id, "variant": session.variant,
                                   "returncode": session.process.returncode, "stderr": stderr},
                        )
                    break
                failed = session.variant
                session.variant, cmd = session.fallback_cmds.pop(0)
                logger.warning(
                    "Playback variant produced no output, falling back",
                    extra={"session_id": session.session_id, "failed": failed,
                           "next": session.variant, "stderr": stderr},
                )
                if session.process.returncode is None:
                    session.process.kill()
                    await session.process.wait()
                output_path.unlink(missing_ok=True)
                session.process = await asyncio.create_subprocess_exec(
                    *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                )
                assign_to_job(session.process.pid)
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
            if session._bench:
                session._bench.mark("remux_complete", bytes=output_path.stat().st_size)
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

    @staticmethod
    async def _wait_stream_output(session: PlaybackSession, output_path: Path) -> bool:
        """True once the running ffmpeg has written a playable start (30 s cap)."""
        def started() -> bool:
            return output_path.exists() and output_path.stat().st_size > _STREAM_READY_BYTES

        for _ in range(300):
            await asyncio.sleep(0.1)
            if started():
                return True
            if session.process.returncode is not None:
                break
        return started()

    @staticmethod
    async def _drain_stderr(session: PlaybackSession) -> str:
        """Tail of the current ffmpeg's stderr, without blocking on a live process."""
        if not session.process or not session.process.stderr:
            return ""
        try:
            data = await asyncio.wait_for(session.process.stderr.read(65536), timeout=1)
        except asyncio.TimeoutError:
            return ""
        return data.decode("utf-8", errors="replace")[-500:]

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
        await self._teardown(session)

    async def _teardown(self, session: PlaybackSession) -> None:
        """Kill the ffmpeg process and remove the output dir.

        Safe to call after the session has already been popped from the registry
        (used by the eviction fast-path in start_session).
        """
        if session.renderer:
            session.renderer.stop()
        if session.process and session.process.returncode is None:
            session.process.kill()
            try:
                await asyncio.wait_for(session.process.wait(), timeout=5)
            except asyncio.TimeoutError:
                pass
        if session.output_dir.exists():
            await asyncio.to_thread(
                shutil.rmtree, session.output_dir, ignore_errors=True
            )
        logger.info("Playback session cleaned up", extra={"session_id": session.session_id})

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
