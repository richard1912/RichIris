"""YOLO-based object detector for AI person detection on motion frames."""

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
        # Use YOLOv8s (small) for better accuracy — still fast on RTX 4080 SUPER (~5-10ms/frame)
        model_dir = Path(__file__).resolve().parent.parent.parent.parent / "data"
        model_dir.mkdir(exist_ok=True)
        model_path = model_dir / "yolov8s.pt"
        self._model = YOLO(str(model_path))

        # Try CUDA, fall back to CPU
        try:
            import torch
            if torch.cuda.is_available():
                self._model.to("cuda")
                self._device = "cuda"
                gpu_name = torch.cuda.get_device_name(0)
                logger.info("YOLO model loaded on CUDA", extra={"gpu": gpu_name})
            else:
                logger.warning("CUDA not available, YOLO running on CPU")
        except Exception:
            logger.warning("Failed to use CUDA, YOLO running on CPU")

    def _fallback_to_cpu(self) -> None:
        """Reload model on CPU after a CUDA error.

        After a CUDA error the entire CUDA context is unrecoverable in-process,
        so calling .to("cpu") on the existing model may also fail (it tries to
        free CUDA memory). Reloading from disk avoids touching the broken context.
        """
        try:
            from ultralytics import YOLO
            model_dir = Path(__file__).resolve().parent.parent.parent.parent / "data"
            model_path = model_dir / "yolov8s.pt"
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

    async def detect_persons(self, frame: np.ndarray, confidence_threshold: float = 0.5) -> list[Detection]:
        """Run person detection on a frame. Returns list of person detections above threshold."""
        if not self._started or self._model is None:
            return []

        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(
            self._executor,
            self._run_inference, frame, confidence_threshold,
        )

    def _run_inference(self, frame: np.ndarray, threshold: float) -> list[Detection]:
        """Blocking YOLO inference (runs in executor). Filters to person class only.

        Also filters out detections whose bounding box is too small (likely artifacts).
        Falls back to CPU if CUDA errors occur (CUDA context can become permanently broken
        after certain errors; reloading on CPU recovers without a service restart).
        """
        try:
            results = self._model(frame, conf=threshold, classes=[0], verbose=False)
        except Exception as e:
            err_str = str(e)
            if "CUDA" in err_str or "cuda" in err_str or "AcceleratorError" in type(e).__name__:
                logger.warning("CUDA inference error — falling back to CPU: %s", type(e).__name__)
                self._fallback_to_cpu()
                results = self._model(frame, conf=threshold, classes=[0], verbose=False)
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
                detections.append(Detection(
                    label="person",
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
