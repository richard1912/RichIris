"""Face enrollment and CRUD API."""

import logging
from datetime import datetime, timedelta
from pathlib import Path

import cv2
import numpy as np
from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import FileResponse
from sqlalchemy import desc, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_config
from app.database import get_db
from app.models import Camera, Face, FaceEmbedding, MotionEvent
from app.schemas import (
    FaceCreate,
    FaceEmbeddingInfo,
    FaceEnrollCandidate,
    FaceEnrollRequest,
    FaceEnrollResponse,
    FaceResponse,
    FaceUpdate,
    UnlabeledThumb,
)
from app.services.face_recognizer import get_face_recognizer
from app.services.ffmpeg import sanitize_camera_name
from app.services.object_detector import build_class_list, get_object_detector

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/faces", tags=["faces"])


async def _latest_crop(db: AsyncSession, face_id: int) -> str | None:
    row = await db.execute(
        select(FaceEmbedding.face_crop_path)
        .where(FaceEmbedding.face_id == face_id)
        .order_by(desc(FaceEmbedding.id))
        .limit(1)
    )
    path = row.scalar_one_or_none()
    return path


@router.get("", response_model=list[FaceResponse])
async def list_faces(db: AsyncSession = Depends(get_db)):
    result = await db.execute(
        select(Face, func.count(FaceEmbedding.id).label("cnt"))
        .outerjoin(FaceEmbedding, FaceEmbedding.face_id == Face.id)
        .group_by(Face.id)
        .order_by(Face.name)
    )
    out: list[FaceResponse] = []
    for face, cnt in result.all():
        latest = await _latest_crop(db, face.id)
        out.append(FaceResponse(
            id=face.id, name=face.name, notes=face.notes,
            embedding_count=cnt, latest_crop_path=latest,
            created_at=face.created_at,
        ))
    return out


@router.post("", response_model=FaceResponse, status_code=201)
async def create_face(data: FaceCreate, db: AsyncSession = Depends(get_db)):
    existing = await db.execute(select(Face).where(Face.name == data.name))
    if existing.scalar_one_or_none():
        raise HTTPException(status_code=409, detail="Face name already exists")
    face = Face(name=data.name, notes=data.notes)
    db.add(face)
    await db.commit()
    await db.refresh(face)
    logger.info("Face created", extra={"face_id": face.id, "face_name": face.name})
    return FaceResponse(
        id=face.id, name=face.name, notes=face.notes,
        embedding_count=0, latest_crop_path=None, created_at=face.created_at,
    )


@router.put("/{face_id}", response_model=FaceResponse)
async def update_face(face_id: int, data: FaceUpdate, db: AsyncSession = Depends(get_db)):
    face = await db.get(Face, face_id)
    if not face:
        raise HTTPException(status_code=404, detail="Face not found")
    if data.name is not None and data.name != face.name:
        existing = await db.execute(
            select(Face).where(Face.name == data.name, Face.id != face_id)
        )
        if existing.scalar_one_or_none():
            raise HTTPException(status_code=409, detail="Face name already exists")
        face.name = data.name
    if data.notes is not None:
        face.notes = data.notes
    await db.commit()
    await db.refresh(face)
    count_result = await db.execute(
        select(func.count(FaceEmbedding.id)).where(FaceEmbedding.face_id == face_id)
    )
    cnt = count_result.scalar() or 0
    latest = await _latest_crop(db, face_id)
    # Matcher cache holds name, so rebuild
    recognizer = get_face_recognizer()
    await recognizer.reload_cache()
    return FaceResponse(
        id=face.id, name=face.name, notes=face.notes,
        embedding_count=cnt, latest_crop_path=latest, created_at=face.created_at,
    )


@router.delete("/{face_id}", status_code=204)
async def delete_face(face_id: int, db: AsyncSession = Depends(get_db)):
    face = await db.get(Face, face_id)
    if not face:
        raise HTTPException(status_code=404, detail="Face not found")
    await db.delete(face)
    await db.commit()
    recognizer = get_face_recognizer()
    await recognizer.reload_cache()
    logger.info("Face deleted", extra={"face_id": face_id})


