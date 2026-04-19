"""Shared face-crop save utility.

Used both by the enrollment router (when a user tags a thumbnail) and by the
motion pipeline / face clusterer (when queuing unknown faces). The crop is a
192x192 square JPEG with 50% margin around the SCRFD bbox — suitable for UI
avatars and for re-detection during enrollment disambiguation.
"""

import logging
from datetime import datetime
from pathlib import Path

import cv2
import numpy as np

logger = logging.getLogger(__name__)

FACE_CROP_MARGIN = 0.5
FACE_CROP_OUTPUT = 192


def save_face_crop(frame: np.ndarray, bbox: tuple[int, int, int, int]) -> str | None:
    """Save a padded, square face thumbnail. Returns path or None on failure."""
    try:
        from app.config import get_config
        config = get_config()
        thumb_root = Path(config.storage.thumbnails_dir)
        face_dir = thumb_root / "_faces"
        face_dir.mkdir(parents=True, exist_ok=True)
        x1, y1, x2, y2 = bbox
        h, w = frame.shape[:2]

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

        ch, cw = crop.shape[:2]
        interp = cv2.INTER_CUBIC if max(ch, cw) < FACE_CROP_OUTPUT else cv2.INTER_AREA
        out = cv2.resize(crop, (FACE_CROP_OUTPUT, FACE_CROP_OUTPUT), interpolation=interp)

        filename = f"face_{datetime.now().strftime('%Y%m%d_%H%M%S_%f')}.jpg"
        path = face_dir / filename
        cv2.imwrite(str(path), out, [cv2.IMWRITE_JPEG_QUALITY, 92])
        return str(path)
    except Exception:
        logger.exception("Failed to save face crop")
        return None
