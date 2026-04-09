"""Stream endpoints — proxies go2rtc WebSocket for MSE live view and HTTP fMP4 for native apps."""

import asyncio
import logging
import time

from fastapi import APIRouter, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import StreamingResponse
import httpx
import websockets

from app.config import get_config
from app.services.go2rtc_client import get_go2rtc_client, get_stream_name
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
async def proxy_fmp4(request: Request, camera_id: int, stream: str = "s2", quality: str = "direct"):
    """Proxy go2rtc HTTP fMP4 stream for native app live view.

    Params:
        stream: 's1' (main 4K) or 's2' (sub-stream). Default 's2'.
        quality: 'direct', 'high', or 'low'. Default 'direct'.
    """
    t_request = time.monotonic()
    original_quality = quality

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

    if quality != "direct":
        go2rtc = get_go2rtc_client()
        if not go2rtc.has_stream(stream_name):
            # High quality not available (HEVC source skips re-encode) — use direct
            logger.info("fMP4 quality fallback: high→direct (HEVC source)", extra={
                "camera_id": camera_id, "stream_name": stream_name,
            })
            quality = "direct"
            stream_name = f"{base_name}_{stream}_direct"
        # No ensure_stream_registered needed — all streams are baked into
        # go2rtc.yaml at startup and survive config reloads.

    from app.services.go2rtc_manager import get_api_port
    go2rtc_url = f"http://127.0.0.1:{get_api_port()}/api/stream.mp4?src={stream_name}"

    t_setup = time.monotonic()
    logger.info("fMP4 request", extra={
        "camera_id": camera_id, "camera": info.camera_name,
        "quality_requested": original_quality, "quality_resolved": quality,
        "stream": stream, "stream_name": stream_name,
        "setup_ms": round((t_setup - t_request) * 1000, 1),
    })

    http_client = _get_pool()

    # Connect to go2rtc and wait for first data BEFORE returning the
    # StreamingResponse. For transcoded streams, go2rtc starts an ffmpeg
    # process that can take 3-8s to produce first output. Without this
    # pre-fetch, mpv receives an HTTP 200 with no video data and times
    # out before ffmpeg is ready. By waiting here, the latency is hidden
    # in the HTTP request phase — the client only sees the response once
    # data is actually flowing.
    buf: asyncio.Queue[bytes | None] = asyncio.Queue(maxsize=128)
    resp = None
    reader_task = None

    async def upstream_reader(resp: httpx.Response) -> None:
        """Read from go2rtc into the buffer."""
        total_bytes = 0
        chunk_count = 0
        try:
            async for chunk in resp.aiter_bytes(chunk_size=65536):
                total_bytes += len(chunk)
                chunk_count += 1
                try:
                    buf.put_nowait(chunk)
                except asyncio.QueueFull:
                    logger.warning(
                        "fMP4 client too slow, buffer overflow — closing",
                        extra={"camera_id": camera_id, "stream_name": stream_name,
                               "total_bytes": total_bytes, "chunks": chunk_count},
                    )
                    return
        except httpx.RemoteProtocolError:
            logger.debug("fMP4 upstream closed by go2rtc", extra={
                "camera_id": camera_id, "total_bytes": total_bytes,
            })
        except Exception:
            logger.warning("fMP4 upstream read error", extra={
                "camera_id": camera_id, "total_bytes": total_bytes,
            }, exc_info=True)
        finally:
            try:
                buf.put_nowait(None)
            except asyncio.QueueFull:
                try:
                    buf.get_nowait()
                    buf.put_nowait(None)
                except (asyncio.QueueEmpty, asyncio.QueueFull):
                    pass

    try:
        t_connect = time.monotonic()
        resp = await http_client.send(
            http_client.build_request("GET", go2rtc_url),
            stream=True,
        )
        resp.raise_for_status()
        t_connected = time.monotonic()
        logger.info("fMP4 go2rtc connected", extra={
            "camera_id": camera_id, "stream_name": stream_name,
            "connect_ms": round((t_connected - t_connect) * 1000, 1),
            "http_status": resp.status_code,
        })

        # Start the reader task, then wait for first chunk via the queue
        reader_task = asyncio.create_task(upstream_reader(resp))
        first_chunk = await asyncio.wait_for(buf.get(), timeout=15.0)
        if first_chunk is None:
            raise asyncio.TimeoutError()

        first_chunk_ms = round((time.monotonic() - t_request) * 1000, 1)
        logger.info("fMP4 first chunk ready", extra={
            "camera_id": camera_id, "stream_name": stream_name,
            "quality": quality, "chunk_bytes": len(first_chunk),
            "time_to_first_chunk_ms": first_chunk_ms,
        })
    except asyncio.TimeoutError:
        logger.warning("fMP4 no data within 15s from go2rtc", extra={
            "camera_id": camera_id, "stream_name": stream_name,
        })
        if reader_task:
            reader_task.cancel()
        if resp:
            await resp.aclose()
        raise HTTPException(status_code=504, detail="Transcoder timeout")
    except httpx.ConnectError:
        logger.warning("fMP4 proxy: connection refused from go2rtc", extra={
            "camera_id": camera_id, "go2rtc_url": go2rtc_url,
        })
        raise HTTPException(status_code=502, detail="go2rtc unavailable")
    except httpx.HTTPStatusError as e:
        logger.warning("fMP4 proxy: HTTP error from go2rtc", extra={
            "camera_id": camera_id, "status": e.response.status_code,
        })
        if resp:
            await resp.aclose()
        raise HTTPException(status_code=502, detail="go2rtc error")
    except HTTPException:
        raise
    except Exception:
        logger.warning("fMP4 proxy: connect error", extra={
            "camera_id": camera_id,
        }, exc_info=True)
        if reader_task:
            reader_task.cancel()
        if resp:
            await resp.aclose()
        raise HTTPException(status_code=502, detail="Proxy error")

    # reader_task and resp are captured in the generator closure
    _reader_task = reader_task
    _resp = resp
    _first_chunk = first_chunk

    async def stream_generator():
        total_bytes_sent = 0
        chunk_count = 0
        try:
            # Yield pre-fetched first chunk immediately
            total_bytes_sent += len(_first_chunk)
            chunk_count = 1
            yield _first_chunk

            while True:
                chunk = await asyncio.wait_for(buf.get(), timeout=30.0)
                if chunk is None:
                    break
                chunk_count += 1
                total_bytes_sent += len(chunk)
                yield chunk
        except asyncio.TimeoutError:
            logger.warning("fMP4 no data for 30s — closing", extra={
                "camera_id": camera_id, "stream_name": stream_name,
                "total_bytes_sent": total_bytes_sent, "chunks": chunk_count,
            })
        except GeneratorExit:
            logger.debug("fMP4 client disconnected (GeneratorExit)", extra={
                "camera_id": camera_id, "stream_name": stream_name,
                "total_bytes_sent": total_bytes_sent, "chunks": chunk_count,
            })
        except Exception:
            logger.warning("fMP4 proxy: unexpected error", extra={
                "camera_id": camera_id,
            }, exc_info=True)
        finally:
            elapsed_s = round(time.monotonic() - t_request, 1)
            if _reader_task and not _reader_task.done():
                _reader_task.cancel()
            await _resp.aclose()
            logger.info("fMP4 session ended", extra={
                "camera_id": camera_id, "stream_name": stream_name,
                "quality": quality, "total_bytes_sent": total_bytes_sent,
                "chunks": chunk_count, "duration_s": elapsed_s,
            })

    return StreamingResponse(stream_generator(), media_type="video/mp4")


