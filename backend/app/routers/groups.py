"""Camera group CRUD and bulk actions."""

import logging

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models import Camera, CameraGroup
from app.schemas import CameraGroupCreate, CameraGroupResponse, CameraGroupUpdate

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/groups", tags=["groups"])


@router.get("", response_model=list[CameraGroupResponse])
async def list_groups(db: AsyncSession = Depends(get_db)):
    """List all camera groups with camera counts."""
    result = await db.execute(
        select(
            CameraGroup,
            func.count(Camera.id).label("camera_count"),
        )
        .outerjoin(Camera, Camera.group_id == CameraGroup.id)
        .group_by(CameraGroup.id)
        .order_by(CameraGroup.sort_order, CameraGroup.id)
    )
    rows = result.all()
    return [
        CameraGroupResponse(
            id=group.id,
            name=group.name,
            sort_order=group.sort_order,
            camera_count=count,
        )
        for group, count in rows
    ]


@router.post("", response_model=CameraGroupResponse, status_code=201)
async def create_group(data: CameraGroupCreate, db: AsyncSession = Depends(get_db)):
    """Create a new camera group."""
    # Check for duplicate name
    existing = await db.execute(
        select(CameraGroup).where(CameraGroup.name == data.name)
    )
    if existing.scalar_one_or_none():
        raise HTTPException(status_code=409, detail="Group name already exists")

    group = CameraGroup(name=data.name)
    db.add(group)
    await db.commit()
    await db.refresh(group)
    logger.info("Camera group created", extra={"group_id": group.id, "group_name": group.name})
    return CameraGroupResponse(id=group.id, name=group.name, sort_order=group.sort_order, camera_count=0)


@router.put("/{group_id}", response_model=CameraGroupResponse)
async def update_group(group_id: int, data: CameraGroupUpdate, db: AsyncSession = Depends(get_db)):
    """Update a camera group."""
    group = await db.get(CameraGroup, group_id)
    if not group:
        raise HTTPException(status_code=404, detail="Group not found")

    if data.name is not None:
        # Check for duplicate name
        existing = await db.execute(
            select(CameraGroup).where(CameraGroup.name == data.name, CameraGroup.id != group_id)
        )
        if existing.scalar_one_or_none():
            raise HTTPException(status_code=409, detail="Group name already exists")
        group.name = data.name

    if data.sort_order is not None:
        group.sort_order = data.sort_order

    await db.commit()
    await db.refresh(group)

    # Get camera count
    count_result = await db.execute(
        select(func.count(Camera.id)).where(Camera.group_id == group_id)
    )
    camera_count = count_result.scalar() or 0

    logger.info("Camera group updated", extra={"group_id": group.id})
    return CameraGroupResponse(id=group.id, name=group.name, sort_order=group.sort_order, camera_count=camera_count)


@router.delete("/{group_id}", status_code=204)
async def delete_group(group_id: int, db: AsyncSession = Depends(get_db)):
    """Delete a camera group. Cameras in the group become ungrouped."""
    group = await db.get(CameraGroup, group_id)
    if not group:
        raise HTTPException(status_code=404, detail="Group not found")

    # Unassign cameras (ON DELETE SET NULL handles this in DB, but be explicit)
    await db.execute(
        update(Camera).where(Camera.group_id == group_id).values(group_id=None)
    )
    await db.delete(group)
    await db.commit()
    logger.info("Camera group deleted", extra={"group_id": group_id})


from pydantic import BaseModel


class BulkActionRequest(BaseModel):
    action: str  # "enable", "disable", "arm_motion", "disarm_motion"


@router.post("/{group_id}/bulk")
async def bulk_action(group_id: int, data: BulkActionRequest, db: AsyncSession = Depends(get_db)):
    """Apply a bulk action to all cameras in a group."""
    group = await db.get(CameraGroup, group_id)
    if not group:
        raise HTTPException(status_code=404, detail="Group not found")

    action_map = {
        "enable": {"enabled": True},
        "disable": {"enabled": False},
        "arm_motion": {"ai_detection": True},
        "disarm_motion": {"ai_detection": False},
    }

    if data.action not in action_map:
        raise HTTPException(status_code=400, detail=f"Unknown action: {data.action}")

    result = await db.execute(
        update(Camera).where(Camera.group_id == group_id).values(**action_map[data.action])
    )
    await db.commit()

    logger.info("Bulk action applied", extra={
        "group_id": group_id, "action": data.action, "cameras_affected": result.rowcount,
    })
    return {"ok": True, "cameras_affected": result.rowcount}
