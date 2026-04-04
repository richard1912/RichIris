"""REST API client for go2rtc stream registration."""

import asyncio
import logging
import re
from urllib.parse import quote

import httpx

from app.config import get_config
from app.services.ffmpeg import probe_video_bitrate, probe_video_codec

logger = logging.getLogger(__name__)

# Default bitrates (kbps) if probing fails
DEFAULT_MAIN_BITRATE_KBPS = 4000
DEFAULT_SUB_BITRATE_KBPS = 1000


def get_stream_name(camera_name: str) -> str:
    """Convert camera name to go2rtc stream key (lowercase, underscored)."""
    name = camera_name.lower().strip()
    name = re.sub(r"[^a-z0-9]+", "_", name)
    return name.strip("_")


def _format_bitrate(kbps: int) -> str:
    """Format kbps as ffmpeg bitrate string (e.g. '4000k' or '1M')."""
    if kbps >= 1000 and kbps % 1000 == 0:
        return f"{kbps // 1000}M"
    return f"{kbps}k"


def _build_quality_profiles(
    main_kbps: int, sub_kbps: int,
    main_codec: str = "hevc", sub_codec: str = "h264",
) -> dict[str, tuple[str | None, str]]:
    """Build quality profiles with probed bitrates.

    High = source-matched visual quality (HEVC re-encode at source bitrate).
    Low = 1/8 of High bitrate.
    Ultra-low = 1/16 of source bitrate, 15fps, no B-frames, short GOP.
    """
    main_high = _format_bitrate(main_kbps)
    main_low = _format_bitrate(max(main_kbps // 8, 500))
    main_ultralow = _format_bitrate(max(main_kbps // 16, 300))
    sub_high = _format_bitrate(sub_kbps)
    sub_low = _format_bitrate(max(sub_kbps // 8, 250))
    sub_ultralow = _format_bitrate(max(sub_kbps // 16, 150))

    # Ultra-low: 15fps, no B-frames, short GOP (30 frames = 2s at 15fps)
    ul_extra = "#raw=-r#raw=15#raw=-bf#raw=0#raw=-g#raw=30"

    # Use HEVC output — matches source codec, supports 8K resolution (no 4096
    # width limit like H.264 NVENC), and is more efficient at the same bitrate.
    main_high = _format_bitrate(main_kbps)
    main_low = _format_bitrate(max(main_kbps // 8, 500))
    main_ultralow = _format_bitrate(max(main_kbps // 16, 300))
    sub_high = _format_bitrate(sub_kbps)
    sub_low = _format_bitrate(max(sub_kbps // 8, 250))
    sub_ultralow = _format_bitrate(max(sub_kbps // 16, 150))

    return {
        "_s1_direct":   (None, "main"),
        "_s1_high":     (f"#video=h265#raw=-b:v#raw={main_high}", "main"),
        "_s1_low":      (f"#video=h265#raw=-b:v#raw={main_low}", "main"),
        "_s1_ultralow": (f"#video=h265#raw=-b:v#raw={main_ultralow}{ul_extra}", "main"),
        "_s2_direct":   (None, "sub"),
        "_s2_high":     (f"#video=h265#raw=-b:v#raw={sub_high}", "sub"),
        "_s2_low":      (f"#video=h265#raw=-b:v#raw={sub_low}", "sub"),
        "_s2_ultralow": (f"#video=h265#raw=-b:v#raw={sub_ultralow}{ul_extra}", "sub"),
    }


def build_streams_config(
    cameras: list[tuple[str, str, str | None]],
    probed_bitrates: dict[str, tuple[int, int, str, str]] | None = None,
) -> dict[str, list[str]]:
    """Build the streams dict for go2rtc.yaml from camera list.

    cameras: list of (name, rtsp_url, sub_stream_url)
    probed_bitrates: optional dict of camera_name -> (main_kbps, sub_kbps, main_codec, sub_codec)

    Returns dict mapping stream names to source URL lists (go2rtc config format).
    """
    streams: dict[str, list[str]] = {}

    for name, rtsp_url, sub_url in cameras:
        stream_name = get_stream_name(name)
        sub = sub_url or rtsp_url
        sources = {"main": rtsp_url, "sub": sub}

        if probed_bitrates and name in probed_bitrates:
            main_kbps, sub_kbps, main_codec, sub_codec = probed_bitrates[name]
        else:
            main_kbps = DEFAULT_MAIN_BITRATE_KBPS
            sub_kbps = DEFAULT_SUB_BITRATE_KBPS
            main_codec = "hevc"
            sub_codec = "h264"

        profiles = _build_quality_profiles(main_kbps, sub_kbps, main_codec, sub_codec)

        for suffix, (params, source_key) in profiles.items():
            key = f"{stream_name}{suffix}"
            raw_url = sources[source_key]
            source_url = raw_url if params is None else f"ffmpeg:{raw_url}{params}"
            streams[key] = [source_url]

    return streams


class Go2rtcClient:
    """Manages camera stream registration with go2rtc."""

    def __init__(self, host: str, port: int) -> None:
        self._base_url = f"http://{host}:{port}"
        self._port = port

    @property
    def port(self) -> int:
        return self._port

    async def register_stream(
        self, camera_name: str, rtsp_url: str, sub_stream_url: str | None = None
    ) -> None:
        """Register camera streams with go2rtc at all quality levels.

        Probes camera bitrates via ffprobe, then registers 6 variants:
        S1/S2 × Direct/High/Low. High matches source bitrate, Low is 1/8.
        """
        stream_name = get_stream_name(camera_name)
        sub_url = sub_stream_url or rtsp_url
        sources = {"main": rtsp_url, "sub": sub_url}

        # Probe bitrates and codecs in parallel
        config = get_config()

        async def _noop() -> None:
            return None

        main_br, sub_br, main_codec, sub_codec = await asyncio.gather(
            probe_video_bitrate(rtsp_url, config),
            probe_video_bitrate(sub_url, config) if sub_stream_url else _noop(),
            probe_video_codec(rtsp_url, config),
            probe_video_codec(sub_url, config) if sub_stream_url else _noop(),
        )
        main_kbps = main_br or DEFAULT_MAIN_BITRATE_KBPS
        sub_kbps = sub_br or DEFAULT_SUB_BITRATE_KBPS
        logger.info(
            "Camera streams probed",
            extra={"camera": camera_name, "main_kbps": main_kbps, "sub_kbps": sub_kbps,
                   "main_codec": main_codec or "unknown", "sub_codec": sub_codec or "unknown",
                   "main_probed": main_br is not None, "sub_probed": sub_br is not None},
        )

        profiles = _build_quality_profiles(
            main_kbps, sub_kbps,
            main_codec=main_codec or "hevc",
            sub_codec=sub_codec or "h264",
        )

        async def _register_one(client: httpx.AsyncClient, key: str, source_url: str) -> None:
            url = f"{self._base_url}/api/streams?name={quote(key)}&src={quote(source_url)}"
            logger.info("Registering stream with go2rtc", extra={"stream_name": key, "source": source_url})
            try:
                resp = await client.put(url)
                resp.raise_for_status()
                logger.info("Stream registered with go2rtc", extra={"stream_name": key})
            except Exception:
                logger.exception("Failed to register stream with go2rtc", extra={"stream_name": key})

        async with httpx.AsyncClient(timeout=10) as client:
            tasks = []
            for suffix, (params, source_key) in profiles.items():
                key = f"{stream_name}{suffix}"
                raw_url = sources[source_key]
                source_url = raw_url if params is None else f"ffmpeg:{raw_url}{params}"
                tasks.append(_register_one(client, key, source_url))
            await asyncio.gather(*tasks)

    async def register_streams_from_config(self, streams: dict[str, list[str]]) -> None:
        """Register ALL streams one at a time to avoid go2rtc concurrent map writes.

        streams: dict from build_streams_config() — {name: [source_url]}
        Transcoded streams are lazy in go2rtc — defining them doesn't start
        ffmpeg until a client actually requests the stream.
        """
        async with httpx.AsyncClient(timeout=15) as client:
            for key, sources in streams.items():
                await self._register_one_stream(client, key, sources)
                await asyncio.sleep(0.1)

        logger.info("All streams registered with go2rtc", extra={
            "count": len(streams),
        })

    async def ensure_stream_registered(self, stream_name: str) -> None:
        """Register a transcoded stream on-demand if not yet registered.

        Called when a client requests a non-direct quality for live view.
        """
        if not hasattr(self, '_registered') :
            self._registered: set[str] = set()
        if stream_name in self._registered:
            return

        streams = getattr(self, '_all_streams', {})
        if stream_name not in streams:
            return

        async with httpx.AsyncClient(timeout=15) as client:
            await self._register_one_stream(client, stream_name, streams[stream_name])
        self._registered.add(stream_name)
        logger.info("On-demand stream registered", extra={"stream_name": stream_name})

    async def _register_one_stream(
        self, client: httpx.AsyncClient, key: str, sources: list[str]
    ) -> None:
        source_url = sources[0] if sources else ""
        url = f"{self._base_url}/api/streams?name={quote(key)}&src={quote(source_url)}"
        try:
            resp = await client.put(url)
            resp.raise_for_status()
            logger.debug("Stream registered", extra={"stream_name": key})
        except Exception:
            logger.warning("Failed to register stream", extra={"stream_name": key})

    async def remove_stream(self, camera_name: str) -> None:
        """Remove all quality variants of a camera stream from go2rtc."""
        stream_name = get_stream_name(camera_name)
        suffixes = ["_s1_direct", "_s1_high", "_s1_low", "_s1_ultralow",
                    "_s2_direct", "_s2_high", "_s2_low", "_s2_ultralow"]

        async def _remove_one(client: httpx.AsyncClient, key: str) -> None:
            url = f"{self._base_url}/api/streams?src={quote(key)}"
            logger.info("Removing stream from go2rtc", extra={"stream_name": key})
            try:
                resp = await client.delete(url)
                resp.raise_for_status()
            except Exception:
                logger.exception("Failed to remove stream from go2rtc", extra={"stream_name": key})

        async with httpx.AsyncClient(timeout=10) as client:
            await asyncio.gather(*[
                _remove_one(client, f"{stream_name}{suffix}")
                for suffix in suffixes
            ])

    async def is_healthy(self) -> bool:
        """Check if go2rtc is responding."""
        try:
            async with httpx.AsyncClient(timeout=5) as client:
                resp = await client.get(f"{self._base_url}/api")
                return resp.status_code == 200
        except Exception:
            return False


# Singleton
_client: Go2rtcClient | None = None


def get_go2rtc_client() -> Go2rtcClient:
    """Return the singleton Go2rtcClient instance."""
    global _client
    if _client is None:
        config = get_config()
        _client = Go2rtcClient(config.go2rtc.host, config.go2rtc.port)
    return _client


# Global semaphore to serialize go2rtc snapshot requests.
# go2rtc has a concurrent map write bug — when multiple new streams start
# simultaneously (e.g. after a crash restart), go2rtc panics. Limiting
# concurrent snapshot requests to 1 ensures stream creation is serialized.
_snapshot_semaphore: asyncio.Semaphore | None = None
# Timestamp of last go2rtc restart — snapshot consumers wait until grace period expires
_go2rtc_restart_time: float = 0.0
GO2RTC_RESTART_GRACE_SECONDS = 15  # Wait this long after restart before sending snapshots


def get_snapshot_semaphore() -> asyncio.Semaphore:
    """Return a global semaphore for serializing go2rtc snapshot requests."""
    global _snapshot_semaphore
    if _snapshot_semaphore is None:
        _snapshot_semaphore = asyncio.Semaphore(1)
    return _snapshot_semaphore


def notify_go2rtc_restart() -> None:
    """Called by go2rtc manager after a restart to trigger a grace period."""
    global _go2rtc_restart_time
    import time
    _go2rtc_restart_time = time.time()
    logger.info("go2rtc restart grace period started", extra={"seconds": GO2RTC_RESTART_GRACE_SECONDS})


async def wait_for_go2rtc_ready() -> None:
    """Wait until the go2rtc restart grace period has expired."""
    import time
    remaining = GO2RTC_RESTART_GRACE_SECONDS - (time.time() - _go2rtc_restart_time)
    if remaining > 0:
        await asyncio.sleep(remaining)
