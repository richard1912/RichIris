"""Camera CRUD API endpoints."""

import asyncio
import json
import logging
import socket
import time
from pathlib import Path
from urllib.parse import urlparse

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from pydantic import BaseModel
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_config
from app.database import get_db
from app.models import Camera, ClipExport, MotionEvent, Recording, Zone
from app.schemas import CameraCreate, CameraResponse, CameraUpdate, TestScriptRequest, TestScriptResponse
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


# --- LAN camera scan ---------------------------------------------------------
# Scans private /24 subnets for hosts with port 554 open, sends an RTSP OPTIONS
# request, and parses the Server header for a brand hint. Zero-dep, cross-platform.

_BRAND_HINTS: list[tuple[str, str]] = [
    # (substring to match in Server header lowercase, normalized brand)
    ("hikvision", "Hikvision"),
    ("dahua", "Dahua"),
    ("reolink", "Reolink"),
    ("axis", "Axis"),
    ("hipcam", "Generic"),
    ("gstreamer", "Generic"),
    ("live555", "Generic"),
    ("onvif", "ONVIF"),
    ("htms", "HTMS"),
    ("tapo", "Tapo"),
    ("tp-link", "Tapo"),
    ("hanwha", "Hanwha"),
    ("samsung", "Hanwha"),
    ("uniview", "Uniview"),
]


class CameraScanRequest(BaseModel):
    subnets: list[str] | None = None   # e.g. ["192.168.8"]; default = auto-detect
    port: int = 554
    timeout_ms: int = 300
    concurrency: int = 64


class CameraScanHit(BaseModel):
    ip: str
    port: int
    server_header: str | None = None
    brand_hint: str | None = None


class CameraScanResponse(BaseModel):
    subnets_scanned: list[str]
    hosts_probed: int
    hits: list[CameraScanHit]
    elapsed_ms: int


def _detect_private_subnets() -> list[tuple[str, int]]:
    """Return list of (prefix, self_host) tuples for each unique private /24
    reachable via a local IPv4 interface. `prefix` is e.g. "192.168.8" and
    `self_host` is the server's own octet on that subnet (used to skip probing
    ourselves). Zero-dep — uses socket.getaddrinfo on the hostname.
    """
    seen: dict[str, int] = {}
    try:
        infos = socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET)
    except socket.gaierror:
        infos = []
    for family, _type, _proto, _canon, sockaddr in infos:
        if family != socket.AF_INET:
            continue
        ip = sockaddr[0]
        parts = ip.split(".")
        if len(parts) != 4:
            continue
        try:
            a, b, _c, d = (int(p) for p in parts)
        except ValueError:
            continue
        # RFC1918 filter
        if not (
            a == 10
            or (a == 172 and 16 <= b <= 31)
            or (a == 192 and b == 168)
        ):
            continue
        prefix = f"{parts[0]}.{parts[1]}.{parts[2]}"
        seen.setdefault(prefix, d)
    return [(prefix, host) for prefix, host in seen.items()]


def _classify_server_header(header: str | None) -> str | None:
    if not header:
        return None
    low = header.lower()
    for needle, brand in _BRAND_HINTS:
        if needle in low:
            return brand
    return None


