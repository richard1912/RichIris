"""Stream endpoints — proxies go2rtc WebSocket for MSE live view and HTTP fMP4 for native apps."""

import asyncio
import logging

from fastapi import APIRouter, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import StreamingResponse
import httpx
import websockets

from app.config import get_config
from app.services.go2rtc_client import get_stream_name
from app.services.stream_manager import get_stream_manager

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/streams", tags=["streams"])

# Module-level connection pool for go2rtc proxying (reused across requests)
_pool: httpx.AsyncClient | None = None


def _get_pool() -> httpx.AsyncClient:
    global _pool
    if _pool is None:
        _pool = httpx.AsyncClient(
            timeout=None,
            limits=httpx.Limits(max_connections=20, max_keepalive_connections=12),
        )
    return _pool


async def close_pool() -> None:
    """Shut down the module-level connection pool."""
    global _pool
    if _pool:
        await _pool.aclose()
        _pool = None


@router.get("/{camera_id}/live.mp4")
async def proxy_fmp4(camera_id: int, stream: str = "s2", quality: str = "direct"):
    """Proxy go2rtc HTTP fMP4 stream for native app live view.

    Params:
        stream: 's1' (main 4K) or 's2' (sub-stream). Default 's2'.
        quality: 'direct', 'high', or 'low'. Default 'direct'.
    """
    mgr = get_stream_manager()
    info = mgr.streams.get(camera_id)
    if not info:
        raise HTTPException(status_code=404, detail="Stream not found")

    if stream not in ("s1", "s2"):
        stream = "s2"
    if quality not in ("direct", "high", "low", "ultralow"):
        quality = "direct"

    config = get_config()
    base_name = get_stream_name(info.camera_name)
    stream_name = f"{base_name}_{stream}_{quality}"

    go2rtc_url = f"http://127.0.0.1:{config.go2rtc.port}/api/stream.mp4?src={stream_name}"
    logger.debug("Proxying fMP4 stream", extra={"camera_id": camera_id, "go2rtc_url": go2rtc_url})

    client = _get_pool()

    async def stream_generator():
        try:
            async with client.stream("GET", go2rtc_url) as resp:
                resp.raise_for_status()
                async for chunk in resp.aiter_bytes(chunk_size=65536):
                    yield chunk
        except Exception:
            logger.debug("fMP4 proxy stream ended", extra={"camera_id": camera_id})

    return StreamingResponse(stream_generator(), media_type="video/mp4")


@router.websocket("/{camera_id}/ws")
async def proxy_ws(websocket: WebSocket, camera_id: int):
    """Proxy WebSocket between browser and go2rtc for MSE streaming."""
    mgr = get_stream_manager()
    info = mgr.streams.get(camera_id)
    if not info:
        await websocket.close(code=4004, reason="Stream not found")
        return

    config = get_config()
    base_name = get_stream_name(info.camera_name)
    ws_stream = websocket.query_params.get("stream", "s2")
    ws_quality = websocket.query_params.get("quality", "direct")
    if ws_stream not in ("s1", "s2"):
        ws_stream = "s2"
    if ws_quality not in ("direct", "high", "low", "ultralow"):
        ws_quality = "direct"
    stream_name = f"{base_name}_{ws_stream}_{ws_quality}"
    go2rtc_url = f"ws://127.0.0.1:{config.go2rtc.port}/api/ws?src={stream_name}"

    await websocket.accept()

    try:
        async with websockets.connect(go2rtc_url) as upstream:
            async def browser_to_go2rtc():
                try:
                    while True:
                        data = await websocket.receive_text()
                        await upstream.send(data)
                except WebSocketDisconnect:
                    pass

            async def go2rtc_to_browser():
                try:
                    async for msg in upstream:
                        if isinstance(msg, str):
                            await websocket.send_text(msg)
                        else:
                            await websocket.send_bytes(msg)
                except websockets.exceptions.ConnectionClosed:
                    pass

            # Run both directions concurrently; cancel both when either ends
            done, pending = await asyncio.wait(
                [asyncio.create_task(browser_to_go2rtc()),
                 asyncio.create_task(go2rtc_to_browser())],
                return_when=asyncio.FIRST_COMPLETED,
            )
            for task in pending:
                task.cancel()

    except Exception:
        logger.debug("WebSocket proxy error", extra={"camera_id": camera_id}, exc_info=True)
    finally:
        try:
            await websocket.close()
        except Exception:
            pass
