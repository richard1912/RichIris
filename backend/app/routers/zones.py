"""Per-camera detection zones CRUD.

Zones are normalized polygon masks (points in [0,1]) that motion scripts can
opt into via `zone_ids`. Any mutation invalidates the ZoneMaskCache so the
next detection loop tick rebuilds the union masks.
"""

import json
import logging

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models import Camera, Zone
from app.schemas import ZoneCreate, ZoneResponse, ZoneUpdate
from app.services.zone_mask import get_zone_mask_cache

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/cameras", tags=["zones"])


def _validate_points(points: list[list[float]]) -> str:
    """Validate polygon points and return a JSON-serialized form, or raise."""
    if not isinstance(points, list) or len(points) < 3:
        raise HTTPException(status_code=400, detail="Zone needs at least 3 points")
    cleaned: list[list[float]] = []
    for p in points:
        if not isinstance(p, (list, tuple)) or len(p) < 2:
            raise HTTPException(status_code=400, detail="Each point must be [x,y]")
        x, y = float(p[0]), float(p[1])
        if not (0.0 <= x <= 1.0 and 0.0 <= y <= 1.0):
            raise HTTPException(
                status_code=400,
                detail=f"Point ({x},{y}) out of range — coords must be in [0,1]",
            )
        cleaned.append([x, y])
    return json.dumps(cleaned)


async def _get_camera(db: AsyncSession, camera_id: int) -> Camera:
    camera = await db.get(Camera, camera_id)
    if camera is None:
        raise HTTPException(status_code=404, detail="Camera not found")
    return camera


async def _prune_zone_id_from_scripts(db: AsyncSession, camera: Camera, zone_id: int) -> None:
    """Remove zone_id from any motion_scripts entry on this camera."""
    if not camera.motion_scripts:
        return
    try:
        scripts = json.loads(camera.motion_scripts)
    except (json.JSONDecodeError, TypeError):
        return
    changed = False
    for s in scripts:
        zids = s.get("zone_ids") or []
        if zone_id in zids:
            s["zone_ids"] = [z for z in zids if z != zone_id]
            changed = True
    if changed:
        camera.motion_scripts = json.dumps(scripts)


@router.get("/{camera_id}/zones", response_model=list[ZoneResponse])
async def list_zones(camera_id: int, db: AsyncSession = Depends(get_db)):
    await _get_camera(db, camera_id)
    result = await db.execute(
        select(Zone).where(Zone.camera_id == camera_id).order_by(Zone.id)
    )
    return [ZoneResponse.from_zone(z) for z in result.scalars().all()]


@router.post("/{camera_id}/zones", response_model=ZoneResponse)
async def create_zone(
    camera_id: int, payload: ZoneCreate, db: AsyncSession = Depends(get_db)
):
    await _get_camera(db, camera_id)
    name = (payload.name or "").strip()
    if not name:
        raise HTTPException(status_code=400, detail="Zone name required")
    points_json = _validate_points(payload.points)
    zone = Zone(camera_id=camera_id, name=name, points_json=points_json)
    db.add(zone)
    await db.commit()
    await db.refresh(zone)
    get_zone_mask_cache().invalidate(zone.id)
    logger.info("Zone created", extra={"camera_id": camera_id, "zone_id": zone.id, "zone_name": name})
    return ZoneResponse.from_zone(zone)


@router.put("/{camera_id}/zones/{zone_id}", response_model=ZoneResponse)
async def update_zone(
    camera_id: int, zone_id: int, payload: ZoneUpdate,
    db: AsyncSession = Depends(get_db),
):
    zone = await db.get(Zone, zone_id)
    if zone is None or zone.camera_id != camera_id:
        raise HTTPException(status_code=404, detail="Zone not found")
    if payload.name is not None:
        name = payload.name.strip()
        if not name:
            raise HTTPException(status_code=400, detail="Zone name required")
        zone.name = name
    if payload.points is not None:
        zone.points_json = _validate_points(payload.points)
    await db.commit()
    await db.refresh(zone)
    get_zone_mask_cache().invalidate(zone.id)
    logger.info("Zone updated", extra={"camera_id": camera_id, "zone_id": zone.id})
    return ZoneResponse.from_zone(zone)


@router.delete("/{camera_id}/zones/{zone_id}")
async def delete_zone(
    camera_id: int, zone_id: int, db: AsyncSession = Depends(get_db)
):
    zone = await db.get(Zone, zone_id)
    if zone is None or zone.camera_id != camera_id:
        raise HTTPException(status_code=404, detail="Zone not found")
    camera = await _get_camera(db, camera_id)
    await _prune_zone_id_from_scripts(db, camera, zone_id)
    await db.delete(zone)
    await db.commit()
    get_zone_mask_cache().invalidate(zone_id)
    logger.info("Zone deleted", extra={"camera_id": camera_id, "zone_id": zone_id})
    return {"ok": True}
