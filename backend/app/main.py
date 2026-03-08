"""FastAPI application with lifespan management."""

import asyncio
import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from starlette.responses import FileResponse
from sqlalchemy import select

from app.config import get_config
from app.database import close_db, get_db, get_session_factory, init_db
from app.logging_config import setup_logging
from app.models import Camera
from app.routers import cameras, clips, recordings, streams, system
from app.services.recorder import cleanup_missing_recordings, scan_all_cameras
from app.services.retention import enforce_retention
from app.services.playback import get_playback_manager
from app.services.stream_manager import get_stream_manager

FRONTEND_DIR = Path(__file__).resolve().parent.parent.parent / "frontend" / "dist"

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application startup and shutdown lifecycle."""
    config = get_config()
    setup_logging(level=config.logging.level, json_output=config.logging.json_output)
    logger.info("RichIris NVR starting up")

    await init_db()

    # Clean up DB records for manually deleted files
    factory = get_session_factory()
    async with factory() as session:
        await cleanup_missing_recordings(session)

    await _start_enabled_cameras()

    scan_task = asyncio.create_task(_periodic_scan())
    retention_task = asyncio.create_task(_periodic_retention())
    yield
    scan_task.cancel()
    retention_task.cancel()

    logger.info("RichIris NVR shutting down")
    mgr = get_stream_manager()
    await mgr.stop_all()
    pb = get_playback_manager()
    await pb.stop_all()
    await close_db()


async def _periodic_scan() -> None:
    """Periodically scan for new recording segments and register them."""
    await asyncio.sleep(30)  # Initial delay to let streams start
    while True:
        try:
            factory = get_session_factory()
            async with factory() as session:
                count = await scan_all_cameras(session)
                if count > 0:
                    logger.info("Periodic scan registered segments", extra={"count": count})
        except Exception:
            logger.exception("Periodic segment scan failed")
        await asyncio.sleep(60)


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


async def _start_enabled_cameras() -> None:
    """Start streams for all enabled cameras in the database."""
    factory = get_session_factory()
    async with factory() as session:
        result = await session.execute(
            select(Camera).where(Camera.enabled == True)
        )
        cameras_list = result.scalars().all()

    mgr = get_stream_manager()
    for cam in cameras_list:
        logger.info("Auto-starting camera stream", extra={"camera_id": cam.id, "camera_name": cam.name})
        await mgr.start_stream(cam.id, cam.name, cam.rtsp_url)

    logger.info("Started enabled cameras", extra={"count": len(cameras_list)})


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
            return FileResponse(FRONTEND_DIR / "index.html")

    return app


app = create_app()
