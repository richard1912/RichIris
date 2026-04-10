"""Camera CRUD API endpoints."""

import asyncio
import json
import logging
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_config
from app.database import get_db
from app.models import Camera, ClipExport, MotionEvent, Recording
from app.schemas import CameraCreate, CameraResponse, CameraUpdate
from app.services.ffmpeg import sanitize_camera_name
from app.services.motion_detector import get_motion_detector
from app.services.stream_manager import get_stream_manager

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/cameras", tags=["cameras"])


# --- RTSP URL patterns for popular camera brands ---
# Each entry: (brand, main_path, sub_path_or_None)
_RTSP_PATTERNS: list[tuple[str, str, str | None]] = [
    # ONVIF Profile S (very common)
    ("ONVIF", "/stream1", "/stream2"),
    ("ONVIF", "/MediaInput/stream_1", "/MediaInput/stream_2"),
    ("ONVIF", "/Streaming/Channels/101", "/Streaming/Channels/102"),
    # Hikvision
    ("Hikvision", "/Streaming/Channels/101", "/Streaming/Channels/102"),
    ("Hikvision", "/ISAPI/Streaming/Channels/101", "/ISAPI/Streaming/Channels/102"),
    ("Hikvision", "/h264/ch1/main/av_stream", "/h264/ch1/sub/av_stream"),
    # Dahua / Amcrest
    ("Dahua", "/cam/realmonitor?channel=1&subtype=0", "/cam/realmonitor?channel=1&subtype=1"),
    # Reolink
    ("Reolink", "/h264Preview_01_main", "/h264Preview_01_sub"),
    ("Reolink", "/Preview_01_main", "/Preview_01_sub"),
    # Tapo / TP-Link
    ("Tapo", "/stream1", "/stream2"),
    # Uniview
    ("Uniview", "/unicast/c1/s0/live", "/unicast/c1/s1/live"),
    # Axis
    ("Axis", "/axis-media/media.amp", None),
    # Hanwha (Samsung)
    ("Hanwha", "/profile2/media.smp", "/profile3/media.smp"),
    # Generic common paths
    ("Generic", "/live/ch00_0", "/live/ch00_1"),
    ("Generic", "/ch0_0.h264", "/ch0_1.h264"),
    ("Generic", "/live0", "/live1"),
    ("Generic", "/video1", "/video2"),
    ("Generic", "/1", "/2"),
    ("Generic", "/1/stream1", "/1/stream2"),
    # HTMS
    ("HTMS", "/Preview_01_main", "/Preview_01_sub"),
]


class RtspDiscoverRequest(BaseModel):
    ip: str
    username: str = ""
    password: str = ""
    port: int = 554


class RtspDiscoverResult(BaseModel):
    brand: str
    main_url: str
    sub_url: str | None = None
    codec: str | None = None
    resolution: str | None = None


async def _probe_rtsp_url(url: str, timeout: float = 5.0) -> dict | None:
    """Try to connect to an RTSP URL via ffprobe. Returns stream info or None."""
    config = get_config()
    cmd = [
        config.ffmpeg.ffprobe_path,
        "-v", "quiet",
        "-print_format", "json",
        "-show_streams",
        "-rtsp_transport", "tcp",
        "-timeout", str(int(timeout * 1_000_000)),  # microseconds
        url,
    ]
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout + 3)
        if proc.returncode != 0:
            return None
        data = json.loads(stdout.decode())
        streams = data.get("streams", [])
        for s in streams:
            if s.get("codec_type") == "video":
                return {
                    "codec": s.get("codec_name", "").lower(),
                    "width": s.get("width"),
                    "height": s.get("height"),
                }
        return None
    except Exception:
        return None


