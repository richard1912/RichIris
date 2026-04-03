"""YOLO-based object detector for AI detection on motion frames."""

import asyncio
import logging
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path

import numpy as np

logger = logging.getLogger(__name__)

# Minimum bounding box area as fraction of frame area.
# Filters out tiny false-positive detections (shadows, artifacts).
MIN_BOX_AREA_FRACTION = 0.002  # 0.2% of frame

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


class ObjectDetector:
    """Singleton YOLO-based object detector shared across all cameras.

    Uses an asyncio.Queue so multiple camera loops can submit frames
    for inference without contending on the GPU. A single consumer
    coroutine pulls frames and runs blocking inference in a thread.
    """

    def __init__(self):
        self._model = None
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="yolo")
        self._started = False
        self._device = "cpu"

    async def start(self) -> None:
        """Load the YOLO model onto GPU (or CPU fallback)."""
        if self._started:
            return

        loop = asyncio.get_event_loop()
        await loop.run_in_executor(self._executor, self._load_model)
        self._started = True

    def _load_model(self) -> None:
        """Blocking model load (runs in executor)."""
        from ultralytics import YOLO

        # Store model in data/ directory alongside DB
        # Use YOLO11x (extra-large) for highest accuracy (~54.7 mAP50-95 on COCO)
        # Still fast on RTX 4080 SUPER with CUDA
        model_dir = Path(__file__).resolve().parent.parent.parent.parent / "data"
        model_dir.mkdir(exist_ok=True)
        model_path = model_dir / "yolo11x.pt"
        self._model = YOLO(str(model_path))

        # Try CUDA, fall back to CPU
        try:
            import torch
            if torch.cuda.is_available():
                self._model.to("cuda")
                self._device = "cuda"
                gpu_name = torch.cuda.get_device_name(0)
                logger.info("YOLO model loaded on CUDA", extra={"model": model_path.name, "gpu": gpu_name})
            else:
                logger.warning("CUDA not available, YOLO running on CPU", extra={"model": model_path.name})
        except Exception:
            logger.warning("Failed to use CUDA, YOLO running on CPU", extra={"model": model_path.name})

    def _fallback_to_cpu(self) -> None:
        """Reload model on CPU after a CUDA error.

        After a CUDA error the entire CUDA context is unrecoverable in-process,
        so calling .to("cpu") on the existing model may also fail (it tries to
        free CUDA memory). Reloading from disk avoids touching the broken context.
        """
        try:
            from ultralytics import YOLO
            model_dir = Path(__file__).resolve().parent.parent.parent.parent / "data"
            model_path = model_dir / "yolo11x.pt"
            self._model = YOLO(str(model_path))
            # Do NOT call .to("cuda") — stay on CPU
            self._device = "cpu"
            logger.warning("YOLO model reloaded on CPU after CUDA error — restart service to re-enable GPU")
        except Exception as e:
            logger.error("Failed to reload YOLO model on CPU: %s", e)

    async def stop(self) -> None:
        """Release model and executor."""
        self._model = None
        self._started = False
        self._executor.shutdown(wait=False)
        logger.info("Object detector stopped")

    async def detect_objects(
        self, frame: np.ndarray, confidence_threshold: float = 0.5,
        classes: list[int] | None = None,
    ) -> list[Detection]:
        """Run object detection on a frame. Returns detections above threshold.

        Args:
            classes: COCO class IDs to detect. None means all classes.
        """
        if not self._started or self._model is None:
            return []

        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(
            self._executor,
            self._run_inference, frame, confidence_threshold, classes,
        )

    def _run_inference(
        self, frame: np.ndarray, threshold: float, classes: list[int] | None,
    ) -> list[Detection]:
        """Blocking YOLO inference (runs in executor).

        Also filters out detections whose bounding box is too small (likely artifacts).
        Falls back to CPU if CUDA errors occur (CUDA context can become permanently broken
        after certain errors; reloading on CPU recovers without a service restart).
        """
        try:
            results = self._model(frame, conf=threshold, classes=classes or None, verbose=False)
        except Exception as e:
            err_str = str(e)
            if "CUDA" in err_str or "cuda" in err_str or "AcceleratorError" in type(e).__name__:
                logger.warning("CUDA inference error — falling back to CPU: %s", type(e).__name__)
                self._fallback_to_cpu()
                results = self._model(frame, conf=threshold, classes=classes or None, verbose=False)
            else:
                raise

        frame_area = frame.shape[0] * frame.shape[1]
        min_box_area = frame_area * MIN_BOX_AREA_FRACTION

        detections = []
        for result in results:
            boxes = result.boxes
            if boxes is None or len(boxes) == 0:
                continue
            for i in range(len(boxes)):
                conf = float(boxes.conf[i])
                x1, y1, x2, y2 = boxes.xyxy[i].int().tolist()
                box_area = (x2 - x1) * (y2 - y1)
                if box_area < min_box_area:
                    continue
                cls_id = int(boxes.cls[i])
                label = COCO_CLASS_NAMES.get(cls_id, f"class_{cls_id}")
                detections.append(Detection(
                    label=label,
                    confidence=conf,
                    x1=x1, y1=y1, x2=x2, y2=y2,
                ))

        return detections


_detector: ObjectDetector | None = None


def get_object_detector() -> ObjectDetector:
    """Get or create the singleton object detector."""
    global _detector
    if _detector is None:
        _detector = ObjectDetector()
    return _detector