@router.get("/{face_id}/embeddings", response_model=list[FaceEmbeddingInfo])
async def list_embeddings(face_id: int, db: AsyncSession = Depends(get_db)):
    face = await db.get(Face, face_id)
    if not face:
        raise HTTPException(status_code=404, detail="Face not found")
    result = await db.execute(
        select(FaceEmbedding).where(FaceEmbedding.face_id == face_id).order_by(desc(FaceEmbedding.id))
    )
    return list(result.scalars().all())


FACE_CROP_MARGIN = 0.5    # Expand bbox by 50% each side so we include hair/chin/ears
FACE_CROP_OUTPUT = 192    # Final square thumbnail size

def _save_face_crop(frame: np.ndarray, bbox: tuple[int, int, int, int]) -> str | None:
    """Save a padded, square face thumbnail suitable for the Faces UI.

    The raw SCRFD bbox is tight on the face; expanding it and centering on a
    square canvas gives a much more recognisable avatar. Small crops are
    upscaled with cubic interpolation so the stored image is always
    FACE_CROP_OUTPUT × FACE_CROP_OUTPUT regardless of how far away the subject
    was from the camera.
    """
    try:
        config = get_config()
        thumb_root = Path(config.storage.thumbnails_dir)
        face_dir = thumb_root / "_faces"
        face_dir.mkdir(parents=True, exist_ok=True)
        x1, y1, x2, y2 = bbox
        h, w = frame.shape[:2]

        # Expand bbox by margin and clamp to frame
        bw = x2 - x1
        bh = y2 - y1
        pad_x = int(bw * FACE_CROP_MARGIN)
        pad_y = int(bh * FACE_CROP_MARGIN)
        x1 = max(0, x1 - pad_x)
        y1 = max(0, y1 - pad_y)
        x2 = min(w, x2 + pad_x)
        y2 = min(h, y2 + pad_y)
        if x2 <= x1 or y2 <= y1:
            return None

        # Make the crop square (around the center) so upscale doesn't stretch
        cx = (x1 + x2) // 2
        cy = (y1 + y2) // 2
        side = max(x2 - x1, y2 - y1)
        half = side // 2
        sx1 = max(0, cx - half)
        sy1 = max(0, cy - half)
        sx2 = min(w, cx + half)
        sy2 = min(h, cy + half)
        crop = frame[sy1:sy2, sx1:sx2]
        if crop.size == 0:
            return None

        # Resize to standard output. Upscale small crops with cubic for
        # smoother edges; downscale large ones with area for sharpness.
        ch, cw = crop.shape[:2]
        interp = cv2.INTER_CUBIC if max(ch, cw) < FACE_CROP_OUTPUT else cv2.INTER_AREA
        out = cv2.resize(crop, (FACE_CROP_OUTPUT, FACE_CROP_OUTPUT), interpolation=interp)

        filename = f"face_{datetime.now().strftime('%Y%m%d_%H%M%S_%f')}.jpg"
        path = face_dir / filename
        cv2.imwrite(str(path), out, [cv2.IMWRITE_JPEG_QUALITY, 92])
        return str(path)
    except Exception:
        logger.exception("Failed to save enrollment face crop")
        return None