@router.post("/discover", response_model=list[RtspDiscoverResult])
async def discover_rtsp(req: RtspDiscoverRequest):
    """Probe common RTSP URL patterns on a camera IP to find working streams.

    Tests multiple brand-specific URL patterns concurrently and returns
    all working main+sub stream combinations.
    """
    ip = req.ip.strip()
    if not ip:
        raise HTTPException(status_code=400, detail="IP address required")

    creds = ""
    if req.username:
        creds = f"{req.username}:{req.password}@" if req.password else f"{req.username}@"

    logger.info("RTSP discovery starting", extra={"ip": ip, "patterns": len(_RTSP_PATTERNS)})

    # Build all URLs to probe
    probe_tasks: list[tuple[str, str, str, str | None]] = []  # (brand, main_url, main_path, sub_path)
    for brand, main_path, sub_path in _RTSP_PATTERNS:
        main_url = f"rtsp://{creds}{ip}:{req.port}{main_path}"
        probe_tasks.append((brand, main_url, main_path, sub_path))

    # Probe all main URLs concurrently (with concurrency limit to avoid overwhelming)
    sem = asyncio.Semaphore(8)

    async def _limited_probe(url: str) -> dict | None:
        async with sem:
            return await _probe_rtsp_url(url, timeout=5.0)

    main_results = await asyncio.gather(
        *[_limited_probe(t[1]) for t in probe_tasks]
    )

    # Collect working results
    results: list[RtspDiscoverResult] = []
    seen_main_paths: set[str] = set()

    for i, info in enumerate(main_results):
        if info is None:
            continue
        brand, main_url, main_path, sub_path = probe_tasks[i]
        # Deduplicate by main path (same URL might match multiple brands)
        if main_path in seen_main_paths:
            continue
        seen_main_paths.add(main_path)

        sub_url = None
        if sub_path:
            sub_url = f"rtsp://{creds}{ip}:{req.port}{sub_path}"

        resolution = None
        if info.get("width") and info.get("height"):
            resolution = f"{info['width']}x{info['height']}"

        results.append(RtspDiscoverResult(
            brand=brand,
            main_url=main_url,
            sub_url=sub_url,
            codec=info.get("codec"),
            resolution=resolution,
        ))

    logger.info("RTSP discovery complete", extra={
        "ip": ip, "found": len(results),
        "brands": [r.brand for r in results],
    })
    return results


@router.get("", response_model=list[CameraResponse])
async def list_cameras(db: AsyncSession = Depends(get_db)):
    """List all cameras."""
    result = await db.execute(select(Camera).order_by(Camera.id))
    cameras = result.scalars().all()
    logger.debug("Listed cameras", extra={"count": len(cameras)})
    return [CameraResponse.from_camera(c) for c in cameras]


@router.get("/{camera_id}", response_model=CameraResponse)
async def get_camera(camera_id: int, db: AsyncSession = Depends(get_db)):
    """Get a single camera by ID."""
    camera = await db.get(Camera, camera_id)
    if not camera:
        raise HTTPException(status_code=404, detail="Camera not found")
    return CameraResponse.from_camera(camera)


@router.post("", response_model=CameraResponse, status_code=201)
async def create_camera(data: CameraCreate, db: AsyncSession = Depends(get_db)):
    """Create a new camera."""
    scripts_json = None
    if data.motion_scripts:
        scripts_json = json.dumps([s.model_dump() for s in data.motion_scripts])
    camera = Camera(
        name=data.name, rtsp_url=data.rtsp_url,
        sub_stream_url=data.sub_stream_url or None,
        enabled=data.enabled, rotation=data.rotation,
        motion_sensitivity=data.motion_sensitivity,
        motion_script=data.motion_script,
        motion_script_off=data.motion_script_off,
        motion_scripts=scripts_json,
        ai_detection=data.ai_detection,
        ai_detect_persons=data.ai_detect_persons,
        ai_detect_vehicles=data.ai_detect_vehicles,
        ai_detect_animals=data.ai_detect_animals,
        ai_confidence_threshold=data.ai_confidence_threshold,
    )
    db.add(camera)
    await db.commit()
    await db.refresh(camera)
    logger.info("Camera created", extra={"camera_id": camera.id, "camera_name": camera.name})

    if camera.enabled:
        try:
            # Restart go2rtc so it picks up the new camera's streams
            from app.services.go2rtc_manager import restart_go2rtc
            await restart_go2rtc()
            mgr = get_stream_manager()
            await mgr.start_stream(camera.id, camera.name, camera.rtsp_url, camera.sub_stream_url)
            # Start the shared frame broker reader for the new camera first,
            # so motion + thumbnail capture have frames available immediately.
            from app.services.frame_broker import get_frame_broker
            await get_frame_broker().add_camera(camera)
            # Start thumbnail capture for the new camera
            from app.services.thumbnail_capture import get_thumbnail_capture
            get_thumbnail_capture().add_camera(camera)
            # Start motion detection for the new camera
            await get_motion_detector().update_camera(camera)
        except Exception:
            logger.exception("Failed to start stream for new camera", extra={"camera_id": camera.id})

    return CameraResponse.from_camera(camera)


