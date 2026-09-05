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

# IoU threshold for NMS on YOLO-family models (RT-DETR is NMS-free).
NMS_IOU_THRESHOLD = 0.45

# Threads for the local CPU fallback session. Deliberately 1: on the i5-8600
# yolo11n@320 costs 21.6 ms on 1 core, 14.0 ms on 2 and 12.1 ms on 5 — buying
# ~9 ms of latency for 4 extra cores is a bad trade on a box that is also
# decoding 8 camera streams. Latency here is not the bottleneck; headroom is.
LOCAL_CPU_THREADS = 1

# Model input size (must match export: imgsz=640)
INPUT_SIZE = 640

# --- Region-of-interest cropping (Frigate-style) -----------------------------
# Detecting on the whole frame wastes most of the model's input on scenery: a
# person 40x80px in a 640x480 frame is still 40x80px after letterboxing, because
# the frame already fits inside 640x640. Cropping a square around the motion and
# letting _preprocess upscale THAT to 640x640 hands the model far more pixels on
# the thing that actually moved. Same inference cost, better small-object recall.
REGION_PADDING = 1.4          # expand the motion bbox by this much before squaring
REGION_MIN_SIZE = 224         # never crop tighter than this — upscaling a 30px
                              # blob to 640 amplifies noise, it does not reveal detail
REGION_SKIP_RATIO = 0.9       # motion this wide already fills the frame; don't bother


def compute_motion_region(
    motion_mask: np.ndarray, frame_h: int, frame_w: int,
) -> tuple[int, int, int] | None:
    """Square crop ``(x, y, size)`` around the motion, or None to use the full frame.

    ``motion_mask`` is the binary threshold image from the motion pre-filter, in
    the same pixel space as the frame. Returns None when the motion is spread so
    widely that the square would cover most of the frame anyway — cropping then
    buys nothing and only risks clipping something at the edge.
    """
    ys, xs = np.nonzero(motion_mask)
    if len(xs) == 0:
        return None

    x1, x2 = int(xs.min()), int(xs.max())
    y1, y2 = int(ys.min()), int(ys.max())

    size = max(x2 - x1, y2 - y1) * REGION_PADDING
    size = int(min(max(size, REGION_MIN_SIZE), frame_h, frame_w))

    if size >= min(frame_h, frame_w) * REGION_SKIP_RATIO:
        return None

    # Centre the square on the motion, then slide it fully inside the frame so
    # the crop is always exactly `size` square (no partial/ragged edges).
    cx, cy = (x1 + x2) / 2, (y1 + y2) / 2
    x = max(0, min(int(round(cx - size / 2)), frame_w - size))
    y = max(0, min(int(round(cy - size / 2)), frame_h - size))
    return x, y, size

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


def _preprocess(
    frame: np.ndarray, input_size: int = INPUT_SIZE,
) -> tuple[np.ndarray, float, int, int]:
    """Resize frame to input_size square with letterboxing, normalize to [0,1] NCHW."""
    h, w = frame.shape[:2]
    scale = min(input_size / w, input_size / h)
    new_w, new_h = int(w * scale), int(h * scale)
    pad_x, pad_y = (input_size - new_w) // 2, (input_size - new_h) // 2

    resized = cv2.resize(frame, (new_w, new_h), interpolation=cv2.INTER_LINEAR)
    canvas = np.full((input_size, input_size, 3), 114, dtype=np.uint8)
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
    input_size: int = INPUT_SIZE,
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
    cx = boxes_norm[:, 0] * input_size
    cy = boxes_norm[:, 1] * input_size
    w = boxes_norm[:, 2] * input_size
    h = boxes_norm[:, 3] * input_size
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


