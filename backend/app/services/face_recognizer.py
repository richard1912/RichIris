"""Facial recognition using SCRFD (detection) + ArcFace (embedding) ONNX models.

Loaded on the same onnxruntime-directml pipeline as the object detector so we
get GPU inference for free when available. Invoked by the motion detector after
RT-DETR confirms a person — we crop the person bbox and then run face detection
+ embedding + cosine match against in-memory cache of enrolled embeddings.
"""

import asyncio
import logging
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path

import cv2
import numpy as np

logger = logging.getLogger(__name__)

SCRFD_INPUT = 640
SCRFD_STRIDES = (8, 16, 32)
SCRFD_ANCHORS_PER_CELL = 2
SCRFD_SCORE_THRESHOLD = 0.5
SCRFD_NMS_IOU = 0.4

ARCFACE_INPUT = 112

# ArcFace 5-point reference landmarks in 112x112 canonical space
_ARCFACE_DST = np.array([
    [38.2946, 51.6963],
    [73.5318, 51.5014],
    [56.0252, 71.7366],
    [41.5493, 92.3655],
    [70.7299, 92.2041],
], dtype=np.float32)

_SCRFD_FILENAMES = ["det_10g.onnx", "scrfd_10g_bnkps.onnx", "scrfd_2.5g_bnkps.onnx", "scrfd_500m_bnkps.onnx"]
_ARCFACE_FILENAMES = ["w600k_r50.onnx", "arcface_r100.onnx"]


@dataclass
class FaceHit:
    x1: int
    y1: int
    x2: int
    y2: int
    score: float
    landmarks: np.ndarray  # (5, 2) in original-frame pixel coords
    embedding: np.ndarray  # (512,) L2-normalized


@dataclass
class FaceMatch:
    face_id: int
    name: str
    confidence: float


def _letterbox(frame: np.ndarray, size: int) -> tuple[np.ndarray, float, int, int]:
    h, w = frame.shape[:2]
    scale = min(size / w, size / h)
    new_w, new_h = int(w * scale), int(h * scale)
    pad_x, pad_y = (size - new_w) // 2, (size - new_h) // 2
    resized = cv2.resize(frame, (new_w, new_h), interpolation=cv2.INTER_LINEAR)
    canvas = np.zeros((size, size, 3), dtype=np.uint8)
    canvas[pad_y:pad_y + new_h, pad_x:pad_x + new_w] = resized
    return canvas, scale, pad_x, pad_y


def _nms(boxes: np.ndarray, scores: np.ndarray, iou_thr: float) -> list[int]:
    if len(boxes) == 0:
        return []
    x1, y1, x2, y2 = boxes[:, 0], boxes[:, 1], boxes[:, 2], boxes[:, 3]
    areas = (x2 - x1) * (y2 - y1)
    order = scores.argsort()[::-1]
    keep = []
    while order.size > 0:
        i = order[0]
        keep.append(int(i))
        xx1 = np.maximum(x1[i], x1[order[1:]])
        yy1 = np.maximum(y1[i], y1[order[1:]])
        xx2 = np.minimum(x2[i], x2[order[1:]])
        yy2 = np.minimum(y2[i], y2[order[1:]])
        w = np.maximum(0.0, xx2 - xx1)
        h = np.maximum(0.0, yy2 - yy1)
        inter = w * h
        iou = inter / (areas[i] + areas[order[1:]] - inter + 1e-9)
        order = order[1:][iou <= iou_thr]
    return keep


