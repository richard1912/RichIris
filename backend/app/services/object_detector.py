"""YOLO-based object detector for AI person detection on motion frames."""

import asyncio
import logging
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path

import numpy as np

logger = logging.getLogger(__name__)


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
        model_dir = Path(__file__).resolve().parent.parent.parent.parent / "data"
        model_dir.mkdir(exist_ok=True)
        model_path = model_dir / "yolov8n.pt"
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
        """Blocking YOLO inference (runs in executor). Filters to person class only."""
        results = self._model(frame, conf=threshold, classes=[0], verbose=False)

        detections = []
        for result in results:
            boxes = result.boxes
            if boxes is None or len(boxes) == 0:
                continue
            for i in range(len(boxes)):
                conf = float(boxes.conf[i])
                x1, y1, x2, y2 = boxes.xyxy[i].int().tolist()
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