@router.get("/{camera_id}/rtsp-info")
async def rtsp_info(camera_id: int, stream: str = "s2", quality: str = "direct"):
    """Return the go2rtc RTSP URL for a camera stream."""
    mgr = get_stream_manager()
    info = mgr.streams.get(camera_id)
    if not info:
        raise HTTPException(status_code=404, detail="Stream not found")

    if stream not in ("s1", "s2"):
        stream = "s2"
    if quality not in ("direct", "high", "low", "ultralow"):
        quality = "direct"

    from app.services.go2rtc_manager import get_rtsp_port
    base_name = get_stream_name(info.camera_name)
    stream_name = f"{base_name}_{stream}_{quality}"

    if quality != "direct":
        go2rtc = get_go2rtc_client()
        if not go2rtc.has_stream(stream_name):
            quality = "direct"
            stream_name = f"{base_name}_{stream}_direct"

    return {
        "rtsp_url": f"rtsp://127.0.0.1:{get_rtsp_port()}/{stream_name}",
        "stream_name": stream_name,
    }


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

    if ws_quality != "direct":
        go2rtc = get_go2rtc_client()
        if not go2rtc.has_stream(stream_name):
            ws_quality = "direct"
            stream_name = f"{base_name}_{ws_stream}_direct"

    from app.services.go2rtc_manager import get_api_port as _get_api_port
    go2rtc_url = f"ws://127.0.0.1:{_get_api_port()}/api/ws?src={stream_name}"

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
