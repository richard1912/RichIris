"""PTZ control endpoints.

Vendor-neutral: everything goes through `PtzManager`, which picks the driver
named in the camera's `ptz_driver` column. A camera without PTZ answers 400.
Camera-side failures come back as 502 (refused / unreachable) or 504 (no
answer in time), never as a hung request.
"""

import logging
from dataclasses import asdict

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models import Camera
from app.schemas import (
    PtzDetectResponse,
    PtzInfoResponse,
    PtzMoveRequest,
    PtzMoveResponse,
    PtzPositionRequest,
    PtzPresetCreate,
    PtzPresetResponse,
    PtzStatusResponse,
)
from app.services.ptz import (
    DIRECTIONS,
    PtzError,
    get_ptz_manager,
    has_ptz,
    refresh_camera_ptz,
)

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/cameras", tags=["ptz"])


async def _get_camera(db: AsyncSession, camera_id: int) -> Camera:
    camera = await db.get(Camera, camera_id)
    if camera is None:
        raise HTTPException(status_code=404, detail="Camera not found")
    return camera


def _http_error(camera_id: int, action: str, e: PtzError) -> HTTPException:
    if e.status_code >= 500:
        logger.warning("PTZ request failed", extra={
            "camera_id": camera_id, "action": action, "error": e.detail,
        })
    return HTTPException(status_code=e.status_code, detail=e.detail)


@router.get("/{camera_id}/ptz", response_model=PtzInfoResponse)
async def ptz_info(camera_id: int, db: AsyncSession = Depends(get_db)):
    """Driver name and the camera's limits (ranges, speeds, preset slots)."""
    camera = await _get_camera(db, camera_id)
    try:
        driver = await get_ptz_manager().driver_for(camera)
        caps = await driver.capabilities()
    except PtzError as e:
        raise _http_error(camera_id, "info", e)
    return {"driver": driver.name, "capabilities": asdict(caps)}


@router.post("/{camera_id}/ptz/detect", response_model=PtzDetectResponse)
async def ptz_detect(camera_id: int, db: AsyncSession = Depends(get_db)):
    """Probe the camera again and store whether it has PTZ."""
    camera = await _get_camera(db, camera_id)
    if await refresh_camera_ptz(camera):
        await db.commit()
    return {"ptz_driver": camera.ptz_driver, "ptz": has_ptz(camera.ptz_driver)}


@router.get("/{camera_id}/ptz/status", response_model=PtzStatusResponse)
async def ptz_status(camera_id: int, db: AsyncSession = Depends(get_db)):
    """Current pan and tilt (degrees), zoom (x) and whether it is moving."""
    camera = await _get_camera(db, camera_id)
    try:
        driver = await get_ptz_manager().driver_for(camera)
        return asdict(await driver.status())
    except PtzError as e:
        raise _http_error(camera_id, "status", e)


@router.post("/{camera_id}/ptz/move", response_model=PtzMoveResponse)
async def ptz_move(camera_id: int, req: PtzMoveRequest, db: AsyncSession = Depends(get_db)):
    """Start a continuous move, or keep the current one alive.

    The move stops by itself `timeout` seconds after the last call, so a
    client holds a direction by re-sending this (about once a second) and
    ends it with `stop`. A client that disappears cannot leave it running.
    """
    if req.direction not in DIRECTIONS:
        raise HTTPException(
            status_code=422,
            detail=f"direction must be one of: {', '.join(DIRECTIONS)}",
        )
    camera = await _get_camera(db, camera_id)
    try:
        timeout = await get_ptz_manager().move(camera, req.direction, req.speed, req.timeout)
    except PtzError as e:
        raise _http_error(camera_id, "move", e)
    return {"ok": True, "timeout": timeout}


@router.post("/{camera_id}/ptz/stop")
async def ptz_stop(camera_id: int, db: AsyncSession = Depends(get_db)):
    """Stop any continuous move. Safe to call when nothing is moving."""
    camera = await _get_camera(db, camera_id)
    try:
        await get_ptz_manager().stop(camera)
    except PtzError as e:
        raise _http_error(camera_id, "stop", e)
    return {"ok": True}


@router.post("/{camera_id}/ptz/focus")
async def ptz_focus(camera_id: int, db: AsyncSession = Depends(get_db)):
    """Refocus once on the current view."""
    camera = await _get_camera(db, camera_id)
    try:
        await get_ptz_manager().autofocus(camera)
    except PtzError as e:
        raise _http_error(camera_id, "focus", e)
    return {"ok": True}


@router.post("/{camera_id}/ptz/position", response_model=PtzStatusResponse)
async def ptz_position(camera_id: int, req: PtzPositionRequest,
                       db: AsyncSession = Depends(get_db)):
    """Go to an absolute position; answers with the (clamped) target."""
    camera = await _get_camera(db, camera_id)
    try:
        return asdict(await get_ptz_manager().goto(camera, req.pan, req.tilt, req.zoom))
    except PtzError as e:
        raise _http_error(camera_id, "position", e)


@router.get("/{camera_id}/ptz/presets", response_model=list[PtzPresetResponse])
async def ptz_list_presets(camera_id: int, db: AsyncSession = Depends(get_db)):
    camera = await _get_camera(db, camera_id)
    try:
        driver = await get_ptz_manager().driver_for(camera)
        return [asdict(p) for p in await driver.list_presets()]
    except PtzError as e:
        raise _http_error(camera_id, "list_presets", e)


@router.post("/{camera_id}/ptz/presets", response_model=PtzPresetResponse, status_code=201)
async def ptz_save_preset(camera_id: int, req: PtzPresetCreate,
                          db: AsyncSession = Depends(get_db)):
    """Save the current position as a preset (lowest free slot unless `id`
    is given; an existing `id` is overwritten)."""
    camera = await _get_camera(db, camera_id)
    try:
        preset = await get_ptz_manager().save_preset(camera, req.id, req.name)
    except PtzError as e:
        raise _http_error(camera_id, "save_preset", e)
    logger.info("PTZ preset saved", extra={"camera_id": camera_id, "preset_id": preset.id})
    return asdict(preset)


@router.post("/{camera_id}/ptz/presets/{preset_id}/goto")
async def ptz_goto_preset(camera_id: int, preset_id: int, db: AsyncSession = Depends(get_db)):
    camera = await _get_camera(db, camera_id)
    try:
        await get_ptz_manager().goto_preset(camera, preset_id)
    except PtzError as e:
        raise _http_error(camera_id, "goto_preset", e)
    return {"ok": True}


@router.delete("/{camera_id}/ptz/presets/{preset_id}", status_code=204)
async def ptz_delete_preset(camera_id: int, preset_id: int, db: AsyncSession = Depends(get_db)):
    camera = await _get_camera(db, camera_id)
    try:
        driver = await get_ptz_manager().driver_for(camera)
        await driver.delete_preset(preset_id)
    except PtzError as e:
        raise _http_error(camera_id, "delete_preset", e)
    logger.info("PTZ preset deleted", extra={"camera_id": camera_id, "preset_id": preset_id})
