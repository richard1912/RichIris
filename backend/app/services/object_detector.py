"""ONNX Runtime-based object detector for AI detection on motion frames.

Uses RT-DETR-L (Real-Time Detection Transformer) exported to ONNX format.
Transformer-based: NMS-free, fewer false positives on ambiguous shapes.
Runs on GPU via DirectML (any GPU, no CUDA required) with CPU fallback.
"""

import asyncio
import logging
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path

import cv2
import numpy as np

logger = logging.getLogger(__name__)

# Minimum bounding box area as fraction of frame area.
# Filters out tiny false-positive detections (shadows, artifacts).
MIN_BOX_AREA_FRACTION = 0.002  # 0.2% of frame

# Model input size (must match export: imgsz=640)
INPUT_SIZE = 640

# COCO class IDs grouped by detection category
CATEGORY_CLASSES = {
    "person": [0],
    "vehicle": [1, 2, 3, 5, 7],       # bicycle, car, motorcycle, bus, truck
    "animal": [14, 15, 16, 17, 18, 19, 20, 21, 22, 23],  # bird-giraffe
}

# Reverse lookup: COCO class ID → human-readable label
COCO_CLASS_NAMES = {
    0: "person",
    1: "bicycle", 2: "car", 3: "motorcycle", 5: "bus", 7: "truck",
    14: "bird", 15: "cat", 16: "dog", 17: "horse",
    18: "sheep", 19: "cow", 20: "elephant", 21: "bear",
    22: "zebra", 23: "giraffe",
}

# All class IDs we care about (for filtering output)
ALL_DETECTION_CLASSES = set()
for ids in CATEGORY_CLASSES.values():
    ALL_DETECTION_CLASSES.update(ids)


def build_class_list(detect_persons: bool, detect_vehicles: bool, detect_animals: bool) -> list[int]:
    """Build a flat list of COCO class IDs from category flags."""
    classes: list[int] = []
    if detect_persons:
        classes.extend(CATEGORY_CLASSES["person"])
    if detect_vehicles:
        classes.extend(CATEGORY_CLASSES["vehicle"])
    if detect_animals:
        classes.extend(CATEGORY_CLASSES["animal"])
    return classes


@dataclass
class Detection:
    label: str
    confidence: float
    x1: int
    y1: int
    x2: int
    y2: int


def _preprocess(frame: np.ndarray) -> tuple[np.ndarray, float, int, int]:
    """Resize frame to 640x640 with letterboxing, normalize to [0,1] NCHW."""
    h, w = frame.shape[:2]
    scale = min(INPUT_SIZE / w, INPUT_SIZE / h)
    new_w, new_h = int(w * scale), int(h * scale)
    pad_x, pad_y = (INPUT_SIZE - new_w) // 2, (INPUT_SIZE - new_h) // 2

    resized = cv2.resize(frame, (new_w, new_h), interpolation=cv2.INTER_LINEAR)
    canvas = np.full((INPUT_SIZE, INPUT_SIZE, 3), 114, dtype=np.uint8)
    canvas[pad_y:pad_y + new_h, pad_x:pad_x + new_w] = resized

    # HWC BGR → CHW RGB, float32 [0,1]
    blob = canvas[:, :, ::-1].transpose(2, 0, 1).astype(np.float32) / 255.0
    return np.expand_dims(blob, 0), scale, pad_x, pad_y


def _postprocess(
    output: np.ndarray,
    scale: float, pad_x: int, pad_y: int,
    frame_h: int, frame_w: int,
    confidence_threshold: float,
    classes: set[int] | None,
) -> list[Detection]:
    """Parse RT-DETR output [1, 300, 84] → list of Detection.

    RT-DETR output: 300 queries, each [cx, cy, w, h, class_scores×80].
    Coordinates are normalized [0,1]. NMS-free — transformer deduplicates.
    """
    preds = output[0]  # (300, 84) — no transpose needed

    # Extract boxes (normalized cx, cy, w, h) and class scores
    boxes_norm = preds[:, :4]
    scores_all = preds[:, 4:]  # (300, 80)

    # Get best class per query
    class_ids = np.argmax(scores_all, axis=1)
    confidences = scores_all[np.arange(len(class_ids)), class_ids]

    # Filter by confidence and class
    mask = confidences >= confidence_threshold
    if classes:
        mask &= np.isin(class_ids, list(classes))

    indices = np.where(mask)[0]
    if len(indices) == 0:
        return []

    boxes_norm = boxes_norm[indices]
    class_ids = class_ids[indices]
    confidences = confidences[indices]

    # Normalized cx,cy,w,h → pixel x1,y1,x2,y2 in 640×640 space
    cx = boxes_norm[:, 0] * INPUT_SIZE
    cy = boxes_norm[:, 1] * INPUT_SIZE
    w = boxes_norm[:, 2] * INPUT_SIZE
    h = boxes_norm[:, 3] * INPUT_SIZE
    x1 = cx - w / 2
    y1 = cy - h / 2
    x2 = cx + w / 2
    y2 = cy + h / 2

    # Undo letterbox: remove padding, rescale to original image
    x1 = (x1 - pad_x) / scale
    y1 = (y1 - pad_y) / scale
    x2 = (x2 - pad_x) / scale
    y2 = (y2 - pad_y) / scale

    # Clip to frame bounds
    x1 = np.clip(x1, 0, frame_w).astype(np.int32)
    y1 = np.clip(y1, 0, frame_h).astype(np.int32)
    x2 = np.clip(x2, 0, frame_w).astype(np.int32)
    y2 = np.clip(y2, 0, frame_h).astype(np.int32)

    # Filter by minimum box area (no NMS needed — RT-DETR is NMS-free)
    frame_area = frame_h * frame_w
    min_box_area = frame_area * MIN_BOX_AREA_FRACTION

    detections = []
    for i in range(len(indices)):
        bx1, by1, bx2, by2 = int(x1[i]), int(y1[i]), int(x2[i]), int(y2[i])
        if (bx2 - bx1) * (by2 - by1) < min_box_area:
            continue
        cls_id = int(class_ids[i])
        label = COCO_CLASS_NAMES.get(cls_id, f"class_{cls_id}")
        detections.append(Detection(
            label=label,
            confidence=float(confidences[i]),
            x1=bx1, y1=by1, x2=bx2, y2=by2,
        ))

    return detections