def _postprocess_yolo(
    output: np.ndarray,
    scale: float, pad_x: int, pad_y: int,
    frame_h: int, frame_w: int,
    confidence_threshold: float,
    classes: set[int] | None,
    input_size: int,
) -> list[Detection]:
    """Parse YOLOv8/v11 output [1, 84, N] → list of Detection.

    Unlike RT-DETR this is NOT NMS-free: the head emits one row per anchor, so
    a single object yields many overlapping boxes and NMS is mandatory. Layout
    is [cx, cy, w, h, class_scores×80] per anchor, transposed, and in INPUT-SPACE
    PIXELS rather than normalized — hence no multiply by input_size here.
    """
    preds = output[0].T  # (84, N) → (N, 84)

    boxes_xywh = preds[:, :4]
    scores_all = preds[:, 4:]

    class_ids = np.argmax(scores_all, axis=1)
    confidences = scores_all[np.arange(len(class_ids)), class_ids]

    mask = confidences >= confidence_threshold
    if classes:
        mask &= np.isin(class_ids, list(classes))
    indices = np.where(mask)[0]
    if len(indices) == 0:
        return []

    boxes_xywh = boxes_xywh[indices]
    class_ids = class_ids[indices]
    confidences = confidences[indices]

    cx, cy, w, h = boxes_xywh[:, 0], boxes_xywh[:, 1], boxes_xywh[:, 2], boxes_xywh[:, 3]
    x1, y1 = cx - w / 2, cy - h / 2

    # NMS in input space, per class (offset each class into its own coordinate
    # band so boxes of different classes never suppress each other).
    offsets = class_ids.astype(np.float32) * (input_size * 2)
    nms_boxes = np.stack([x1 + offsets, y1, w, h], axis=1)
    keep = cv2.dnn.NMSBoxes(
        nms_boxes.tolist(), confidences.astype(np.float32).tolist(),
        float(confidence_threshold), NMS_IOU_THRESHOLD,
    )
    if len(keep) == 0:
        return []
    keep = np.asarray(keep).flatten()

    # Undo letterbox → original frame pixels
    fx1 = np.clip((x1[keep] - pad_x) / scale, 0, frame_w).astype(np.int32)
    fy1 = np.clip((y1[keep] - pad_y) / scale, 0, frame_h).astype(np.int32)
    fx2 = np.clip((x1[keep] + w[keep] - pad_x) / scale, 0, frame_w).astype(np.int32)
    fy2 = np.clip((y1[keep] + h[keep] - pad_y) / scale, 0, frame_h).astype(np.int32)

    min_box_area = frame_h * frame_w * MIN_BOX_AREA_FRACTION
    detections = []
    for i, k in enumerate(keep):
        bx1, by1, bx2, by2 = int(fx1[i]), int(fy1[i]), int(fx2[i]), int(fy2[i])
        if (bx2 - bx1) * (by2 - by1) < min_box_area:
            continue
        cls_id = int(class_ids[k])
        detections.append(Detection(
            label=COCO_CLASS_NAMES.get(cls_id, f"class_{cls_id}"),
            confidence=float(confidences[k]),
            x1=bx1, y1=by1, x2=bx2, y2=by2,
        ))
    return detections


# Local-fallback model priority. The SMALL model comes first on purpose: this
# path only runs when the remote GPU is unreachable, and the box's i5-8600 needs
# 577 ms for rtdetr-l (2 cores) versus 22 ms for yolo11n@320 (1 core). Running
# the big model here is what turned a Windows crash into 6.5 h of degraded NVR
# on 2026-08-31. Accuracy is lower, but a fallback that keeps up beats one that
# collapses. The remote tier still serves RT-DETR-L whenever the GPU is up.
_MODEL_FILENAMES = ["yolo11n-320.onnx", "rtdetr-l.onnx", "yolo11x.onnx"]


