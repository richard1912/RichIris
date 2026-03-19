"""Stream endpoints — proxies go2rtc WebSocket for MSE live view."""

import asyncio
import logging

from fastapi import APIRouter, HTTPException, WebSocket, WebSocketDisconnect
import httpx
import websockets

from app.config import get_config
from app.services.go2rtc_client import get_stream_name
from app.services.stream_manager import get_stream_manager

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/streams", tags=["streams"])


@router.websocket("/{camera_id}/ws")
async def proxy_ws(websocket: WebSocket, camera_id: int):
    """Proxy WebSocket between browser and go2rtc for MSE streaming."""
    mgr = get_stream_manager()
    info = mgr.streams.get(camera_id)
    if not info:
        await websocket.close(code=4004, reason="Stream not found")
        return

    config = get_config()
    stream_name = get_stream_name(info.camera_name)
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