@router.post("/{face_id}/embeddings", response_model=FaceEnrollResponse)
async def enroll_embedding(
    face_id: int, data: FaceEnrollRequest, db: AsyncSession = Depends(get_db),
):
    """Run SCRFD+ArcFace on a thumbnail, store the matching face embedding."""
    face = await db.get(Face, face_id)
    if not face:
        raise HTTPException(status_code=404, detail="Face not found")

    recognizer = get_face_recognizer()
    if not recognizer.available:
        raise HTTPException(status_code=503, detail="Face recognizer not ready")

    src = Path(data.source_thumbnail_path)
    if not src.exists():
        raise HTTPException(status_code=404, detail="Source image not found")

    frame = cv2.imread(str(src))
    if frame is None:
        raise HTTPException(status_code=400, detail="Could not read source image")

    # Mirror the live pipeline's two-stage approach: full-frame SCRFD first
    # (catches close-up or obvious faces), then fall back to running SCRFD
    # inside each RT-DETR person bbox. Without the crop step, tiny faces in
    # high-res thumbnails (e.g. a person at the far end of a 4K frame) get
    # downscaled into oblivion when SCRFD letterboxes to 640×640 and are
    # missed entirely.
    hits = await recognizer.detect_and_embed(frame, person_bbox=None)
    if not hits:
        detector = get_object_detector()
        if detector and detector._started:
            persons = await detector.detect_objects(
                frame, 0.5, classes=build_class_list(True, False, False),
            )
            seen_boxes: list[tuple[int, int, int, int]] = []
            for p in persons:
                bbox = (p.x1, p.y1, p.x2, p.y2)
                crop_hits = await recognizer.detect_and_embed(frame, person_bbox=bbox)
                for h in crop_hits:
                    # Dedupe: skip faces that overlap ones we've already kept
                    key = (h.x1, h.y1, h.x2, h.y2)
                    if any(abs(k[0] - key[0]) < 20 and abs(k[1] - key[1]) < 20
                           for k in seen_boxes):
                        continue
                    seen_boxes.append(key)
                    hits.append(h)
    if not hits:
        return FaceEnrollResponse(status="no_face")

    chosen = None
    if data.bbox is not None and len(data.bbox) == 4:
        # Match the closest hit to the requested bbox by center distance
        tx = (data.bbox[0] + data.bbox[2]) / 2.0
        ty = (data.bbox[1] + data.bbox[3]) / 2.0
        best_d = float("inf")
        for h in hits:
            cx = (h.x1 + h.x2) / 2.0
            cy = (h.y1 + h.y2) / 2.0
            d = (cx - tx) ** 2 + (cy - ty) ** 2
            if d < best_d:
                best_d = d
                chosen = h
    elif len(hits) == 1:
        chosen = hits[0]
    else:
        # Multiple faces — let the caller pick
        return FaceEnrollResponse(
            status="multiple_faces",
            candidates=[
                FaceEnrollCandidate(bbox=[h.x1, h.y1, h.x2, h.y2], score=h.score)
                for h in hits
            ],
        )

    if chosen is None:
        return FaceEnrollResponse(status="no_face")

    crop_path = _save_face_crop(frame, (chosen.x1, chosen.y1, chosen.x2, chosen.y2))
    blob = chosen.embedding.astype(np.float32).tobytes()
    emb = FaceEmbedding(
        face_id=face_id,
        embedding=blob,
        source_thumbnail_path=str(src),
        face_crop_path=crop_path,
    )
    db.add(emb)
    await db.commit()
    await db.refresh(emb)
    await recognizer.reload_cache()
    logger.info("Face embedding enrolled", extra={
        "face_id": face_id, "embedding_id": emb.id, "score": chosen.score,
    })
    return FaceEnrollResponse(status="enrolled", embedding_id=emb.id, crop_path=crop_path)


@router.delete("/embeddings/{embedding_id}", status_code=204)
async def delete_embedding(embedding_id: int, db: AsyncSession = Depends(get_db)):
    emb = await db.get(FaceEmbedding, embedding_id)
    if not emb:
        raise HTTPException(status_code=404, detail="Embedding not found")
    await db.delete(emb)
    await db.commit()
    recognizer = get_face_recognizer()
    await recognizer.reload_cache()