def _scrfd_postprocess(
    outputs: list[np.ndarray],
    scale: float, pad_x: int, pad_y: int,
    frame_h: int, frame_w: int,
) -> list[tuple[np.ndarray, float, np.ndarray]]:
    """SCRFD outputs come as 9 tensors: (scores, bboxes, kps) × 3 strides.
    Returns list of (bbox_xyxy, score, landmarks_5x2) in original frame coords.
    """
    # SCRFD output order when exported with kps: scores×3, bboxes×3, kps×3
    # Reorder defensively by output count and shape.
    if len(outputs) != 9:
        return []
    scores_list = [outputs[i] for i in range(3)]
    bboxes_list = [outputs[i] for i in range(3, 6)]
    kps_list = [outputs[i] for i in range(6, 9)]

    all_boxes, all_scores, all_kps = [], [], []
    for stride, scores, bboxes, kps in zip(SCRFD_STRIDES, scores_list, bboxes_list, kps_list):
        # scores: (1, N*K, 1) or (N*K, 1) -> flatten
        s = scores.reshape(-1)
        b = bboxes.reshape(-1, 4)
        k = kps.reshape(-1, 10)
        # Generate anchor centers
        feat_w = SCRFD_INPUT // stride
        feat_h = SCRFD_INPUT // stride
        ys, xs = np.mgrid[0:feat_h, 0:feat_w]
        centers = np.stack([xs.ravel(), ys.ravel()], axis=-1).astype(np.float32) * stride
        centers = np.repeat(centers, SCRFD_ANCHORS_PER_CELL, axis=0)
        if centers.shape[0] != s.shape[0]:
            # Shape mismatch: safer to abort this stride
            continue
        mask = s >= SCRFD_SCORE_THRESHOLD
        if not np.any(mask):
            continue
        centers_f = centers[mask]
        s_f = s[mask]
        b_f = b[mask] * stride
        k_f = k[mask] * stride
        # bbox: ltrb distances from center
        x1 = centers_f[:, 0] - b_f[:, 0]
        y1 = centers_f[:, 1] - b_f[:, 1]
        x2 = centers_f[:, 0] + b_f[:, 2]
        y2 = centers_f[:, 1] + b_f[:, 3]
        boxes = np.stack([x1, y1, x2, y2], axis=-1)
        # kps: 5 points of (dx, dy) offsets from center
        kps_pts = k_f.reshape(-1, 5, 2)
        kps_pts[:, :, 0] = kps_pts[:, :, 0] + centers_f[:, 0:1]
        kps_pts[:, :, 1] = kps_pts[:, :, 1] + centers_f[:, 1:2]
        all_boxes.append(boxes)
        all_scores.append(s_f)
        all_kps.append(kps_pts)

    if not all_boxes:
        return []
    boxes = np.concatenate(all_boxes, axis=0)
    scores = np.concatenate(all_scores, axis=0)
    kps = np.concatenate(all_kps, axis=0)

    # Undo letterbox
    boxes[:, 0::2] = (boxes[:, 0::2] - pad_x) / scale
    boxes[:, 1::2] = (boxes[:, 1::2] - pad_y) / scale
    kps[:, :, 0] = (kps[:, :, 0] - pad_x) / scale
    kps[:, :, 1] = (kps[:, :, 1] - pad_y) / scale

    boxes[:, 0::2] = np.clip(boxes[:, 0::2], 0, frame_w)
    boxes[:, 1::2] = np.clip(boxes[:, 1::2], 0, frame_h)

    keep = _nms(boxes, scores, SCRFD_NMS_IOU)
    return [(boxes[i], float(scores[i]), kps[i]) for i in keep]


def _align_face(frame: np.ndarray, landmarks: np.ndarray) -> np.ndarray:
    """Warp face to 112x112 canonical via 5-point affine."""
    tform = cv2.estimateAffinePartial2D(landmarks.astype(np.float32), _ARCFACE_DST, method=cv2.LMEDS)[0]
    if tform is None:
        return cv2.resize(frame, (ARCFACE_INPUT, ARCFACE_INPUT))
    return cv2.warpAffine(frame, tform, (ARCFACE_INPUT, ARCFACE_INPUT), borderValue=0.0)


def _arcface_preprocess(aligned_bgr: np.ndarray) -> np.ndarray:
    rgb = cv2.cvtColor(aligned_bgr, cv2.COLOR_BGR2RGB).astype(np.float32)
    rgb = (rgb - 127.5) / 127.5
    blob = rgb.transpose(2, 0, 1)
    return np.expand_dims(blob, 0)


def _l2_normalize(v: np.ndarray) -> np.ndarray:
    n = np.linalg.norm(v) + 1e-9
    return v / n


