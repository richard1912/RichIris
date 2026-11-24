"""Events feed: detection events with the AI guard's description of each.

Backs the app's Events pane. See services/guard.py for where assessments come from.
"""

import json
import logging
from datetime import datetime, timedelta
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import FileResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models import Camera, EventAssessment, MotionEvent
from app.services.guard import SUSPICION_LABELS, get_guard

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/events", tags=["events"])


def _assessment(a: EventAssessment | None, event_id: int) -> dict | None:
    if a is None:
        return None
    return {
        "status": a.status, "model": a.model, "subject": a.subject, "people_count": a.people_count,
        "description": a.description, "activity": a.activity, "suspicion": a.suspicion,
        "suspicion_label": SUSPICION_LABELS[max(0, min(a.suspicion or 0, 3))],
        "reason": a.reason, "would_alert": bool(a.would_alert), "latency_ms": a.latency_ms,
        "frame_urls": [f"/api/events/{event_id}/frames/{n}.jpg" for n in range(a.frame_count or 0)],
        "created_at": a.created_at.isoformat() if a.created_at else None,
        "error": a.error,
    }


@router.get("")
async def list_events(
    limit: int = Query(50, ge=1, le=200),
    before: str | None = Query(None, description="Only events that started before this local ISO time"),
    camera_id: int | None = None,
    min_suspicion: int | None = Query(None, ge=0, le=3),
    assessed: bool = Query(True, description="Only events the AI guard has looked at"),
    db: AsyncSession = Depends(get_db),
) -> list[dict]:
    """Detection events, newest first, each with the guard's assessment (or null)."""
    q = (select(MotionEvent, EventAssessment, Camera.name)
         .join(Camera, Camera.id == MotionEvent.camera_id)
         .where(MotionEvent.detection_label.is_not(None)))
    needs_assessment = assessed or (min_suspicion is not None and min_suspicion > 0)
    if needs_assessment:
        q = q.join(EventAssessment, EventAssessment.motion_event_id == MotionEvent.id)
    else:
        q = q.outerjoin(EventAssessment, EventAssessment.motion_event_id == MotionEvent.id)
    if min_suspicion:
        q = q.where(EventAssessment.suspicion >= min_suspicion)
    if camera_id is not None:
        q = q.where(MotionEvent.camera_id == camera_id)
    if before:
        try:
            q = q.where(MotionEvent.start_time < datetime.fromisoformat(before))
        except ValueError:
            raise HTTPException(status_code=400, detail="before must be an ISO datetime")
    rows = (await db.execute(q.order_by(MotionEvent.start_time.desc()).limit(limit))).all()
    out = []
    for ev, a, cam_name in rows:
        try:
            fired = json.loads(ev.scripts_fired) if ev.scripts_fired else []
        except ValueError:
            fired = []
        out.append({
            "id": ev.id, "camera_id": ev.camera_id, "camera_name": cam_name,
            "start_time": ev.start_time.isoformat(),
            "end_time": ev.end_time.isoformat() if ev.end_time else None,
            "label": ev.detection_label, "confidence": ev.detection_confidence,
            "thumbnail_url": f"/api/motion/{ev.camera_id}/events/{ev.id}/thumbnail" if ev.thumbnail_path else None,
            "scripts_fired": fired,
            "assessment": _assessment(a, ev.id),
        })
    return out


@router.get("/{event_id}/frames/{n}.jpg")
async def event_frame(event_id: int, n: int, db: AsyncSession = Depends(get_db)):
    """One of the frames the guard was shown for this event."""
    a = (await db.execute(
        select(EventAssessment).where(EventAssessment.motion_event_id == event_id))).scalar_one_or_none()
    if a is None or not a.frames_dir or n < 0 or n >= (a.frame_count or 0):
        raise HTTPException(status_code=404, detail="Frame not found")
    path = Path(a.frames_dir) / f"{n}.jpg"
    if not path.exists():
        raise HTTPException(status_code=404, detail="Frame file missing")
    return FileResponse(path, media_type="image/jpeg", headers={"Cache-Control": "public, max-age=86400"})


@router.get("/human")
async def human(camera: str, window: float = 45, wait: float = 12,
                db: AsyncSession = Depends(get_db)) -> dict:
    """For the alert scripts: has the AI confirmed a real person on this camera just now?

    `human` is true, false (every recent person event was a false detection: do not notify)
    or null (unknown: notify). Waits up to `wait` seconds for a verdict still in progress.
    """
    cam = (await db.execute(select(Camera).where(Camera.name == camera))).scalar_one_or_none()
    if cam is None:
        raise HTTPException(status_code=404, detail="Camera not found")
    return await get_guard().human_on_camera(cam.id, min(max(window, 5), 600), min(max(wait, 0), 25))


@router.post("/backfill")
async def backfill(date: str, cameras: str = "", from_time: str = "00:00", hours: float = 24) -> dict:
    """Assess a past day's person events from the recordings (those with no verdict yet).

    Runs in the background; GET the same path for progress. One Claude call per event.
    """
    import asyncio
    guard = get_guard()
    if guard.backfill_state.get("running"):
        raise HTTPException(status_code=409, detail="A backfill is already running")
    try:
        day = datetime.strptime(f"{date} {from_time}", "%Y-%m-%d %H:%M")
    except ValueError:
        raise HTTPException(status_code=400, detail="date must be YYYY-MM-DD and from_time HH:MM")
    wanted = {c.strip() for c in cameras.split(",") if c.strip()} or None
    guard.backfill_state = {"running": True, "day": date, "total": None, "done": 0}
    task = asyncio.get_running_loop().create_task(guard.backfill(day, wanted, day + timedelta(hours=hours)))
    guard._tasks.add(task)
    task.add_done_callback(guard._tasks.discard)
    return {"started": date}


@router.get("/backfill")
async def backfill_progress() -> dict:
    return get_guard().backfill_state


@router.post("/try")
async def try_guard(camera_id: int, event_id: int | None = None, db: AsyncSession = Depends(get_db)) -> dict:
    """Run the guard on a camera's live picture right now and return its verdict.

    For checking the model and tuning the brief without waiting for a person. Stores nothing,
    unless `event_id` names an existing motion event with no assessment yet: then the verdict is
    saved against it through the normal path (a test hook: the frames are of NOW, not of that event).
    """
    cam = await db.get(Camera, camera_id)
    if cam is None:
        raise HTTPException(status_code=404, detail="Camera not found")
    guard = get_guard()
    cfg = await guard._settings()
    if event_id is not None:
        ev = await db.get(MotionEvent, event_id)
        if ev is None or ev.camera_id != camera_id:
            raise HTTPException(status_code=404, detail="Event not found on that camera")
        await guard._assess_and_store(ev.id, cam.id, cam.name, ev.start_time, None, cfg)
        return {"camera": cam.name, "stored_for_event": ev.id}
    try:
        verdict, frames = await guard.assess(cam.name, datetime.now(), None, cfg)
    except RuntimeError as e:
        raise HTTPException(status_code=502, detail=str(e))
    return {"camera": cam.name, "model": cfg["model"], "frames": len(frames), "verdict": verdict}
