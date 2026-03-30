"""Motion event API endpoints."""

import logging
from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models import MotionEvent
from app.schemas import MotionEventResponse

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/motion", tags=["motion"])


@router.get("/{camera_id}/events", response_model=list[MotionEventResponse])
async def get_motion_events(
    camera_id: int,
    date: str = Query(..., description="Date in YYYY-MM-DD format"),
    db: AsyncSession = Depends(get_db),
):
    """List motion events for a camera on a specific date."""
    day_start = datetime.strptime(date, "%Y-%m-%d")
    day_end = day_start + timedelta(days=1)

    result = await db.execute(
        select(MotionEvent)
        .where(MotionEvent.camera_id == camera_id)
        .where(MotionEvent.start_time >= day_start)
        .where(MotionEvent.start_time < day_end)
        .order_by(MotionEvent.start_time)
    )
    events = result.scalars().all()
    logger.debug("Listed motion events", extra={"camera_id": camera_id, "date": date, "count": len(events)})
    return events
