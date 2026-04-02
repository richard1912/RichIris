"""FastAPI application with lifespan management."""

import asyncio
import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
import httpx
from fastapi.staticfiles import StaticFiles
from starlette.responses import FileResponse
from sqlalchemy import select

from app.config import get_config
from app.database import close_db, get_db, get_session_factory, init_db
from app.logging_config import setup_logging
from app.models import Camera
from app.routers import cameras, clips, motion, recordings, streams, system
from app.services.job_object import create_job_object
from app.services.recorder import cleanup_missing_recordings, scan_all_cameras
from app.services.retention import enforce_retention
from app.services.playback import get_playback_manager
from app.services.stream_manager import get_stream_manager
from app.services.thumbnail_capture import get_thumbnail_capture
from app.services.motion_detector import get_motion_detector
from app.services.object_detector import get_object_detector

FRONTEND_DIR = Path(__file__).resolve().parent.parent.parent / "frontend" / "dist"

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application startup and shutdown lifecycle."""
    config = get_config()
    setup_logging(level=config.logging.level, json_output=config.logging.json_output, timezone=config.logging.timezone)
    logger.info("RichIris NVR starting up")

    create_job_object()
    _kill_orphaned_ffmpeg()

    await init_db()

    # Clean up DB records for manually deleted files
    factory = get_session_factory()
    async with factory() as session:
        await cleanup_missing_recordings(session)

    cameras_list = await _start_enabled_cameras()

    # Verify go2rtc is reachable (live view depends on it, recording does not)
    await _check_go2rtc_health()

    # Pre-warm go2rtc streams so they're ready before clients connect
    asyncio.create_task(_prewarm_streams(cameras_list))

    thumb_capture = get_thumbnail_capture()
    thumb_capture.start(cameras_list)

    # Start AI object detector if any camera has AI detection enabled
    obj_detector = get_object_detector()
    if any(getattr(cam, 'ai_detection', False) for cam in cameras_list):
        await obj_detector.start()

    motion_detector = get_motion_detector()
    motion_detector.start(cameras_list)

    scan_task = asyncio.create_task(_periodic_scan())
    retention_task = asyncio.create_task(_periodic_retention())
    yield
    scan_task.cancel()
    retention_task.cancel()

    logger.info("RichIris NVR shutting down")
    await motion_detector.stop()
    await obj_detector.stop()
    mgr = get_stream_manager()
    await mgr.stop_all()
    pb = get_playback_manager()
    await pb.stop_all()
    await thumb_capture.stop()
    from app.routers.streams import close_pool
    await close_pool()
    await close_db()


async def _periodic_scan() -> None:
    """Periodically scan for new recording segments and register them."""
    await asyncio.sleep(15)  # Initial delay to let streams start
    while True:
        try:
            factory = get_session_factory()
            async with factory() as session:
                count = await scan_all_cameras(session)
                if count > 0:
                    logger.info("Periodic scan registered segments", extra={"count": count})
        except Exception:
            logger.exception("Periodic segment scan failed")
        await asyncio.sleep(15)


async def _periodic_retention() -> None:
    """Periodically enforce retention policies (every 6 hours)."""
    await asyncio.sleep(120)  # Initial delay
    while True:
        try:
            factory = get_session_factory()
            async with factory() as session:
                result = await enforce_retention(session)
                if result["deleted"] > 0:
                    logger.info("Retention cycle complete", extra=result)
        except Exception:
            logger.exception("Retention cycle failed")
        await asyncio.sleep(6 * 3600)  # Every 6 hours


def _kill_orphaned_ffmpeg() -> None:
    """Kill any ffmpeg processes left over from a previous run."""
    import os
    if os.name != "nt":
        return
    try:
        import subprocess
        result = subprocess.run(
            ["tasklist", "/fi", "imagename eq ffmpeg.exe", "/fo", "csv", "/nh"],
            capture_output=True, text=True, timeout=10,
        )
        pids = []
        for line in result.stdout.strip().splitlines():
            parts = line.strip().strip('"').split('","')
            if len(parts) >= 2:
                try:
                    pids.append(int(parts[1]))
                except ValueError:
                    pass
        if not pids:
            return
        logger.warning("Killing orphaned ffmpeg processes from previous run", extra={"pids": pids, "count": len(pids)})
        subprocess.run(
            ["taskkill", "/F"] + [arg for pid in pids for arg in ("/PID", str(pid))],
            capture_output=True, timeout=10,
        )
    except Exception:
        logger.exception("Failed to clean up orphaned ffmpeg processes")


async def _check_go2rtc_health() -> None:
    """Retry loop to verify go2rtc is responding. Logs warning if unavailable."""
    from app.services.go2rtc_client import get_go2rtc_client

    client = get_go2rtc_client()
    for attempt in range(5):
        if await client.is_healthy():
            logger.info("go2rtc health check passed")
            return
        logger.warning("go2rtc not responding, retrying", extra={"attempt": attempt + 1})
        await asyncio.sleep(2)
    logger.error("go2rtc unavailable — live view will not work, recording continues")


async def _start_enabled_cameras() -> list:
    """Start streams for all enabled cameras in the database. Returns camera list."""
    factory = get_session_factory()
    async with factory() as session:
        result = await session.execute(
            select(Camera).where(Camera.enabled == True)
        )
        cameras_list = result.scalars().all()

    mgr = get_stream_manager()
    await asyncio.gather(*[
        mgr.start_stream(cam.id, cam.name, cam.rtsp_url, cam.sub_stream_url)
        for cam in cameras_list
    ])

    logger.info("Started enabled cameras", extra={"count": len(cameras_list)})
    return cameras_list


async def _prewarm_streams(cameras_list: list) -> None:
    """Trigger go2rtc lazy RTSP connections so streams are ready for clients."""
    from app.services.go2rtc_client import get_stream_name

    config = get_config()

    async def warm_one(cam) -> None:
        stream_name = get_stream_name(cam.name)
        # Warm the s2_direct stream (most commonly used, fastest path)
        url = f"http://127.0.0.1:{config.go2rtc.port}/api/stream.mp4?src={stream_name}_s2_direct"
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(10.0)) as client:
                async with client.stream("GET", url) as resp:
                    total = 0
                    async for chunk in resp.aiter_bytes(chunk_size=8192):
                        total += len(chunk)
                        if total > 32768:
                            break
            logger.info("Pre-warmed stream", extra={"camera": cam.name})
        except Exception:
            logger.warning("Failed to pre-warm stream", extra={"camera": cam.name})

    await asyncio.gather(*[warm_one(cam) for cam in cameras_list])


def create_app() -> FastAPI:
    """Create and configure the FastAPI application."""
    app = FastAPI(
        title="RichIris NVR",
        version="0.1.0",
        lifespan=lifespan,
    )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=["http://localhost:5173"],
        allow_methods=["*"],
        allow_headers=["*"],
    )

    app.include_router(cameras.router)
    app.include_router(clips.router)
    app.include_router(motion.router)
    app.include_router(recordings.router)
    app.include_router(streams.router)
    app.include_router(system.router)

    @app.get("/api/health")
    async def health():
        return {"status": "ok"}

    # Serve frontend static build (must be after API routes)
    if FRONTEND_DIR.is_dir():
        app.mount("/assets", StaticFiles(directory=FRONTEND_DIR / "assets"), name="static")

        @app.get("/{full_path:path}")
        async def serve_frontend(full_path: str):
            file = FRONTEND_DIR / full_path
            if file.is_file():
                return FileResponse(file)
            return FileResponse(
                FRONTEND_DIR / "index.html",
                headers={"Cache-Control": "no-cache, no-store, must-revalidate"},
            )

    return app


app = create_app()