async def _probe_host(ip: str, port: int, connect_timeout: float) -> CameraScanHit | None:
    """TCP-connect to ip:port. If open, send RTSP OPTIONS and parse Server header.
    Returns a CameraScanHit if the port is reachable, None otherwise.

    The TCP connect uses `connect_timeout` (short — this dominates scan latency).
    The OPTIONS reply gets a longer read budget so slower cameras still hint a brand.
    """
    try:
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(ip, port), timeout=connect_timeout
        )
    except (asyncio.TimeoutError, OSError):
        return None

    read_timeout = max(1.0, connect_timeout * 4)
    server_header: str | None = None
    try:
        request = (
            f"OPTIONS rtsp://{ip}:{port}/ RTSP/1.0\r\n"
            f"CSeq: 1\r\n"
            f"User-Agent: RichIris-Scan/1.0\r\n"
            f"\r\n"
        )
        writer.write(request.encode("ascii"))
        await writer.drain()
        try:
            data = await asyncio.wait_for(reader.read(2048), timeout=read_timeout)
        except asyncio.TimeoutError:
            data = b""
        if data:
            try:
                text = data.decode("ascii", errors="replace")
                for line in text.splitlines():
                    if line.lower().startswith("server:"):
                        server_header = line.split(":", 1)[1].strip()
                        break
            except Exception:
                pass
    finally:
        try:
            writer.close()
            await writer.wait_closed()
        except Exception:
            pass

    return CameraScanHit(
        ip=ip,
        port=port,
        server_header=server_header,
        brand_hint=_classify_server_header(server_header),
    )


def _hosts_from_existing_cameras(cameras: list[Camera]) -> set[str]:
    """Extract host strings from existing camera rtsp_url values so we can
    exclude them from scan results.
    """
    hosts: set[str] = set()
    for cam in cameras:
        for url in (cam.rtsp_url, cam.sub_stream_url):
            if not url:
                continue
            try:
                parsed = urlparse(url)
                if parsed.hostname:
                    hosts.add(parsed.hostname)
            except Exception:
                continue
    return hosts


@router.post("/scan", response_model=CameraScanResponse)
async def scan_cameras(
    req: CameraScanRequest, db: AsyncSession = Depends(get_db)
):
    """LAN-scan for IP cameras.

    Enumerates private /24 subnets (auto-detected or provided), TCP-probes
    `port` on each host, and for reachable hosts issues an RTSP OPTIONS to
    sniff the Server header for a brand hint. Excludes IPs that are already
    attached to an existing camera.
    """
    t0 = time.monotonic()

    # Determine subnets + self hosts
    if req.subnets:
        subnets: list[tuple[str, int]] = [(s.strip().rstrip("."), -1) for s in req.subnets if s.strip()]
    else:
        subnets = _detect_private_subnets()

    # Exclude already-added camera hosts
    result = await db.execute(select(Camera))
    existing_hosts = _hosts_from_existing_cameras(list(result.scalars().all()))

    # Build candidate list
    candidates: list[str] = []
    for prefix, self_host in subnets:
        for host in range(1, 255):
            if host == self_host:
                continue
            ip = f"{prefix}.{host}"
            if ip in existing_hosts:
                continue
            candidates.append(ip)

    logger.info(
        "Camera LAN scan starting",
        extra={
            "subnets": [s[0] for s in subnets],
            "candidates": len(candidates),
            "port": req.port,
            "timeout_ms": req.timeout_ms,
            "concurrency": req.concurrency,
        },
    )

    timeout = max(0.05, req.timeout_ms / 1000.0)
    sem = asyncio.Semaphore(max(1, req.concurrency))

    async def _limited(ip: str) -> CameraScanHit | None:
        async with sem:
            return await _probe_host(ip, req.port, timeout)

    results = await asyncio.gather(*[_limited(ip) for ip in candidates])
    hits = [r for r in results if r is not None]

    elapsed_ms = int((time.monotonic() - t0) * 1000)
    logger.info(
        "Camera LAN scan complete",
        extra={
            "subnets": [s[0] for s in subnets],
            "hosts_probed": len(candidates),
            "hits": len(hits),
            "elapsed_ms": elapsed_ms,
        },
    )

    return CameraScanResponse(
        subnets_scanned=[s[0] for s in subnets],
        hosts_probed=len(candidates),
        hits=hits,
        elapsed_ms=elapsed_ms,
    )


# --- Batch RTSP discovery ---------------------------------------------------

class DiscoverBatchRequest(BaseModel):
    targets: list[RtspDiscoverRequest]
    host_concurrency: int = 4   # how many hosts to probe in parallel


