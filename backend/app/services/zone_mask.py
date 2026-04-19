"""Polygon zone mask cache for per-script motion/detection filtering.

Zones are stored as normalized [0,1] polygons so they survive stream resolution
changes. On first use at a given (zone, shape), we rasterize once with
cv2.fillPoly and cache the uint8 mask. Union masks for scripts that reference
multiple zones are also cached keyed by sorted tuple(zone_ids).

Zone CRUD calls invalidate(zone_id) to drop cached entries, which is cheap —
the next detector frame rebuilds whatever it needs.
"""

import json
import logging

import cv2
import numpy as np

from app.database import get_session_factory

logger = logging.getLogger(__name__)


class ZoneMaskCache:
    def __init__(self):
        # Normalized polygon points keyed by zone_id
        self._points: dict[int, list[tuple[float, float]]] = {}
        # Rasterized single-zone masks keyed by (zone_id, (h, w))
        self._masks: dict[tuple[int, tuple[int, int]], np.ndarray] = {}
        # Union masks keyed by (tuple(sorted(zone_ids)), (h, w))
        self._union_masks: dict[tuple[tuple[int, ...], tuple[int, int]], np.ndarray | None] = {}

    async def _load_points(self, zone_id: int) -> list[tuple[float, float]] | None:
        """Load and cache normalized points for a zone from DB. Returns None if missing."""
        from app.models import Zone as ZoneModel
        factory = get_session_factory()
        async with factory() as session:
            zone = await session.get(ZoneModel, zone_id)
            if zone is None:
                return None
            try:
                raw = json.loads(zone.points_json)
            except (json.JSONDecodeError, TypeError):
                return None
        points = [
            (float(p[0]), float(p[1])) for p in raw if isinstance(p, (list, tuple)) and len(p) >= 2
        ]
        return points if len(points) >= 3 else None

    async def get_mask(self, zone_id: int, shape: tuple[int, int]) -> np.ndarray | None:
        """Return a uint8 0/255 mask for zone at (h, w), rasterizing on first use."""
        cache_key = (zone_id, shape)
        mask = self._masks.get(cache_key)
        if mask is not None:
            return mask
        points = self._points.get(zone_id)
        if points is None:
            points = await self._load_points(zone_id)
            if points is None:
                return None
            self._points[zone_id] = points
        h, w = shape
        pts_abs = np.array(
            [[int(round(x * w)), int(round(y * h))] for x, y in points],
            dtype=np.int32,
        )
        mask = np.zeros((h, w), dtype=np.uint8)
        cv2.fillPoly(mask, [pts_abs], 255)
        self._masks[cache_key] = mask
        return mask

    async def get_union_mask(
        self, zone_ids: list[int], shape: tuple[int, int]
    ) -> np.ndarray | None:
        """Return OR-combined mask for the given zones at (h, w). None if no zones resolve."""
        if not zone_ids:
            return None
        key = (tuple(sorted(set(zone_ids))), shape)
        if key in self._union_masks:
            return self._union_masks[key]
        union: np.ndarray | None = None
        for zid in key[0]:
            m = await self.get_mask(zid, shape)
            if m is None:
                continue
            if union is None:
                union = m.copy()
            else:
                union = cv2.bitwise_or(union, m)
        self._union_masks[key] = union
        return union

    def invalidate(self, zone_id: int) -> None:
        """Drop all cached entries referencing the given zone."""
        self._points.pop(zone_id, None)
        for k in list(self._masks):
            if k[0] == zone_id:
                del self._masks[k]
        # Union cache: wipe entirely — cheap to rebuild and guarantees correctness.
        self._union_masks.clear()

    @staticmethod
    def bbox_in_mask(
        bbox: tuple[int, int, int, int], union_mask: np.ndarray
    ) -> bool:
        """True if the bbox's bottom-center pixel falls inside the mask.

        Bottom-center = where a person's feet / a vehicle's wheels meet the
        ground, which is the natural anchor for "is this object in the zone".
        """
        x1, y1, x2, y2 = bbox
        h, w = union_mask.shape[:2]
        cx = int((x1 + x2) / 2.0)
        cy = int(y2)
        if cx < 0 or cy < 0 or cx >= w or cy >= h:
            return False
        return bool(union_mask[cy, cx])

    @staticmethod
    def motion_in_mask(
        motion_thresh: np.ndarray, union_mask: np.ndarray, sensitivity_pct: float
    ) -> bool:
        """True if motion pixels inside the zone exceed sensitivity_pct of zone area."""
        if motion_thresh.shape[:2] != union_mask.shape[:2]:
            return False
        zone_size = int(cv2.countNonZero(union_mask))
        if zone_size == 0:
            return False
        intersect = cv2.bitwise_and(motion_thresh, union_mask)
        changed = int(cv2.countNonZero(intersect))
        return (changed / zone_size) * 100.0 >= sensitivity_pct


_cache: ZoneMaskCache | None = None


def get_zone_mask_cache() -> ZoneMaskCache:
    global _cache
    if _cache is None:
        _cache = ZoneMaskCache()
    return _cache