@router.put("/{camera_id}", response_model=CameraResponse)
async def update_camera(
    camera_id: int, data: CameraUpdate, db: AsyncSession = Depends(get_db)
):
    """Update an existing camera."""
    camera = await db.get(Camera, camera_id)
    if not camera:
        raise HTTPException(status_code=404, detail="Camera not found")

    mgr = get_stream_manager()
    needs_restart = False

    old_name = camera.name

    if data.name is not None:
        camera.name = data.name
        needs_restart = True
    if data.rtsp_url is not None:
        camera.rtsp_url = data.rtsp_url
        needs_restart = True
    if "sub_stream_url" in (data.model_fields_set or set()):
        camera.sub_stream_url = data.sub_stream_url or None
        needs_restart = True
    if data.enabled is not None:
        camera.enabled = data.enabled
    if data.rotation is not None:
        camera.rotation = data.rotation
    if data.motion_sensitivity is not None:
        camera.motion_sensitivity = data.motion_sensitivity
    if "motion_script" in (data.model_fields_set or set()):
        camera.motion_script = data.motion_script
    if "motion_script_off" in (data.model_fields_set or set()):
        camera.motion_script_off = data.motion_script_off
    if data.ai_detection is not None:
        camera.ai_detection = data.ai_detection
    if data.ai_detect_persons is not None:
        camera.ai_detect_persons = data.ai_detect_persons
    if data.ai_detect_vehicles is not None:
        camera.ai_detect_vehicles = data.ai_detect_vehicles
    if data.ai_detect_animals is not None:
        camera.ai_detect_animals = data.ai_detect_animals
    if data.ai_confidence_threshold is not None:
        camera.ai_confidence_threshold = data.ai_confidence_threshold
    if "motion_scripts" in (data.model_fields_set or set()):
        if data.motion_scripts is not None:
            camera.motion_scripts = json.dumps([s.model_dump() for s in data.motion_scripts])
        else:
            camera.motion_scripts = None

    await db.commit()
    await db.refresh(camera)
    logger.info("Camera updated", extra={"camera_id": camera.id})

    # Rename recording folder and update DB paths if name changed
    if data.name is not None and data.name != old_name:
        await _rename_camera_folder(db, camera.id, old_name, data.name)

    from app.services.frame_broker import get_frame_broker
    broker = get_frame_broker()
    if not camera.enabled:
        await mgr.stop_stream(camera.id)
        await broker.remove_camera(camera.id)
    elif needs_restart:
        await mgr.stop_stream(camera.id)
        await broker.remove_camera(camera.id)
        await mgr.start_stream(camera.id, camera.name, camera.rtsp_url, camera.sub_stream_url)
        await broker.add_camera(camera)

    # Update motion detection if settings changed
    detector = get_motion_detector()
    await detector.update_camera(camera)

    return CameraResponse.from_camera(camera)


@router.delete("/{camera_id}", status_code=204)
async def delete_camera(camera_id: int, db: AsyncSession = Depends(get_db)):
    """Delete a camera and stop its stream. Video files on disk are preserved."""
    camera = await db.get(Camera, camera_id)
    if not camera:
        raise HTTPException(status_code=404, detail="Camera not found")

    mgr = get_stream_manager()
    await mgr.stop_stream(camera.id)
    from app.services.frame_broker import get_frame_broker
    await get_frame_broker().remove_camera(camera.id)

    # Remove DB metadata (recordings + clip exports) so FK constraints don't block delete.
    # Actual video files on disk are NOT deleted.
    for model in (ClipExport, MotionEvent, Recording):
        result = await db.execute(select(model).where(model.camera_id == camera_id))
        for row in result.scalars().all():
            await db.delete(row)

    await db.delete(camera)
    await db.commit()
    logger.info("Camera deleted", extra={"camera_id": camera_id})

    # Restart go2rtc to remove deleted camera's streams
    try:
        from app.services.go2rtc_manager import restart_go2rtc
        await restart_go2rtc()
    except Exception:
        logger.exception("Failed to restart go2rtc after camera delete")



async def _rename_camera_folder(
    db: AsyncSession, camera_id: int, old_name: str, new_name: str
) -> None:
    """Rename the recording folder on disk and update all DB file paths."""
    config = get_config()
    rec_root = Path(config.storage.recordings_dir)
    old_safe = sanitize_camera_name(old_name)
    new_safe = sanitize_camera_name(new_name)

    old_dir = rec_root / old_safe
    new_dir = rec_root / new_safe

    if old_dir.exists() and not new_dir.exists():
        old_dir.rename(new_dir)
        logger.info("Renamed camera folder", extra={"old": str(old_dir), "new": str(new_dir)})

        # Update all recording paths in DB
        result = await db.execute(
            select(Recording).where(Recording.camera_id == camera_id)
        )
        for rec in result.scalars().all():
            if rec.file_path:
                rec.file_path = rec.file_path.replace(
                    str(old_dir), str(new_dir)
                )
        await db.commit()
        logger.info("Updated recording paths", extra={"camera_id": camera_id})
