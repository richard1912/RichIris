"""Lightweight benchmark trace for cross-tier playback timing.

The frontend sends an `X-Bench-Id` header on the playback POST. Backend phases
are logged with that id so the dev can grep both client (`[BENCH:<id>]`) and
server (`bench_id=<id>`) logs to reconstruct the full pipeline.
"""

from __future__ import annotations

import logging
import time
from typing import Any

logger = logging.getLogger(__name__)


class BenchmarkTrace:
    """Records phase timings against a shared bench_id from the client."""

    def __init__(self, bench_id: str | None, **context: Any) -> None:
        self.bench_id = bench_id or "-"
        self.context = context
        self.start = time.monotonic()
        self.last = self.start
        self.phases: list[tuple[str, int]] = []

    def mark(self, phase: str, **extra: Any) -> None:
        now = time.monotonic()
        delta_ms = int((now - self.last) * 1000)
        total_ms = int((now - self.start) * 1000)
        self.last = now
        self.phases.append((phase, total_ms))
        logger.info(
            "[BENCH:%s] %s +%dms total=%dms",
            self.bench_id,
            phase,
            delta_ms,
            total_ms,
            extra={
                "bench_id": self.bench_id,
                "phase": phase,
                "delta_ms": delta_ms,
                "total_ms": total_ms,
                **self.context,
                **extra,
            },
        )

    def summary(self) -> None:
        total_ms = int((time.monotonic() - self.start) * 1000)
        logger.info(
            "[BENCH:%s] backend_summary total=%dms phases=%d",
            self.bench_id,
            total_ms,
            len(self.phases),
            extra={
                "bench_id": self.bench_id,
                "total_ms": total_ms,
                "phases": [{"name": n, "total_ms": t} for n, t in self.phases],
                **self.context,
            },
        )