@router.get("/thumbnails/unlabeled", response_model=list[UnlabeledThumb])
async def unlabeled_thumbnails(
    date: str | None = Query(None, description="YYYY-MM-DD — defaults to last 7 days"),
    camera_id: int | None = Query(None),
    limit: int = Query(100, ge=1, le=500),
    with_face_only: bool = Query(True, description="Only include events where SCRFD detected a face"),
    db: AsyncSession = Depends(get_db),
):
    """Return recent motion-event thumbnails suitable for face enrollment.

    By default, only thumbnails where the face pipeline actually detected a
    face during the live event are returned (i.e. `face_matches` is set or
    `face_unknown` is true). This dramatically trims noise from distant
    walk-bys that contain no usable face. Pass `with_face_only=false` to see
    every person event (useful for cameras where Face Recognition is disabled,
    so nothing populates `face_matches`/`face_unknown`).
    """
    query = (
        select(MotionEvent, Camera)
        .join(Camera, Camera.id == MotionEvent.camera_id)
        .where(MotionEvent.thumbnail_path.isnot(None))
        .where(MotionEvent.detection_label == "person")
    )
    if with_face_only:
        # face_detected is populated on every person event after v0.0.15;
        # fall back to the older face_matches/face_unknown signals so thumbnails
        # from FR-enabled cameras captured before the upgrade still qualify.
        query = query.where(
            (MotionEvent.face_detected == True)  # noqa: E712
            | (MotionEvent.face_matches.isnot(None))
            | (MotionEvent.face_unknown == True)  # noqa: E712
        )
    if date is not None:
        day_start = datetime.strptime(date, "%Y-%m-%d")
        day_end = day_start + timedelta(days=1)
        query = query.where(MotionEvent.start_time >= day_start).where(MotionEvent.start_time < day_end)
    else:
        cutoff = datetime.now() - timedelta(days=7)
        query = query.where(MotionEvent.start_time >= cutoff)
    if camera_id is not None:
        query = query.where(MotionEvent.camera_id == camera_id)
    query = query.order_by(desc(MotionEvent.start_time)).limit(limit)
    result = await db.execute(query)
    events = result.all()

    # Bulk-lookup: which thumbs have already been enrolled to which faces.
    # Done as a single join keyed on source_thumbnail_path.
    thumb_paths = [e.thumbnail_path for e, _ in events if e.thumbnail_path]
    assigned: dict[str, list[str]] = {}
    if thumb_paths:
        rows = await db.execute(
            select(FaceEmbedding.source_thumbnail_path, Face.name)
            .join(Face, Face.id == FaceEmbedding.face_id)
            .where(FaceEmbedding.source_thumbnail_path.in_(thumb_paths))
        )
        for path, name in rows.all():
            assigned.setdefault(path, []).append(name)

    out: list[UnlabeledThumb] = []
    for event, camera in events:
        names = assigned.get(event.thumbnail_path, [])
        # Dedupe while preserving order (multiple embeddings per face possible)
        seen: set[str] = set()
        unique_names = [n for n in names if not (n in seen or seen.add(n))]
        out.append(UnlabeledThumb(
            event_id=event.id,
            camera_id=camera.id,
            camera_name=camera.name,
            start_time=event.start_time,
            thumbnail_url=f"/api/motion/{camera.id}/events/{event.id}/thumbnail",
            detection_label=event.detection_label,
            assigned_face_names=unique_names,
        ))
    return out


@router.get("/thumbnails/event/{event_id}/path")
async def resolve_event_thumbnail_path(event_id: int, db: AsyncSession = Depends(get_db)):
    """Resolve a motion-event id to the on-disk thumbnail path (used for enrollment)."""
    event = await db.get(MotionEvent, event_id)
    if not event or not event.thumbnail_path:
        raise HTTPException(status_code=404, detail="Thumbnail not found")
    return {"source_thumbnail_path": event.thumbnail_path}


@router.get("/embeddings/{embedding_id}/crop")
async def get_embedding_crop(embedding_id: int, db: AsyncSession = Depends(get_db)):
    emb = await db.get(FaceEmbedding, embedding_id)
    if not emb or not emb.face_crop_path:
        raise HTTPException(status_code=404, detail="Crop not found")
    path = Path(emb.face_crop_path)
    if not path.exists():
        raise HTTPException(status_code=404, detail="Crop file missing")
    return FileResponse(
        path, media_type="image/jpeg",
        headers={"Cache-Control": "public, max-age=86400"},
    )


@router.get("/{face_id}/latest-crop")
async def latest_crop_file(face_id: int, db: AsyncSession = Depends(get_db)):
    path_str = await _latest_crop(db, face_id)
    if not path_str:
        raise HTTPException(status_code=404, detail="No crops for face")
    path = Path(path_str)
    if not path.exists():
        raise HTTPException(status_code=404, detail="Crop file missing")
    return FileResponse(
        path, media_type="image/jpeg",
        headers={"Cache-Control": "public, max-age=86400"},
    )
