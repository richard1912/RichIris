"""Background face clusterer.

Drains pending rows from the `unclustered_faces` queue (written by the motion
pipeline when SCRFD+ArcFace finds a face that doesn't match any named person
at the strict threshold). For each row, runs a greedy nearest-neighbor lookup
across **all** Face rows (named + unnamed) at a looser cosine threshold. If a
match is found, the embedding is attached to that face; else a new unnamed
Face is created (an Immich-style "suggested person"). The user can then name
the cluster from the Faces UI, merge clusters together, or discard noise.

Matches Immich's `handleRecognizeFaces` algorithm: greedy, no DBSCAN. Simple,
robust, and trivially explainable to users.
"""

import asyncio
import logging
from datetime import datetime

import numpy as np
from sqlalchemy import func, select, update

from app.database import get_session_factory
from app.models import Face, FaceEmbedding, UnclusteredFace
from app.services.face_recognizer import get_face_recognizer

logger = logging.getLogger(__name__)

# Tuning defaults — can be lifted into the settings table later if needed.
CLUSTER_INTERVAL_SECONDS = 60
CLUSTER_BATCH_SIZE = 50
# Looser than the live match threshold (default 0.60): clusters should
# aggregate aggressively because false merges are easily corrected by the user.
CLUSTER_THRESHOLD = 0.55
# Cap embeddings stored per auto-clustered face so a busy cluster doesn't
# bloat the DB. Beyond the cap we count the hit but don't store the vector.
CLUSTER_MAX_EMBEDDINGS = 30


class FaceClusterer:
    def __init__(self) -> None:
        self._task: asyncio.Task | None = None
        self._running = False

    def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._task = asyncio.create_task(self._loop())
        logger.info("Face clusterer started", extra={
            "interval_s": CLUSTER_INTERVAL_SECONDS,
            "threshold": CLUSTER_THRESHOLD,
            "max_embeddings": CLUSTER_MAX_EMBEDDINGS,
        })

    async def stop(self) -> None:
        self._running = False
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
            self._task = None

    async def _loop(self) -> None:
        # Initial delay — let other startup tasks settle (recognizer cache load,
        # camera warmup) before we do any DB work.
        await asyncio.sleep(10)
        while self._running:
            try:
                await self.drain_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Face clusterer loop iteration failed")
            await asyncio.sleep(CLUSTER_INTERVAL_SECONDS)

    async def drain_once(self, limit: int = CLUSTER_BATCH_SIZE) -> int:
        """Process up to `limit` pending rows. Returns how many were processed."""
        recognizer = get_face_recognizer()
        if not recognizer.available:
            return 0

        factory = get_session_factory()
        async with factory() as session:
            result = await session.execute(
                select(UnclusteredFace)
                .where(UnclusteredFace.processed_at.is_(None))
                .order_by(UnclusteredFace.id.asc())
                .limit(limit)
            )
            pending = list(result.scalars().all())

        if not pending:
            return 0

        processed = 0
        created = 0
        attached = 0
        for row in pending:
            try:
                ok = await self._process_row(row)
                if ok:
                    processed += 1
                    if row.assigned_face_id is None:
                        pass  # shouldn't happen after _process_row
            except Exception:
                logger.exception("Face clusterer: failed to process row", extra={
                    "unclustered_face_id": row.id,
                })

        # _process_row calls reload_cache if a new Face was created, so the
        # in-memory matcher stays fresh for subsequent rows in this batch.
        logger.info("Face clusterer drained batch", extra={
            "pending": len(pending), "processed": processed,
        })
        return processed

    async def _process_row(self, row: UnclusteredFace) -> bool:
        """Greedy nearest-neighbor on one row. Returns True if processed."""
        recognizer = get_face_recognizer()
        emb = np.frombuffer(row.embedding, dtype=np.float32)
        if emb.shape[0] != 512:
            await self._mark_processed(row.id, assigned_face_id=None)
            return True

        # Nearest match at the clustering threshold — includes both named and
        # nameless (cluster) faces because the matcher cache holds every row.
        match = recognizer.match(emb, CLUSTER_THRESHOLD)

        factory = get_session_factory()
        async with factory() as session:
            if match is not None:
                # Attach to existing face (named or unnamed) — but first check
                # the per-cluster embedding cap so we don't bloat a busy cluster.
                count_res = await session.execute(
                    select(func.count(FaceEmbedding.id))
                    .where(FaceEmbedding.face_id == match.face_id)
                )
                count = int(count_res.scalar() or 0)
                if count < CLUSTER_MAX_EMBEDDINGS:
                    session.add(FaceEmbedding(
                        face_id=match.face_id,
                        embedding=row.embedding,
                        face_crop_path=row.face_crop_path,
                        source="auto_clustered",
                        detection_score=row.detection_score,
                        source_motion_event_id=row.motion_event_id,
                    ))
                await session.execute(
                    update(UnclusteredFace)
                    .where(UnclusteredFace.id == row.id)
                    .values(processed_at=datetime.now(), assigned_face_id=match.face_id)
                )
                await session.commit()
                if count < CLUSTER_MAX_EMBEDDINGS:
                    await recognizer.reload_cache()
                return True

            # No match — create a new unnamed Face (cluster seed).
            new_face = Face(name=None, notes=None)
            session.add(new_face)
            await session.flush()
            new_face_id = new_face.id
            session.add(FaceEmbedding(
                face_id=new_face_id,
                embedding=row.embedding,
                face_crop_path=row.face_crop_path,
                source="auto_clustered",
                detection_score=row.detection_score,
                source_motion_event_id=row.motion_event_id,
            ))
            await session.execute(
                update(UnclusteredFace)
                .where(UnclusteredFace.id == row.id)
                .values(processed_at=datetime.now(), assigned_face_id=new_face_id)
            )
            await session.commit()

        # Reload cache so the next row in this batch can match this new cluster.
        await recognizer.reload_cache()
        return True

    async def _mark_processed(self, row_id: int, assigned_face_id: int | None) -> None:
        factory = get_session_factory()
        async with factory() as session:
            await session.execute(
                update(UnclusteredFace)
                .where(UnclusteredFace.id == row_id)
                .values(processed_at=datetime.now(), assigned_face_id=assigned_face_id)
            )
            await session.commit()


_clusterer: FaceClusterer | None = None


def get_face_clusterer() -> FaceClusterer:
    global _clusterer
    if _clusterer is None:
        _clusterer = FaceClusterer()
    return _clusterer
