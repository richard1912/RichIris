"""Two-tier storage flusher: moves finalized HOT segments to the ARCHIVE tier.

The recorder always writes to the HOT tier (``config.storage.recordings_dir``,
which lives under ``data_dir`` — a fast SSD when the user points data_dir there).
This background flusher copies finalized segments older than
``hot_retention_minutes`` (or sooner under ``hot_max_gb`` backpressure) to the
ARCHIVE tier (``config.storage.archive_recordings_dir`` — a large HDD) and
rewrites the segment's ``file_path`` + ``tier`` in a single DB transaction, the
same way the recorder rewrites ``file_path`` on rename. Because reads always use
the absolute ``file_path``, playback/clips/retention need no changes.

Resilience (the whole point, given the failing-drive history):
- The live recorder/detection path never awaits archive I/O.
- If the archive drive is offline/slow, the flush enters degraded mode: it logs
  a warning, leaves segments on HOT, and retries next cycle. Recording continues.
- Crash-safe / idempotent: copy → verify size → atomic replace → commit (point of
  no return) → delete source. A crash at any point loses no segment; at worst a
  stale ``.flushtmp`` (cleaned on startup) or a re-copyable HOT orphan remains.
"""

import asyncio
import logging
import os
import shutil
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_config
from app.database import get_session_factory
from app.models import Recording

logger = logging.getLogger(__name__)

_GIB = 1_073_741_824
_TMP_SUFFIX = ".flushtmp"
MAX_PER_CYCLE = 50  # cap segments moved per cycle so a backlog drains smoothly


@dataclass
class FlushState:
    """Live status for the UI status panel."""
    last_flush_at: float | None = None   # epoch seconds of last successful move
    last_flushed_count: int = 0          # segments moved in the most recent cycle
    degraded: bool = False               # archive drive unreachable/slow last cycle
    last_error: str = ""


class StorageFlusher:
    def __init__(self) -> None:
        self.state = FlushState()
        self._lock = asyncio.Lock()

    def _two_tier_active(self) -> tuple[bool, str, str]:
        """Return (active, hot_recordings_dir, archive_recordings_dir)."""
        cfg = get_config().storage
        hot = cfg.recordings_dir
        archive = cfg.archive_recordings_dir
        active = bool(cfg.two_tier_enabled and archive and archive != hot)
        return active, hot, archive

    async def _select_candidates(self, session: AsyncSession, hot: str) -> list[Recording]:
        """Oldest-first finalized HOT segments eligible for flush.

        Eligible = past the retention window, plus (under hot_max_gb backpressure)
        additional oldest segments until projected HOT usage is back under the cap.
        """
        cfg = get_config().storage
        cutoff = datetime.now() - timedelta(minutes=cfg.hot_retention_minutes)

        result = await session.execute(
            select(Recording)
            .where(
                Recording.in_progress == False,  # noqa: E712
                Recording.tier == "hot",
            )
            .order_by(Recording.start_time.asc())
        )
        hot_recs = list(result.scalars().all())
        if not hot_recs:
            return []

        chosen: list[Recording] = []
        chosen_ids: set[int] = set()

        # Age-eligible
        for rec in hot_recs:
            end = rec.end_time or rec.start_time
            if end < cutoff:
                chosen.append(rec)
                chosen_ids.add(rec.id)

        # Backpressure: if total HOT bytes exceed the cap, flush more oldest-first
        if cfg.hot_max_gb and cfg.hot_max_gb > 0:
            cap = cfg.hot_max_gb * _GIB
            total_hot = sum((r.file_size or 0) for r in hot_recs)
            projected = total_hot - sum((r.file_size or 0) for r in chosen)
            for rec in hot_recs:
                if projected <= cap:
                    break
                if rec.id not in chosen_ids:
                    chosen.append(rec)
                    chosen_ids.add(rec.id)
                    projected -= (rec.file_size or 0)

        # Keep global oldest-first order and cap per cycle
        chosen.sort(key=lambda r: r.start_time)
        return chosen[:MAX_PER_CYCLE]

    async def flush_once(self) -> int:
        """Run a single flush pass. Returns the number of segments moved."""
        active, hot, archive = self._two_tier_active()
        if not active:
            return 0

        async with self._lock:
            factory = get_session_factory()
            moved = 0
            loop = asyncio.get_event_loop()
            hot_root = Path(hot)
            archive_root = Path(archive)

            async with factory() as session:
                candidates = await self._select_candidates(session, hot)

                for rec in candidates:
                    src = Path(rec.file_path)
                    if not src.exists():
                        continue  # orphan; cleanup_missing_recordings handles the row

                    # Mirror the HOT-relative path under the archive root.
                    try:
                        rel = src.relative_to(hot_root)
                    except ValueError:
                        # Not under the hot recordings dir (custom/legacy path) — skip.
                        continue
                    dest = archive_root / rel
                    tmp = dest.with_name(dest.name + _TMP_SUFFIX)

                    try:
                        src_size = src.stat().st_size
                        # All archive I/O is guarded — failure → degraded mode.
                        await loop.run_in_executor(
                            None, lambda: dest.parent.mkdir(parents=True, exist_ok=True)
                        )
                        await loop.run_in_executor(None, shutil.copy2, str(src), str(tmp))
                        if tmp.stat().st_size != src_size:
                            tmp.unlink(missing_ok=True)
                            logger.warning(
                                "Flush size mismatch, will retry",
                                extra={"src": str(src), "expected": src_size},
                            )
                            continue
                        # Atomic publish on the archive volume.
                        await loop.run_in_executor(None, os.replace, str(tmp), str(dest))
                    except OSError as e:
                        # Archive drive offline/slow → degraded mode. Leave on HOT.
                        try:
                            tmp.unlink(missing_ok=True)
                        except OSError:
                            pass
                        self.state.degraded = True
                        self.state.last_error = str(e)
                        logger.warning(
                            "Archive flush failed (degraded mode); recording continues on HOT",
                            extra={"src": str(src), "archive": str(archive_root), "error": str(e)},
                        )
                        break  # stop this cycle; retry next cycle with backoff

                    # Point of no return: DB now authoritative for the archive copy.
                    rec.file_path = str(dest)
                    rec.tier = "archive"
                    await session.commit()

                    # Source is now an unreferenced duplicate — safe to remove.
                    try:
                        src.unlink()
                    except OSError:
                        logger.debug("Could not remove HOT source after flush", extra={"src": str(src)})

                    moved += 1

            if moved:
                import time
                self.state.last_flush_at = time.time()
                self.state.degraded = False
                self.state.last_error = ""
                logger.info("Flushed segments HOT→archive", extra={"count": moved})
            self.state.last_flushed_count = moved
            return moved

    def cleanup_stray_tmp(self) -> None:
        """Remove leftover .flushtmp files from an interrupted flush (startup)."""
        active, _hot, archive = self._two_tier_active()
        if not active:
            return
        root = Path(archive)
        if not root.exists():
            return
        removed = 0
        try:
            for p in root.rglob(f"*{_TMP_SUFFIX}"):
                try:
                    p.unlink()
                    removed += 1
                except OSError:
                    pass
        except OSError:
            return
        if removed:
            logger.info("Cleaned stray flush temp files", extra={"count": removed})


_flusher: StorageFlusher | None = None


def get_storage_flusher() -> StorageFlusher:
    global _flusher
    if _flusher is None:
        _flusher = StorageFlusher()
    return _flusher