class DiscoverBatchResponse(BaseModel):
    results: dict[str, list[RtspDiscoverResult]]


@router.post("/discover_batch", response_model=DiscoverBatchResponse)
async def discover_rtsp_batch(req: DiscoverBatchRequest):
    """Run RTSP URL discovery for multiple hosts in parallel.

    Reuses the same pattern list + ffprobe helper as `/discover` but fans out
    across multiple hosts. Bounded host-level concurrency avoids spawning
    hundreds of concurrent ffprobe processes (each host already runs up to 8
    patterns in parallel internally, so 4 hosts × 8 = 32 ffprobes max).
    """
    if not req.targets:
        return DiscoverBatchResponse(results={})

    logger.info(
        "Batch RTSP discovery starting",
        extra={"targets": len(req.targets), "host_concurrency": req.host_concurrency},
    )

    host_sem = asyncio.Semaphore(max(1, req.host_concurrency))

    async def _one(target: RtspDiscoverRequest) -> tuple[str, list[RtspDiscoverResult]]:
        async with host_sem:
            try:
                found = await discover_rtsp(target)
            except HTTPException:
                found = []
            except Exception:
                logger.exception("Discover failed for host", extra={"ip": target.ip})
                found = []
            return target.ip, found

    pairs = await asyncio.gather(*[_one(t) for t in req.targets])

    results: dict[str, list[RtspDiscoverResult]] = {}
    for ip, found in pairs:
        # If the same IP appears twice in the request, merge (last wins).
        results[ip] = found

    logger.info(
        "Batch RTSP discovery complete",
        extra={"targets": len(req.targets), "hosts_with_hits": sum(1 for v in results.values() if v)},
    )
    return DiscoverBatchResponse(results=results)


# --- One-shot snapshot ------------------------------------------------------
# Used by the Scan & Add wizard to show a preview frame next to each camera
# name field, so users can identify cameras visually before choosing a name.

class SnapshotRequest(BaseModel):
    rtsp_url: str
    width: int = 320   # downscale for quick transfer; 320 keeps it small
    timeout_s: float = 8.0


@router.post("/snapshot")
async def camera_snapshot(req: SnapshotRequest):
    """Grab a single JPEG frame from an arbitrary RTSP URL.

    Used by the Scan & Add wizard's preview step. Not tied to a stored
    camera — the caller passes the full RTSP URL (credentials embedded) so
    we can snapshot cameras before they're added to the DB.

    Returns `image/jpeg` bytes on success, 504 on timeout, 502 on ffmpeg
    failure. Downscaled to `width` for fast transfer.
    """
    url = req.rtsp_url.strip()
    if not url.lower().startswith("rtsp://"):
        raise HTTPException(status_code=400, detail="rtsp_url must be an rtsp:// URL")

    config = get_config()
    width = max(64, min(req.width, 1280))
    timeout = max(2.0, min(req.timeout_s, 20.0))

    cmd = [
        config.ffmpeg.path,
        "-nostdin",
        "-loglevel", "error",
        "-rtsp_transport", "tcp",
        "-timeout", str(int(timeout * 1_000_000)),  # microseconds (socket I/O)
        "-i", url,
        "-vf", f"scale={width}:-2",
        "-frames:v", "1",
        "-f", "image2",
        "-vcodec", "mjpeg",
        "-q:v", "5",
        "-",
    ]

    logger.info("Camera snapshot starting", extra={"url_host": urlparse(url).hostname, "width": width})
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout + 3)
        except asyncio.TimeoutError:
            try:
                proc.kill()
            except Exception:
                pass
            logger.warning("Camera snapshot timed out", extra={"url_host": urlparse(url).hostname})
            raise HTTPException(status_code=504, detail="Snapshot timed out")
    except FileNotFoundError:
        raise HTTPException(status_code=500, detail="ffmpeg not available")

    if proc.returncode != 0 or not stdout:
        err = (stderr or b"").decode("utf-8", errors="replace")[:500]
        logger.warning(
            "Camera snapshot failed",
            extra={"url_host": urlparse(url).hostname, "returncode": proc.returncode, "stderr": err},
        )
        raise HTTPException(status_code=502, detail=f"Snapshot failed: {err.strip() or 'no output'}")

    logger.info("Camera snapshot captured", extra={"url_host": urlparse(url).hostname, "bytes": len(stdout)})
    return Response(content=stdout, media_type="image/jpeg")


