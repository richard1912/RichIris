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

    High = source-matched visual quality (2x bitrate for HEVC→H.264 conversion).
    Low = 1/4 of High bitrate.
    """
    # HEVC→H.264 needs ~2x bitrate for equivalent visual quality
    main_mult = 2 if main_codec == "hevc" else 1
    sub_mult = 2 if sub_codec == "hevc" else 1
    main_high_kbps = main_kbps * main_mult
    sub_high_kbps = sub_kbps * sub_mult
    main_high = _format_bitrate(main_high_kbps)
    main_low = _format_bitrate(max(main_high_kbps // 4, 500))
    sub_high = _format_bitrate(sub_high_kbps)
    sub_low = _format_bitrate(max(sub_high_kbps // 4, 250))

    return {
        "_s1_direct": (None, "main"),
        "_s1_high":   (f"#video=h264#raw=-b:v#raw={main_high}", "main"),
        "_s1_low":    (f"#video=h264#raw=-b:v#raw={main_low}", "main"),
        "_s2_direct": (None, "sub"),
        "_s2_high":   (f"#video=h264#raw=-b:v#raw={sub_high}", "sub"),
        "_s2_low":    (f"#video=h264#raw=-b:v#raw={sub_low}", "sub"),
    }


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
        S1/S2 × Direct/High/Low. High matches source bitrate, Low is 1/4.
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

    async def remove_stream(self, camera_name: str) -> None:
        """Remove all quality variants of a camera stream from go2rtc."""
        stream_name = get_stream_name(camera_name)
        suffixes = ["_s1_direct", "_s1_high", "_s1_low", "_s2_direct", "_s2_high", "_s2_low"]

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
