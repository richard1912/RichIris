"""HLS stream serving endpoints."""

import logging
from pathlib import Path

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse

from app.config import get_config
from app.services.ffmpeg import sanitize_camera_name
from app.services.stream_manager import get_stream_manager

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/streams", tags=["streams"])


@router.get("/{camera_id}/index.m3u8")
async def get_live_playlist(camera_id: int):
    """Serve the HLS playlist for a camera's live stream."""
    mgr = get_stream_manager()
    status = mgr.get_status(camera_id)
    if not status["running"]:
        raise HTTPException(status_code=404, detail="Stream not running")

    info = mgr.streams.get(camera_id)
    if not info:
        raise HTTPException(status_code=404, detail="Stream not found")

    # Start live process on-demand if not running
    if not mgr.is_live_running(camera_id):
        await mgr.start_live(camera_id)

    mgr.touch_live(camera_id)

    # Wait for HLS playlist to be created (first segment can take 10+ seconds)
    playlist_path = _get_playlist_path(info.camera_name)
    if not playlist_path.exists():
        import asyncio
        for _ in range(40):  # up to 20 seconds
            await asyncio.sleep(0.5)
            mgr.touch_live(camera_id)  # keep alive while waiting
            if playlist_path.exists():
                break
            # Bail if the live process died
            if not mgr.is_live_running(camera_id):
                raise HTTPException(status_code=503, detail="Live stream failed to start")
        else:
            raise HTTPException(status_code=503, detail="Live stream starting, try again shortly")

    return FileResponse(
        playlist_path,
        media_type="application/vnd.apple.mpegurl",
        headers={"Cache-Control": "no-cache, no-store"},
    )


@router.get("/{camera_id}/{filename}")
async def get_live_segment(camera_id: int, filename: str):
    """Serve an HLS segment file for a camera's live stream."""
    mgr = get_stream_manager()
    info = mgr.streams.get(camera_id)
    if not info:
        raise HTTPException(status_code=404, detail="Stream not found")

    mgr.touch_live(camera_id)

    segment_path = _get_live_dir(info.camera_name) / filename
    if not segment_path.exists():
        raise HTTPException(status_code=404, detail="Segment not found")

    return FileResponse(segment_path, media_type="video/mp2t")


def _get_live_dir(camera_name: str) -> Path:
    """Get the live HLS directory for a camera."""
    config = get_config()
    safe_name = sanitize_camera_name(camera_name)
    return Path(config.storage.live_dir) / safe_name


def _get_playlist_path(camera_name: str) -> Path:
    """Get the HLS playlist path for a camera."""
    return _get_live_dir(camera_name) / "stream.m3u8"
