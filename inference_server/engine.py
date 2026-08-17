"""ONNX session management for the RichIris inference server.

Loads RT-DETR (object detection), SCRFD (face detection) and ArcFace (face
embedding) once at startup on DirectML (GPU) with CPU fallback, warms them up,
and serialises all inference behind a single threading.Lock — DirectML crashes
on concurrent sessions (same rationale as backend/app/services/_onnx_lock.py).
"""

import logging
import threading
import time
from pathlib import Path

import numpy as np

import pipelines

logger = logging.getLogger("inference.engine")

# Models live in the repo's dependencies/models/ (shared with the backend)
MODELS_DIR = Path(__file__).resolve().parent.parent / "dependencies" / "models"

_DETECT_FILENAMES = ["rtdetr-l.onnx", "yolo11x.onnx"]
_SCRFD_FILENAMES = ["det_10g.onnx", "scrfd_10g_bnkps.onnx", "scrfd_2.5g_bnkps.onnx", "scrfd_500m_bnkps.onnx"]
_ARCFACE_FILENAMES = ["w600k_r50.onnx", "arcface_r100.onnx"]

_PROVIDERS_TO_TRY = [
    (["DmlExecutionProvider", "CPUExecutionProvider"], "DirectML"),
    (["CUDAExecutionProvider", "CPUExecutionProvider"], "CUDA"),
    (["CPUExecutionProvider"], "CPU"),
]


class Engine:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.detector = None
        self.detector_input = None
        self.scrfd = None
        self.scrfd_input = None
        self.arcface = None
        self.arcface_input = None
        self.provider = "none"
        self.started_at = time.monotonic()

    # -- loading ------------------------------------------------------------

    def _find_model(self, filenames: list[str]) -> Path | None:
        for filename in filenames:
            p = MODELS_DIR / filename
            if p.exists():
                return p
        return None

    def _load_session(self, model_path: Path):
        import onnxruntime as ort
        for providers, label in _PROVIDERS_TO_TRY:
            try:
                sess = ort.InferenceSession(str(model_path), providers=providers)
                active = sess.get_providers()
                self.provider = active[0] if active else "unknown"
                logger.info("Model loaded: %s (provider=%s, attempted=%s)",
                            model_path.name, self.provider, label)
                return sess
            except Exception:
                logger.debug("Provider %s unavailable for %s", label, model_path.name)
                continue
        logger.error("Failed to load %s with any provider", model_path.name)
        return None

    def load_all(self) -> None:
        detect_path = self._find_model(_DETECT_FILENAMES)
        scrfd_path = self._find_model(_SCRFD_FILENAMES)
        arc_path = self._find_model(_ARCFACE_FILENAMES)

        if detect_path:
            self.detector = self._load_session(detect_path)
            if self.detector:
                self.detector_input = self.detector.get_inputs()[0].name
                dummy = np.random.rand(1, 3, pipelines.INPUT_SIZE, pipelines.INPUT_SIZE).astype(np.float32)
                self.detector.run(None, {self.detector_input: dummy})
        else:
            logger.error("Detection model not found in %s", MODELS_DIR)

        if scrfd_path:
            self.scrfd = self._load_session(scrfd_path)
            if self.scrfd:
                self.scrfd_input = self.scrfd.get_inputs()[0].name
                dummy = np.random.rand(1, 3, pipelines.SCRFD_INPUT, pipelines.SCRFD_INPUT).astype(np.float32)
                self.scrfd.run(None, {self.scrfd_input: dummy})
        else:
            logger.warning("SCRFD model not found in %s", MODELS_DIR)

        if arc_path:
            self.arcface = self._load_session(arc_path)
            if self.arcface:
                self.arcface_input = self.arcface.get_inputs()[0].name
                dummy = np.random.rand(1, 3, pipelines.ARCFACE_INPUT, pipelines.ARCFACE_INPUT).astype(np.float32)
                self.arcface.run(None, {self.arcface_input: dummy})
        else:
            logger.warning("ArcFace model not found in %s", MODELS_DIR)

        logger.info("Engine ready (provider=%s, detect=%s, scrfd=%s, arcface=%s)",
                    self.provider, bool(self.detector), bool(self.scrfd), bool(self.arcface))

    # -- inference ----------------------------------------------------------

    def detect(self, frame: np.ndarray, threshold: float, classes: set[int] | None) -> list[dict]:
        if self.detector is None:
            raise RuntimeError("detection model not loaded")
        h, w = frame.shape[:2]
        blob, scale, pad_x, pad_y = pipelines.detect_preprocess(frame)
        with self.lock:
            outputs = self.detector.run(None, {self.detector_input: blob})
        class_set = classes if classes else pipelines.ALL_DETECTION_CLASSES
        return pipelines.detect_postprocess(
            outputs[0], scale, pad_x, pad_y, h, w, threshold, class_set,
        )

    def faces(self, frame: np.ndarray, person_bbox: tuple[int, int, int, int] | None) -> list[dict]:
        if self.scrfd is None or self.arcface is None:
            raise RuntimeError("face models not loaded")

        if person_bbox is not None:
            cropped = pipelines.crop_person_bbox(frame, person_bbox)
            if cropped is None:
                return []
            crop, offset = cropped
        else:
            crop = frame
            offset = (0, 0)

        blob, scale, pad_x, pad_y = pipelines.scrfd_preprocess(crop)
        with self.lock:
            outputs = self.scrfd.run(None, {self.scrfd_input: blob})

        h, w = crop.shape[:2]
        raw = pipelines.scrfd_postprocess(outputs, scale, pad_x, pad_y, h, w)
        if not raw:
            return []

        results = []
        for box, score, landmarks in raw:
            box_w = float(box[2] - box[0])
            box_h = float(box[3] - box[1])
            if min(box_w, box_h) < pipelines.SCRFD_MIN_FACE_SIZE:
                continue
            aligned = pipelines.align_face(crop, landmarks)
            emb_blob = pipelines.arcface_preprocess(aligned)
            with self.lock:
                out = self.arcface.run(None, {self.arcface_input: emb_blob})[0]
            emb = pipelines.l2_normalize(out.reshape(-1).astype(np.float32))

            full_lm = landmarks.copy()
            full_lm[:, 0] += offset[0]
            full_lm[:, 1] += offset[1]
            results.append({
                "bbox": [int(box[0]) + offset[0], int(box[1]) + offset[1],
                         int(box[2]) + offset[0], int(box[3]) + offset[1]],
                "det_score": float(score),
                "kps": full_lm.tolist(),
                "embedding": emb,
            })
        return results
