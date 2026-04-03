"""REST API client for go2rtc stream registration."""

import asyncio
import logging
import re
from urllib.parse import quote

import httpx

from app.config import get_config

logger = logging.getLogger(__name__)

# Quality profiles: suffix → (ffmpeg hash params or None, source key).
# source key: "main" = rtsp_url (stream1), "sub" = sub_stream_url (stream2).
# go2rtc lazily connects — unused quality streams consume zero resources.
QUALITY_PROFILES: dict[str, tuple[str | None, str]] = {
    "_s1_direct": (None, "main"),                                        # native passthrough (HEVC)
    "_s1_high":   ("#video=h264", "main"),                               # native res H.264 re-encode
    "_s1_low":    ("#video=h264#raw=-b:v#raw=2M", "main"),                 # native res H.264 reduced bitrate
    "_s2_direct": (None, "sub"),                                         # native passthrough
    "_s2_high":   ("#video=h264", "sub"),                                # native res H.264 re-encode
    "_s2_low":    ("#video=h264#raw=-b:v#raw=500k", "sub"),              # native res H.264 reduced bitrate
}


def get_stream_name(camera_name: str) -> str:
    """Convert camera name to go2rtc stream key (lowercase, underscored)."""
    name = camera_name.lower().strip()
    name = re.sub(r"[^a-z0-9]+", "_", name)
    return name.strip("_")


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

        Registers 6 variants: S1/S2 × Direct/High/Low.
        S1 uses rtsp_url (main stream), S2 uses sub_stream_url (or rtsp_url fallback).
        go2rtc lazily connects so unused qualities cost nothing.
        """
        stream_name = get_stream_name(camera_name)
        sources = {"main": rtsp_url, "sub": sub_stream_url or rtsp_url}

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
            for suffix, (params, source_key) in QUALITY_PROFILES.items():
                key = f"{stream_name}{suffix}"
                raw_url = sources[source_key]
                source_url = raw_url if params is None else f"ffmpeg:{raw_url}{params}"
                tasks.append(_register_one(client, key, source_url))
            await asyncio.gather(*tasks)

    async def remove_stream(self, camera_name: str) -> None:
        """Remove all quality variants of a camera stream from go2rtc."""
        stream_name = get_stream_name(camera_name)

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
                for suffix in QUALITY_PROFILES
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
