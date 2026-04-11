"""FastAPI application with lifespan management."""

import asyncio
import logging
import time
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from sqlalchemy import select

from app.config import get_app_dir, get_config
from app.database import close_db, get_db, get_session_factory, init_db
from app.logging_config import setup_logging
from app.models import Camera
from app.routers import backup, cameras, clips, motion, recordings, settings, storage, streams, system
from app.services.job_object import create_job_object
from app.services.recorder import cleanup_missing_recordings, scan_all_cameras
from app.services.retention import enforce_retention
from app.services.playback import get_playback_manager
from app.services.frame_broker import get_frame_broker
from app.services.stream_manager import get_stream_manager
from app.services.thumbnail_capture import get_thumbnail_capture
from app.services.motion_detector import get_motion_detector
from app.services.object_detector import get_object_detector

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

    # Migrate legacy config.yaml settings into DB (one-time, idempotent)
    from app.config import migrate_legacy_config, load_settings_from_db
    factory = get_session_factory()
    async with factory() as session:
        await migrate_legacy_config(session)

    # Populate config singleton with DB settings (overrides bootstrap defaults)
    async with factory() as session:
        await load_settings_from_db(session)

    # Re-apply logging config now that DB settings are loaded
    config = get_config()
    setup_logging(level=config.logging.level, json_output=config.logging.json_output, timezone=config.logging.timezone)

    # Clean up DB records for manually deleted files
    async with factory() as session:
        await cleanup_missing_recordings(session)

    # Load enabled cameras for go2rtc config and stream startup
    t0 = time.monotonic()
    cameras_list = await _load_enabled_cameras()
    logger.info("Startup: cameras loaded", extra={
        "count": len(cameras_list), "ms": round((time.monotonic() - t0) * 1000, 1),
    })

    # Build go2rtc streams config
    from app.services.go2rtc_client import build_streams_config, get_go2rtc_client
    t0 = time.monotonic()
    streams_config = build_streams_config([
        (cam.name, cam.rtsp_url, cam.sub_stream_url) for cam in cameras_list
    ])
    logger.info("Startup: streams config built", extra={
        "stream_count": len(streams_config), "ms": round((time.monotonic() - t0) * 1000, 1),
    })

    from app.services.go2rtc_manager import start_go2rtc, stop_go2rtc
    t0 = time.monotonic()
    await start_go2rtc(streams=streams_config)
    logger.info("Startup: go2rtc started", extra={
        "ms": round((time.monotonic() - t0) * 1000, 1),
    })

    # Verify go2rtc is reachable (live view depends on it, recording does not)
    t0 = time.monotonic()
    await _check_go2rtc_health()
    logger.info("Startup: go2rtc health verified", extra={
        "ms": round((time.monotonic() - t0) * 1000, 1),
    })

    # Store streams config for on-demand registration (streams already in go2rtc.yaml)
    client = get_go2rtc_client()
    client.store_streams_config(streams_config)

    # Start recording ffmpeg processes
    t0 = time.monotonic()
    await _start_camera_recordings(cameras_list)
    logger.info("Startup: recordings started", extra={
        "ms": round((time.monotonic() - t0) * 1000, 1),
    })

    # Shared frame broker: one persistent ffmpeg per camera pulling MJPEG
    # frames from the s2_direct relay. Keeps the sub-stream warm AND feeds
    # both motion detection and thumbnail capture — so it must start first.
    frame_broker = get_frame_broker()
    await frame_broker.start(cameras_list)

    thumb_capture = get_thumbnail_capture()
    thumb_capture.start(cameras_list)

    # Start AI object detector if any camera has AI detection enabled
    obj_detector = get_object_detector()
    if any(getattr(cam, 'ai_detection', False) for cam in cameras_list):
        await obj_detector.start()

    motion_detector = get_motion_detector()
    await motion_detector.start(cameras_list)

    from app.services.update_checker import get_update_checker
    update_checker = get_update_checker()
    app_exe = Path(get_app_dir()) / "app" / "richiris.exe"
    await update_checker.start(current_version=app.version, app_exe=app_exe)

    scan_task = asyncio.create_task(_periodic_scan())
    retention_task = asyncio.create_task(_periodic_retention())
    yield
    scan_task.cancel()
    retention_task.cancel()

    logger.info("RichIris NVR shutting down")
    await update_checker.stop()
    await motion_detector.stop()
    await obj_detector.stop()
    await thumb_capture.stop()
    await frame_broker.stop()
    mgr = get_stream_manager()
    await mgr.stop_all()
    pb = get_playback_manager()
    await pb.stop_all()
    from app.routers.streams import close_pool
    await close_pool()
    await stop_go2rtc()
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


async def _load_enabled_cameras() -> list:
    """Load all enabled cameras from the database."""
    factory = get_session_factory()
    async with factory() as session:
        result = await session.execute(
            select(Camera).where(Camera.enabled == True)
        )
        return result.scalars().all()


async def _start_camera_recordings(cameras_list: list) -> None:
    """Start recording ffmpeg processes for all cameras (parallel)."""
    mgr = get_stream_manager()
    await asyncio.gather(*[
        mgr.start_stream(cam.id, cam.name, cam.rtsp_url, cam.sub_stream_url)
        for cam in cameras_list
    ])

    logger.info("Started enabled cameras", extra={"count": len(cameras_list)})



def create_app() -> FastAPI:
    """Create and configure the FastAPI application."""
    app = FastAPI(
        title="RichIris NVR",
        version="0.0.7",
        lifespan=lifespan,
    )

    app.include_router(backup.router)
    app.include_router(cameras.router)
    app.include_router(clips.router)
    app.include_router(motion.router)
    app.include_router(recordings.router)
    app.include_router(settings.router)
    app.include_router(storage.router)
    app.include_router(streams.router)
    app.include_router(system.router)

    @app.get("/api/health")
    async def health():
        return {"status": "ok", "app": "richiris", "version": app.version}

    return app


app = create_app()
