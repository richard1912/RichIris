"""Face enrollment and CRUD API."""

import asyncio
import logging
import uuid  # TEMP FACE-DIAG
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
from app.models import Camera, Face, FaceEmbedding, MotionEvent, UnclusteredFace
from app.schemas import (
    FaceClusterMergeRequest,
    FaceClusterNameRequest,
    FaceClusterResponse,
    FaceCreate,
    FaceEmbeddingInfo,
    FaceEnrollCandidate,
    FaceEnrollRequest,
    FaceEnrollResponse,
    FaceResponse,
    FaceUpdate,
    UnlabeledThumb,
)
from app.services.benchmark import BenchmarkTrace  # TEMP FACE-DIAG
from app.services.face_crop import FACE_CROP_MARGIN, FACE_CROP_OUTPUT, save_face_crop
from app.services.face_recognizer import get_face_recognizer
from app.services.ffmpeg import sanitize_camera_name
from app.services.object_detector import build_class_list, get_object_detector

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/faces", tags=["faces"])


def _diag_id(prefix: str) -> str:  # TEMP FACE-DIAG
    return f"face-{prefix}-{uuid.uuid4().hex[:6]}"


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
    # Named people only. Unnamed clusters live under /api/faces/clusters.
    result = await db.execute(
        select(Face, func.count(FaceEmbedding.id).label("cnt"))
        .outerjoin(FaceEmbedding, FaceEmbedding.face_id == Face.id)
        .where(Face.name.isnot(None))
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


# --- Clustering ("suggested people") endpoints -----------------------------
# Immich-style: the background clusterer creates unnamed Face rows; these
# endpoints let the UI list them, name them (promote to a person), merge two
# clusters, or discard noise. The re-cluster endpoint wipes auto-clustered
# state and lets the worker rebuild from the queue.

@router.get("/clusters", response_model=list[FaceClusterResponse])
async def list_clusters(
    min_size: int = Query(1, ge=1, le=100),
    limit: int = Query(100, ge=1, le=500),
    db: AsyncSession = Depends(get_db),
):
    """Return unnamed face clusters with at least `min_size` embeddings.

    Sorted by embedding count descending so the most confidently-formed
    clusters surface first. Each cluster returns up to 4 sample crop paths
    (highest detection_score first) for the UI mosaic, plus aggregated info
    about which cameras the person appeared on and when.
    """
    rows = (await db.execute(
        select(Face, func.count(FaceEmbedding.id).label("cnt"))
        .join(FaceEmbedding, FaceEmbedding.face_id == Face.id)
        .where(Face.name.is_(None))
        .group_by(Face.id)
        .having(func.count(FaceEmbedding.id) >= min_size)
        .order_by(desc("cnt"))
        .limit(limit)
    )).all()

    out: list[FaceClusterResponse] = []
    for face, cnt in rows:
        # Sample embeddings: best-score first, cap 4. UI fetches crops by id.
        sample_ids = (await db.execute(
            select(FaceEmbedding.id)
            .where(FaceEmbedding.face_id == face.id)
            .where(FaceEmbedding.face_crop_path.isnot(None))
            .order_by(desc(FaceEmbedding.detection_score), desc(FaceEmbedding.id))
            .limit(4)
        )).scalars().all()
        # Aggregate camera names + latest event time via the motion_event fk.
        cam_rows = (await db.execute(
            select(Camera.name, func.max(MotionEvent.start_time))
            .join(MotionEvent, MotionEvent.camera_id == Camera.id)
            .join(FaceEmbedding, FaceEmbedding.source_motion_event_id == MotionEvent.id)
            .where(FaceEmbedding.face_id == face.id)
            .group_by(Camera.name)
        )).all()
        cam_names = [r[0] for r in cam_rows]
        latest = max((r[1] for r in cam_rows if r[1] is not None), default=None)
        out.append(FaceClusterResponse(
            id=face.id,
            embedding_count=int(cnt),
            sample_embedding_ids=[int(i) for i in sample_ids],
            latest_event_time=latest,
            cameras_seen=cam_names,
            created_at=face.created_at,
        ))
    return out


@router.post("/clusters/{cluster_id}/name", response_model=FaceResponse)
async def name_cluster(
    cluster_id: int, data: FaceClusterNameRequest,
    db: AsyncSession = Depends(get_db),
):
    """Promote an unnamed cluster to a named person."""
    face = await db.get(Face, cluster_id)
    if not face:
        raise HTTPException(status_code=404, detail="Cluster not found")
    if face.name is not None:
        raise HTTPException(status_code=409, detail="Cluster is already named")
    # Name must be unique among named faces.
    clash = await db.execute(
        select(Face).where(Face.name == data.name, Face.id != cluster_id)
    )
    if clash.scalar_one_or_none():
        raise HTTPException(status_code=409, detail="Face name already exists")
    face.name = data.name
    await db.commit()
    await db.refresh(face)
    cnt = (await db.execute(
        select(func.count(FaceEmbedding.id)).where(FaceEmbedding.face_id == cluster_id)
    )).scalar() or 0
    latest = await _latest_crop(db, cluster_id)
    recognizer = get_face_recognizer()
    await recognizer.reload_cache()
    logger.info("Cluster promoted to named face", extra={
        "face_id": cluster_id, "face_name": data.name, "embedding_count": cnt,
    })
    return FaceResponse(
        id=face.id, name=face.name, notes=face.notes,
        embedding_count=cnt, latest_crop_path=latest, created_at=face.created_at,
    )


@router.post("/clusters/{cluster_id}/merge", response_model=FaceResponse)
async def merge_cluster(
    cluster_id: int, data: FaceClusterMergeRequest,
    db: AsyncSession = Depends(get_db),
):
    """Reparent all embeddings of `cluster_id` into `target_face_id`, then delete the source."""
    if cluster_id == data.target_face_id:
        raise HTTPException(status_code=400, detail="Cannot merge a cluster into itself")
    src = await db.get(Face, cluster_id)
    tgt = await db.get(Face, data.target_face_id)
    if not src:
        raise HTTPException(status_code=404, detail="Source cluster not found")
    if not tgt:
        raise HTTPException(status_code=404, detail="Target face not found")
    from sqlalchemy import update as sa_update
    await db.execute(
        sa_update(FaceEmbedding)
        .where(FaceEmbedding.face_id == cluster_id)
        .values(face_id=data.target_face_id)
    )
    # UnclusteredFace.assigned_face_id needs to follow the reparent so history
    # stays coherent (e.g., re-cluster operations depend on it).
    await db.execute(
        sa_update(UnclusteredFace)
        .where(UnclusteredFace.assigned_face_id == cluster_id)
        .values(assigned_face_id=data.target_face_id)
    )
    await db.delete(src)
    await db.commit()
    await db.refresh(tgt)
    cnt = (await db.execute(
        select(func.count(FaceEmbedding.id)).where(FaceEmbedding.face_id == data.target_face_id)
    )).scalar() or 0
    latest = await _latest_crop(db, data.target_face_id)
    recognizer = get_face_recognizer()
    await recognizer.reload_cache()
    logger.info("Cluster merged", extra={
        "source_face_id": cluster_id, "target_face_id": data.target_face_id,
    })
    return FaceResponse(
        id=tgt.id, name=tgt.name, notes=tgt.notes,
        embedding_count=cnt, latest_crop_path=latest, created_at=tgt.created_at,
    )


@router.delete("/clusters/{cluster_id}", status_code=204)
async def discard_cluster(cluster_id: int, db: AsyncSession = Depends(get_db)):
    """Delete an unnamed cluster (user says 'not a real person / noise')."""
    face = await db.get(Face, cluster_id)
    if not face:
        raise HTTPException(status_code=404, detail="Cluster not found")
    if face.name is not None:
        raise HTTPException(status_code=409, detail="Named faces must use DELETE /api/faces/{id}")
    await db.delete(face)
    await db.commit()
    recognizer = get_face_recognizer()
    await recognizer.reload_cache()
    logger.info("Cluster discarded", extra={"face_id": cluster_id})


@router.post("/clusters/recluster", status_code=202)
async def recluster(db: AsyncSession = Depends(get_db)):
    """Delete all auto-clustered embeddings + all unnamed clusters, then
    re-queue every processed UnclusteredFace row so the background worker
    rebuilds clustering state from scratch.

    User-enrolled embeddings on named faces are preserved.
    """
    from sqlalchemy import delete as sa_delete
    from sqlalchemy import update as sa_update
    # Drop auto-clustered embeddings (leaves user-enrolled ones intact).
    await db.execute(
        sa_delete(FaceEmbedding).where(FaceEmbedding.source == "auto_clustered")
    )
    # Drop unnamed Face rows (their embeddings were deleted above or cascade).
    await db.execute(sa_delete(Face).where(Face.name.is_(None)))
    # Re-queue all UnclusteredFace rows: clear processed_at so the worker
    # re-processes them, and clear assigned_face_id since those IDs may be gone.
    await db.execute(
        sa_update(UnclusteredFace).values(processed_at=None, assigned_face_id=None)
    )
    await db.commit()
    recognizer = get_face_recognizer()
    await recognizer.reload_cache()
    # Kick the worker so the user doesn't wait for the next tick.
    from app.services.face_clusterer import get_face_clusterer
    clusterer = get_face_clusterer()
    asyncio.create_task(clusterer.drain_once())
    logger.info("Re-cluster triggered")
    return {"status": "queued"}


@router.get("/{face_id}/embeddings", response_model=list[FaceEmbeddingInfo])
async def list_embeddings(face_id: int, db: AsyncSession = Depends(get_db)):
    trace = BenchmarkTrace(_diag_id("list-emb"), face_id=face_id)  # TEMP FACE-DIAG
    face = await db.get(Face, face_id)
    if not face:
        raise HTTPException(status_code=404, detail="Face not found")
    result = await db.execute(
        select(FaceEmbedding).where(FaceEmbedding.face_id == face_id).order_by(desc(FaceEmbedding.id))
    )
    rows = list(result.scalars().all())  # TEMP FACE-DIAG (was return list(...))
    trace.mark("embeddings_query", count=len(rows))  # TEMP FACE-DIAG
    trace.summary()  # TEMP FACE-DIAG
    return rows


def _save_face_crop(frame: np.ndarray, bbox: tuple[int, int, int, int], trace: BenchmarkTrace | None = None) -> str | None:  # TEMP FACE-DIAG
    """Thin wrapper around services.face_crop.save_face_crop for enrollment trace hooks."""
    path = save_face_crop(frame, bbox)
    if trace is not None and path is not None:  # TEMP FACE-DIAG
        trace.mark("save_crop_done", out_path=path)
    return path


@router.post("/{face_id}/embeddings", response_model=FaceEnrollResponse)
async def enroll_embedding(
    face_id: int, data: FaceEnrollRequest, db: AsyncSession = Depends(get_db),
):
    """Run SCRFD+ArcFace on a thumbnail, store the matching face embedding."""
    # TEMP FACE-DIAG: end-to-end trace for enrollment
    trace = BenchmarkTrace(_diag_id("enroll"), face_id=face_id, has_bbox=data.bbox is not None)
    face = await db.get(Face, face_id)
    trace.mark("face_lookup", found=bool(face))
    if not face:
        raise HTTPException(status_code=404, detail="Face not found")

    recognizer = get_face_recognizer()
    if not recognizer.available:
        raise HTTPException(status_code=503, detail="Face recognizer not ready")
    trace.mark("recognizer_ready")

    src = Path(data.source_thumbnail_path)
    if not src.exists():
        raise HTTPException(status_code=404, detail="Source image not found")

    frame = cv2.imread(str(src))
    if frame is None:
        raise HTTPException(status_code=400, detail="Could not read source image")
    trace.mark("imread", path=str(src), shape=f"{frame.shape[1]}x{frame.shape[0]}", bytes=src.stat().st_size)  # TEMP FACE-DIAG

    # Mirror the live pipeline's two-stage approach: full-frame SCRFD first
    # (catches close-up or obvious faces), then fall back to running SCRFD
    # inside each RT-DETR person bbox. Without the crop step, tiny faces in
    # high-res thumbnails (e.g. a person at the far end of a 4K frame) get
    # downscaled into oblivion when SCRFD letterboxes to 640×640 and are
    # missed entirely.
    hits = await recognizer.detect_and_embed(frame, person_bbox=None)
    trace.mark("scrfd_fullframe", hits=len(hits))  # TEMP FACE-DIAG
    if not hits:
        detector = get_object_detector()
        if detector and detector._started:
            persons = await detector.detect_objects(
                frame, 0.5, classes=build_class_list(True, False, False),
            )
            trace.mark("rtdetr_persons", count=len(persons))  # TEMP FACE-DIAG
            seen_boxes: list[tuple[int, int, int, int]] = []
            for idx, p in enumerate(persons):  # TEMP FACE-DIAG: idx
                bbox = (p.x1, p.y1, p.x2, p.y2)
                crop_hits = await recognizer.detect_and_embed(frame, person_bbox=bbox)
                trace.mark(f"scrfd_person_{idx}", bbox=f"{p.x1},{p.y1},{p.x2},{p.y2}", hits=len(crop_hits))  # TEMP FACE-DIAG
                for h in crop_hits:
                    # Dedupe: skip faces that overlap ones we've already kept
                    key = (h.x1, h.y1, h.x2, h.y2)
                    if any(abs(k[0] - key[0]) < 20 and abs(k[1] - key[1]) < 20
                           for k in seen_boxes):
                        continue
                    seen_boxes.append(key)
                    hits.append(h)
        else:
            trace.mark("rtdetr_unavailable")  # TEMP FACE-DIAG
    if not hits:
        trace.mark("no_face_return")  # TEMP FACE-DIAG
        trace.summary()  # TEMP FACE-DIAG
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
        trace.mark("multiple_faces_return", candidates=len(hits))  # TEMP FACE-DIAG
        trace.summary()  # TEMP FACE-DIAG
        return FaceEnrollResponse(
            status="multiple_faces",
            candidates=[
                FaceEnrollCandidate(bbox=[h.x1, h.y1, h.x2, h.y2], score=h.score)
                for h in hits
            ],
        )

    if chosen is None:
        trace.mark("no_face_after_pick")  # TEMP FACE-DIAG
        trace.summary()  # TEMP FACE-DIAG
        return FaceEnrollResponse(status="no_face")
    trace.mark("face_chosen", score=chosen.score)  # TEMP FACE-DIAG

    crop_path = _save_face_crop(frame, (chosen.x1, chosen.y1, chosen.x2, chosen.y2), trace=trace)  # TEMP FACE-DIAG trace arg
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
    trace.mark("db_commit", embedding_id=emb.id)  # TEMP FACE-DIAG
    await recognizer.reload_cache()
    trace.mark("cache_reload")  # TEMP FACE-DIAG
    logger.info("Face embedding enrolled", extra={
        "face_id": face_id, "embedding_id": emb.id, "score": chosen.score,
    })
    trace.summary()  # TEMP FACE-DIAG
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
    trace = BenchmarkTrace(_diag_id("unlabeled"), date=date, camera_id=camera_id, limit=limit, with_face_only=with_face_only)  # TEMP FACE-DIAG
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
    trace.mark("motion_query", event_count=len(events))  # TEMP FACE-DIAG

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
    trace.mark("assigned_lookup", assigned_count=len(assigned), thumb_paths=len(thumb_paths))  # TEMP FACE-DIAG

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
    trace.mark("build_response", returned=len(out))  # TEMP FACE-DIAG
    trace.summary()  # TEMP FACE-DIAG
    return out


@router.get("/thumbnails/event/{event_id}/path")
async def resolve_event_thumbnail_path(event_id: int, db: AsyncSession = Depends(get_db)):
    """Resolve a motion-event id to the on-disk thumbnail path (used for enrollment)."""
    trace = BenchmarkTrace(_diag_id("resolve"), event_id=event_id)  # TEMP FACE-DIAG
    event = await db.get(MotionEvent, event_id)
    if not event or not event.thumbnail_path:
        trace.mark("not_found")  # TEMP FACE-DIAG
        trace.summary()  # TEMP FACE-DIAG
        raise HTTPException(status_code=404, detail="Thumbnail not found")
    # TEMP FACE-DIAG: verify file actually exists on disk + report size
    p = Path(event.thumbnail_path)
    trace.mark("resolved", path=event.thumbnail_path, exists=p.exists(), bytes=(p.stat().st_size if p.exists() else -1))
    trace.summary()  # TEMP FACE-DIAG
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