class FaceRecognizer:
    """Singleton face pipeline.  SCRFD detector + ArcFace embedder + in-memory matcher."""

    def __init__(self):
        self._detector = None
        self._detector_input = None
        self._embedder = None
        self._embedder_input = None
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="face")
        self._started = False
        self._provider = "cpu"
        # Matcher cache: list of (face_id, name, 512-D vector)
        self._enrollment: list[tuple[int, str, np.ndarray]] = []
        self._loaded = False

    @property
    def available(self) -> bool:
        return self._started and self._detector is not None and self._embedder is not None

    async def start(self) -> None:
        if self._started:
            return
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(self._executor, self._load_models)
        self._started = True

    def _find_model(self, filenames: list[str]) -> Path | None:
        from app.config import get_app_dir, get_bootstrap
        data_dir = Path(get_bootstrap().data_dir)
        app_dir = get_app_dir()
        for filename in filenames:
            for directory in [data_dir, app_dir / "dependencies" / "models"]:
                p = directory / filename
                if p.exists():
                    return p
        return None

    def _load_session(self, model_path: Path):
        import onnxruntime as ort
        providers_to_try = [
            (["DmlExecutionProvider", "CPUExecutionProvider"], "DirectML"),
            (["CUDAExecutionProvider", "CPUExecutionProvider"], "CUDA"),
            (["CPUExecutionProvider"], "CPU"),
        ]
        for providers, label in providers_to_try:
            try:
                sess = ort.InferenceSession(str(model_path), providers=providers)
                self._provider = sess.get_providers()[0]
                logger.info("Face model loaded", extra={
                    "model": model_path.name, "provider": self._provider, "attempted": label,
                })
                return sess
            except Exception:
                continue
        return None

    def _load_models(self) -> None:
        scrfd = self._find_model(_SCRFD_FILENAMES)
        arc = self._find_model(_ARCFACE_FILENAMES)
        if scrfd is None or arc is None:
            logger.warning("Face models not found — facial recognition disabled", extra={
                "scrfd_found": bool(scrfd), "arcface_found": bool(arc),
            })
            return
        self._detector = self._load_session(scrfd)
        if self._detector:
            self._detector_input = self._detector.get_inputs()[0].name
        self._embedder = self._load_session(arc)
        if self._embedder:
            self._embedder_input = self._embedder.get_inputs()[0].name

    async def stop(self) -> None:
        self._detector = None
        self._embedder = None
        self._started = False
        self._executor.shutdown(wait=False)
        logger.info("Face recognizer stopped")

    async def reload_cache(self) -> None:
        """Rebuild in-memory embedding cache from DB."""
        from sqlalchemy import select

        from app.database import get_session_factory
        from app.models import Face, FaceEmbedding
        factory = get_session_factory()
        async with factory() as session:
            result = await session.execute(
                select(FaceEmbedding.face_id, Face.name, FaceEmbedding.embedding)
                .join(Face, Face.id == FaceEmbedding.face_id)
            )
            rows = result.all()
        cache: list[tuple[int, str, np.ndarray]] = []
        for face_id, name, blob in rows:
            vec = np.frombuffer(blob, dtype=np.float32)
            if vec.shape[0] != 512:
                continue
            cache.append((int(face_id), str(name), vec))
        self._enrollment = cache
        self._loaded = True
        logger.info("Face embedding cache loaded", extra={"count": len(cache)})

    async def detect_and_embed(
        self, frame_bgr: np.ndarray, person_bbox: tuple[int, int, int, int] | None = None,
    ) -> list[FaceHit]:
        """Run SCRFD + ArcFace. If person_bbox given, crop first and report coords in the full frame."""
        if not self.available:
            return []
        from app.services._onnx_lock import get_onnx_lock
        loop = asyncio.get_event_loop()
        async with get_onnx_lock():
            return await loop.run_in_executor(
                self._executor, self._detect_and_embed_sync, frame_bgr, person_bbox,
            )

    def _detect_and_embed_sync(
        self, frame: np.ndarray, person_bbox: tuple[int, int, int, int] | None,
    ) -> list[FaceHit]:
        if person_bbox is not None:
            x1, y1, x2, y2 = person_bbox
            # Pad the bbox slightly in case the head extends above the torso bbox
            h, w = frame.shape[:2]
            pad = int(0.1 * max(1, x2 - x1))
            pad_y = int(0.2 * max(1, y2 - y1))
            cx1 = max(0, x1 - pad)
            cy1 = max(0, y1 - pad_y)
            cx2 = min(w, x2 + pad)
            cy2 = min(h, y2 + pad_y)
            if cx2 <= cx1 or cy2 <= cy1:
                return []
            crop = frame[cy1:cy2, cx1:cx2]
            offset = (cx1, cy1)
        else:
            crop = frame
            offset = (0, 0)

        canvas, scale, pad_x, pad_yy = _letterbox(crop, SCRFD_INPUT)
        blob = canvas[:, :, ::-1].astype(np.float32)
        blob = (blob - 127.5) / 128.0
        blob = blob.transpose(2, 0, 1)[None, ...]

        try:
            outputs = self._detector.run(None, {self._detector_input: blob})
        except Exception:
            logger.exception("SCRFD inference failed")
            return []

        h, w = crop.shape[:2]
        raw = _scrfd_postprocess(outputs, scale, pad_x, pad_yy, h, w)
        if not raw:
            return []

        hits: list[FaceHit] = []
        for box, score, landmarks in raw:
            try:
                aligned = _align_face(crop, landmarks)
                emb_blob = _arcface_preprocess(aligned)
                out = self._embedder.run(None, {self._embedder_input: emb_blob})[0]
                emb = _l2_normalize(out.reshape(-1).astype(np.float32))
            except Exception:
                logger.exception("ArcFace embedding failed")
                continue
            # Translate box + landmarks back to full-frame coords if we cropped
            fbx1 = int(box[0]) + offset[0]
            fby1 = int(box[1]) + offset[1]
            fbx2 = int(box[2]) + offset[0]
            fby2 = int(box[3]) + offset[1]
            full_lm = landmarks.copy()
            full_lm[:, 0] += offset[0]
            full_lm[:, 1] += offset[1]
            hits.append(FaceHit(
                x1=fbx1, y1=fby1, x2=fbx2, y2=fby2,
                score=score, landmarks=full_lm, embedding=emb,
            ))
        return hits

    def match(self, embedding: np.ndarray, threshold: float) -> FaceMatch | None:
        if not self._enrollment:
            return None
        best_sim = -1.0
        best: tuple[int, str] | None = None
        for face_id, name, vec in self._enrollment:
            sim = float(np.dot(embedding, vec))
            if sim > best_sim:
                best_sim = sim
                best = (face_id, name)
        if best is None or best_sim < threshold:
            return None
        return FaceMatch(face_id=best[0], name=best[1], confidence=best_sim)


_recognizer: FaceRecognizer | None = None


def get_face_recognizer() -> FaceRecognizer:
    global _recognizer
    if _recognizer is None:
        _recognizer = FaceRecognizer()
    return _recognizer