# Model filenames to search for, in priority order
_MODEL_FILENAMES = ["rtdetr-l.onnx", "yolo11x.onnx"]


class ObjectDetector:
    """Singleton ONNX Runtime-based object detector shared across all cameras.

    Uses DirectML (GPU) with CPU fallback. A ThreadPoolExecutor serializes
    inference so multiple camera loops don't contend on the GPU.
    """

    def __init__(self):
        self._session = None
        self._input_name = None
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="detector")
        self._started = False
        self._provider = "cpu"

    async def start(self) -> None:
        """Load the ONNX model."""
        if self._started:
            return

        loop = asyncio.get_event_loop()
        await loop.run_in_executor(self._executor, self._load_model)
        self._started = True

    def _load_model(self) -> None:
        """Blocking model load (runs in executor)."""
        import onnxruntime as ort

        from app.config import get_app_dir, get_bootstrap
        data_dir = Path(get_bootstrap().data_dir)
        app_dir = get_app_dir()

        # Search for any supported model file
        model_path = None
        for filename in _MODEL_FILENAMES:
            for directory in [data_dir, app_dir / "dependencies" / "models"]:
                p = directory / filename
                if p.exists():
                    model_path = p
                    break
            if model_path:
                break

        if model_path is None:
            logger.error("Detection ONNX model not found", extra={
                "searched_filenames": _MODEL_FILENAMES,
            })
            return

        # Try DirectML (GPU) first, then CPU
        providers_to_try = [
            (["DmlExecutionProvider", "CPUExecutionProvider"], "DirectML"),
            (["CUDAExecutionProvider", "CPUExecutionProvider"], "CUDA"),
            (["CPUExecutionProvider"], "CPU"),
        ]

        for providers, label in providers_to_try:
            try:
                self._session = ort.InferenceSession(str(model_path), providers=providers)
                active = self._session.get_providers()
                self._provider = active[0] if active else "unknown"
                self._input_name = self._session.get_inputs()[0].name

                # Warmup inference
                dummy = np.random.rand(1, 3, INPUT_SIZE, INPUT_SIZE).astype(np.float32)
                self._session.run(None, {self._input_name: dummy})

                logger.info("Detection model loaded", extra={
                    "model": model_path.name,
                    "provider": self._provider,
                    "attempted": label,
                })
                return
            except Exception:
                logger.debug("Provider %s not available, trying next", label)
                continue

        logger.error("Failed to load detection model with any provider")

    async def stop(self) -> None:
        """Release model and executor."""
        self._session = None
        self._started = False
        self._executor.shutdown(wait=False)
        logger.info("Object detector stopped")

    async def detect_objects(
        self, frame: np.ndarray, confidence_threshold: float = 0.5,
        classes: list[int] | None = None,
    ) -> list[Detection]:
        """Run object detection on a frame. Returns detections above threshold."""
        if not self._started or self._session is None:
            return []

        from app.services._onnx_lock import get_onnx_lock
        loop = asyncio.get_event_loop()
        async with get_onnx_lock():
            return await loop.run_in_executor(
                self._executor,
                self._run_inference, frame, confidence_threshold, classes,
            )

    def _run_inference(
        self, frame: np.ndarray, threshold: float, classes: list[int] | None,
    ) -> list[Detection]:
        """Blocking ONNX inference (runs in executor)."""
        h, w = frame.shape[:2]
        blob, scale, pad_x, pad_y = _preprocess(frame)

        try:
            outputs = self._session.run(None, {self._input_name: blob})
        except Exception:
            logger.exception("ONNX inference failed")
            return []

        class_set = set(classes) if classes else ALL_DETECTION_CLASSES
        return _postprocess(
            outputs[0], scale, pad_x, pad_y,
            h, w, threshold, class_set,
        )


_detector: ObjectDetector | None = None


def get_object_detector() -> ObjectDetector:
    """Get or create the singleton object detector."""
    global _detector
    if _detector is None:
        _detector = ObjectDetector()
    return _detector