class ObjectDetector:
    """Singleton ONNX Runtime-based object detector shared across all cameras.

    Uses DirectML (GPU) with CPU fallback. A ThreadPoolExecutor serializes
    inference so multiple camera loops don't contend on the GPU.
    """

    def __init__(self):
        self._session = None
        self._input_name = None
        self._input_size = INPUT_SIZE
        self._is_yolo = False
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="detector")
        self._started = False
        self._provider = "cpu"
        self._load_attempted = False
        self._load_lock = asyncio.Lock()

    async def start(self) -> None:
        """Load the ONNX model (deferred while remote inference is configured)."""
        if self._started:
            return

        from app.services.remote_inference import get_remote_inference
        if get_remote_inference().enabled:
            # Remote inference serves detections; keep the local model unloaded
            # (saves ~0.5-1 GB RAM) and lazy-load it only on remote failure.
            logger.info("Remote inference configured — deferring local detection model load")
        else:
            await self._ensure_local_loaded()
        self._started = True

    async def _ensure_local_loaded(self) -> None:
        """Load the local ONNX model once (lazy fallback path)."""
        if self._session is not None or self._load_attempted:
            return
        async with self._load_lock:
            if self._session is not None or self._load_attempted:
                return
            self._load_attempted = True
            loop = asyncio.get_event_loop()
            await loop.run_in_executor(self._executor, self._load_model)

    def _load_model(self) -> None:
        """Blocking model load (runs in executor)."""
        import onnxruntime as ort

        from app.config import get_app_dir, get_bootstrap
        data_dir = Path(get_bootstrap().data_dir)
        app_dir = get_app_dir()

        # An explicit ai.local_model wins; otherwise fall back to the priority
        # list. A configured-but-missing file falls through rather than leaving
        # the detector dead — a typo should not silently disable detection.
        from app.config import get_config
        configured = (get_config().ai.local_model or "").strip()
        candidates = ([configured] if configured else []) + [
            f for f in _MODEL_FILENAMES if f != configured
        ]

        model_path = None
        for filename in candidates:
            for directory in [data_dir, app_dir / "dependencies" / "models"]:
                p = directory / filename
                if p.exists():
                    model_path = p
                    break
            if model_path:
                break

        if model_path is None:
            logger.error("Detection ONNX model not found", extra={
                "searched_filenames": candidates,
            })
            return
        if configured and model_path.name != configured:
            logger.warning("Configured local model not found — using fallback", extra={
                "configured": configured, "using": model_path.name,
            })

        # Try DirectML (GPU) first, then CPU
        providers_to_try = [
            (["DmlExecutionProvider", "CPUExecutionProvider"], "DirectML"),
            (["CUDAExecutionProvider", "CPUExecutionProvider"], "CUDA"),
            (["CPUExecutionProvider"], "CPU"),
        ]

        for providers, label in providers_to_try:
            try:
                opts = ort.SessionOptions()
                opts.intra_op_num_threads = LOCAL_CPU_THREADS
                opts.inter_op_num_threads = 1
                self._session = ort.InferenceSession(
                    str(model_path), opts, providers=providers,
                )
                active = self._session.get_providers()
                self._provider = active[0] if active else "unknown"
                inp = self._session.get_inputs()[0]
                self._input_name = inp.name

                # Take the input size from the model rather than assuming 640 —
                # the small fallback model is a 320 export.
                shape = inp.shape
                self._input_size = shape[2] if isinstance(shape[2], int) else INPUT_SIZE

                # RT-DETR emits [1, 300, 84] (NMS-free); YOLO emits [1, 84, N]
                # (needs NMS). Picking the parser by shape rather than filename
                # means a swapped-in model can't be silently mis-parsed.
                out_shape = self._session.get_outputs()[0].shape
                self._is_yolo = (
                    len(out_shape) == 3
                    and isinstance(out_shape[1], int)
                    and out_shape[1] <= out_shape[2]
                )

                # Warmup inference
                dummy = np.random.rand(
                    1, 3, self._input_size, self._input_size,
                ).astype(np.float32)
                self._session.run(None, {self._input_name: dummy})

                logger.info("Detection model loaded", extra={
                    "model": model_path.name,
                    "provider": self._provider,
                    "attempted": label,
                    "input_size": self._input_size,
                    "format": "yolo" if self._is_yolo else "rtdetr",
                    "threads": LOCAL_CPU_THREADS,
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
        """Run object detection on a frame. Returns detections above threshold.

        Tries the remote inference server first (if configured); falls back to
        the local in-process ONNX session on failure.
        """
        if not self._started:
            return []

        from app.services.remote_inference import get_remote_inference
        remote = get_remote_inference()
        if remote.enabled:
            result = await remote.detect(frame, confidence_threshold, classes)
            if result is not None:
                return result
            await self._ensure_local_loaded()

        if self._session is None:
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
        blob, scale, pad_x, pad_y = _preprocess(frame, self._input_size)

        try:
            outputs = self._session.run(None, {self._input_name: blob})
        except Exception:
            logger.exception("ONNX inference failed")
            return []

        class_set = set(classes) if classes else ALL_DETECTION_CLASSES
        parse = _postprocess_yolo if self._is_yolo else _postprocess
        return parse(
            outputs[0], scale, pad_x, pad_y,
            h, w, threshold, class_set, self._input_size,
        )


_detector: ObjectDetector | None = None


def get_object_detector() -> ObjectDetector:
    """Get or create the singleton object detector."""
    global _detector
    if _detector is None:
        _detector = ObjectDetector()
    return _detector
