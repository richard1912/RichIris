"""REST API client for go2rtc stream registration."""

import logging
import re
from urllib.parse import quote

import httpx

from app.config import get_config

logger = logging.getLogger(__name__)


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
        """Register a camera stream with go2rtc.

        Wraps the RTSP URL with ffmpeg: prefix to transcode HEVC→H.264
        for MSE browser compatibility. Sub-streams are 640x480 so
        transcode cost is negligible.
        """
        stream_name = get_stream_name(camera_name)
        raw_url = sub_stream_url or rtsp_url
        # ffmpeg: prefix tells go2rtc to transcode via ffmpeg; #video=h264 forces H.264 output
        source_url = f"ffmpeg:{raw_url}#video=h264"
        url = f"{self._base_url}/api/streams?name={quote(stream_name)}&src={quote(source_url)}"

        logger.info(
            "Registering stream with go2rtc",
            extra={"stream_name": stream_name, "source": source_url},
        )
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.put(url)
                resp.raise_for_status()
            logger.info("Stream registered with go2rtc", extra={"stream_name": stream_name})
        except Exception:
            logger.exception("Failed to register stream with go2rtc", extra={"stream_name": stream_name})

    async def remove_stream(self, camera_name: str) -> None:
        """Remove a camera stream from go2rtc."""
        stream_name = get_stream_name(camera_name)
        url = f"{self._base_url}/api/streams?src={quote(stream_name)}"

        logger.info("Removing stream from go2rtc", extra={"stream_name": stream_name})
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.delete(url)
                resp.raise_for_status()
        except Exception:
            logger.exception("Failed to remove stream from go2rtc", extra={"stream_name": stream_name})

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