class CameraReorderRequest(BaseModel):
    order: list[int]  # list of camera IDs in desired order


@router.put("/reorder")
async def reorder_cameras(data: CameraReorderRequest, db: AsyncSession = Depends(get_db)):
    """Update sort_order for all cameras based on provided ID list."""
    for idx, camera_id in enumerate(data.order):
        await db.execute(
            update(Camera).where(Camera.id == camera_id).values(sort_order=idx)
        )
    await db.commit()
    logger.info("Cameras reordered", extra={"count": len(data.order)})
    return {"ok": True}


@router.get("", response_model=list[CameraResponse])
async def list_cameras(db: AsyncSession = Depends(get_db)):
    """List all cameras."""
    result = await db.execute(select(Camera).order_by(Camera.sort_order, Camera.id))
    cameras = result.scalars().all()
    # Zone counts in one aggregate query keyed by camera_id
    zc_rows = (await db.execute(
        select(Zone.camera_id, func.count(Zone.id)).group_by(Zone.camera_id)
    )).all()
    zone_counts = {cid: cnt for cid, cnt in zc_rows}
    logger.debug("Listed cameras", extra={"count": len(cameras)})
    return [CameraResponse.from_camera(c, zone_count=zone_counts.get(c.id, 0)) for c in cameras]


@router.get("/{camera_id}", response_model=CameraResponse)
async def get_camera(camera_id: int, db: AsyncSession = Depends(get_db)):
    """Get a single camera by ID."""
    camera = await db.get(Camera, camera_id)
    if not camera:
        raise HTTPException(status_code=404, detail="Camera not found")
    zc = (await db.execute(
        select(func.count(Zone.id)).where(Zone.camera_id == camera_id)
    )).scalar_one()
    return CameraResponse.from_camera(camera, zone_count=zc)


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
        sort_order=data.sort_order, group_id=data.group_id,
        motion_sensitivity=data.motion_sensitivity,
        motion_script=data.motion_script,
        motion_script_off=data.motion_script_off,
        motion_scripts=scripts_json,
        ai_detection=data.ai_detection,
        ai_detect_persons=data.ai_detect_persons,
        ai_detect_vehicles=data.ai_detect_vehicles,
        ai_detect_animals=data.ai_detect_animals,
        ai_confidence_threshold=data.ai_confidence_threshold,
        face_recognition=data.face_recognition,
        face_match_threshold=data.face_match_threshold,
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

    return CameraResponse.from_camera(camera, zone_count=0)


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
    if data.sort_order is not None:
        camera.sort_order = data.sort_order
    if "group_id" in (data.model_fields_set or set()):
        camera.group_id = data.group_id
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
    if data.face_recognition is not None:
        camera.face_recognition = data.face_recognition
    if data.face_match_threshold is not None:
        camera.face_match_threshold = data.face_match_threshold
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
    from app.services.go2rtc_client import get_go2rtc_client
    broker = get_frame_broker()
    if not camera.enabled:
        await mgr.stop_stream(camera.id)
        await broker.remove_camera(camera.id)
    elif needs_restart:
        await mgr.stop_stream(camera.id)
        await broker.remove_camera(camera.id)
        # Re-register the camera's streams with go2rtc under the (possibly
        # new) name. The startup config in go2rtc.yaml is baked at launch,
        # so a rename leaves go2rtc without the new stream key → keepalive
        # 404s. Registering via HTTP adds the new variants in-memory;
        # harmless if names happen to match (PUT is idempotent).
        try:
            await get_go2rtc_client().register_stream(
                camera.name, camera.rtsp_url, camera.sub_stream_url,
            )
        except Exception:
            logger.exception("Failed to re-register go2rtc streams after update",
                             extra={"camera_id": camera.id, "camera_name": camera.name})
        await mgr.start_stream(camera.id, camera.name, camera.rtsp_url, camera.sub_stream_url)
        await broker.add_camera(camera)

    # Update motion detection if settings changed
    detector = get_motion_detector()
    await detector.update_camera(camera)

    zc = (await db.execute(
        select(func.count(Zone.id)).where(Zone.camera_id == camera.id)
    )).scalar_one()
    return CameraResponse.from_camera(camera, zone_count=zc)


@router.post("/test-script", response_model=TestScriptResponse)
async def test_script(req: TestScriptRequest):
    """Run a script command and return its output for testing purposes."""
    import os
    command = req.command.strip()
    if not command:
        raise HTTPException(400, "Command is required")
    try:
        env = {
            **os.environ,
            "MOTION_CAMERA": "Test",
            "MOTION_TIME": "2000-01-01T00:00:00",
            "MOTION_INTENSITY": "0",
            "DETECTION_LABEL": "",
            "DETECTION_CONFIDENCE": "",
        }
        proc = await asyncio.create_subprocess_shell(
            command, env=env,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=15)
        return TestScriptResponse(
            exit_code=proc.returncode,
            stdout=stdout.decode(errors="replace")[-2000:],
            stderr=stderr.decode(errors="replace")[-2000:],
        )
    except asyncio.TimeoutError:
        try:
            proc.kill()
        except Exception:
            pass
        return TestScriptResponse(exit_code=-1, stdout="", stderr="", timed_out=True)
    except Exception as e:
        return TestScriptResponse(exit_code=-1, stdout="", stderr=str(e))


@router.delete("/{camera_id}", status_code=204)
async def delete_camera(
    camera_id: int,
    purge_data: bool = Query(False, description="Also delete recording and thumbnail files from disk"),
    db: AsyncSession = Depends(get_db),
):
    """Delete a camera and stop its stream.

    With ``purge_data=true`` the camera's recording files, thumbnails, and
    all related DB rows (recordings, motion events, clip exports) are
    permanently deleted from disk. Without it the files are preserved.
    """
    camera = await db.get(Camera, camera_id)
    if not camera:
        raise HTTPException(status_code=404, detail="Camera not found")

    camera_name = camera.name  # capture before delete

    mgr = get_stream_manager()
    await mgr.stop_stream(camera.id)
    from app.services.frame_broker import get_frame_broker
    await get_frame_broker().remove_camera(camera.id)

    # Remove DB metadata (recordings + clip exports) so FK constraints don't block delete.
    for model in (ClipExport, MotionEvent, Recording):
        result = await db.execute(select(model).where(model.camera_id == camera_id))
        for row in result.scalars().all():
            await db.delete(row)

    await db.delete(camera)
    await db.commit()
    logger.info("Camera deleted", extra={
        "camera_id": camera_id, "purge_data": purge_data,
    })

    # Purge on-disk data if requested.
    if purge_data:
        from app.services.ffmpeg import sanitize_camera_name
        config = get_config()
        safe_name = sanitize_camera_name(camera_name)
        for base, label in [
            (config.storage.recordings_dir, "recordings"),
            (config.storage.thumbnails_dir, "thumbnails"),
        ]:
            cam_dir = Path(base) / safe_name
            if cam_dir.is_dir():
                import shutil as _shutil
                _shutil.rmtree(cam_dir, ignore_errors=True)
                logger.info(
                    "Purged camera data directory",
                    extra={"camera_id": camera_id, "type": label, "path": str(cam_dir)},
                )

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
