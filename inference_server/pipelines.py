"""Pre/post-processing for RT-DETR, SCRFD and ArcFace.

Self-contained copies of the pipelines in backend/app/services/object_detector.py
and face_recognizer.py — deliberately NOT imported from the backend package so
this server needs only numpy + cv2 + onnxruntime (no FastAPI-app config,
SQLAlchemy, etc.). Keep in sync if the backend pipelines change.
"""

import cv2
import numpy as np

# ---------------------------------------------------------------------------
# RT-DETR object detection
# ---------------------------------------------------------------------------

INPUT_SIZE = 640
MIN_BOX_AREA_FRACTION = 0.002  # 0.2% of frame

COCO_CLASS_NAMES = {
    0: "person",
    1: "bicycle", 2: "car", 3: "motorcycle", 5: "bus", 7: "truck",
    14: "bird", 15: "cat", 16: "dog", 17: "horse",
    18: "sheep", 19: "cow", 20: "elephant", 21: "bear",
    22: "zebra", 23: "giraffe",
}

ALL_DETECTION_CLASSES = set(COCO_CLASS_NAMES.keys())


def detect_preprocess(frame: np.ndarray) -> tuple[np.ndarray, float, int, int]:
    """Resize frame to 640x640 with letterboxing, normalize to [0,1] NCHW."""
    h, w = frame.shape[:2]
    scale = min(INPUT_SIZE / w, INPUT_SIZE / h)
    new_w, new_h = int(w * scale), int(h * scale)
    pad_x, pad_y = (INPUT_SIZE - new_w) // 2, (INPUT_SIZE - new_h) // 2

    resized = cv2.resize(frame, (new_w, new_h), interpolation=cv2.INTER_LINEAR)
    canvas = np.full((INPUT_SIZE, INPUT_SIZE, 3), 114, dtype=np.uint8)
    canvas[pad_y:pad_y + new_h, pad_x:pad_x + new_w] = resized

    blob = canvas[:, :, ::-1].transpose(2, 0, 1).astype(np.float32) / 255.0
    return np.expand_dims(blob, 0), scale, pad_x, pad_y


def detect_postprocess(
    output: np.ndarray,
    scale: float, pad_x: int, pad_y: int,
    frame_h: int, frame_w: int,
    confidence_threshold: float,
    classes: set[int] | None,
) -> list[dict]:
    """Parse RT-DETR output [1, 300, 84] → list of detection dicts."""
    preds = output[0]

    boxes_norm = preds[:, :4]
    scores_all = preds[:, 4:]

    class_ids = np.argmax(scores_all, axis=1)
    confidences = scores_all[np.arange(len(class_ids)), class_ids]

    mask = confidences >= confidence_threshold
    if classes:
        mask &= np.isin(class_ids, list(classes))

    indices = np.where(mask)[0]
    if len(indices) == 0:
        return []

    boxes_norm = boxes_norm[indices]
    class_ids = class_ids[indices]
    confidences = confidences[indices]

    cx = boxes_norm[:, 0] * INPUT_SIZE
    cy = boxes_norm[:, 1] * INPUT_SIZE
    w = boxes_norm[:, 2] * INPUT_SIZE
    h = boxes_norm[:, 3] * INPUT_SIZE
    x1 = cx - w / 2
    y1 = cy - h / 2
    x2 = cx + w / 2
    y2 = cy + h / 2

    x1 = (x1 - pad_x) / scale
    y1 = (y1 - pad_y) / scale
    x2 = (x2 - pad_x) / scale
    y2 = (y2 - pad_y) / scale

    x1 = np.clip(x1, 0, frame_w).astype(np.int32)
    y1 = np.clip(y1, 0, frame_h).astype(np.int32)
    x2 = np.clip(x2, 0, frame_w).astype(np.int32)
    y2 = np.clip(y2, 0, frame_h).astype(np.int32)

    frame_area = frame_h * frame_w
    min_box_area = frame_area * MIN_BOX_AREA_FRACTION

    detections = []
    for i in range(len(indices)):
        bx1, by1, bx2, by2 = int(x1[i]), int(y1[i]), int(x2[i]), int(y2[i])
        if (bx2 - bx1) * (by2 - by1) < min_box_area:
            continue
        cls_id = int(class_ids[i])
        detections.append({
            "class_id": cls_id,
            "label": COCO_CLASS_NAMES.get(cls_id, f"class_{cls_id}"),
            "confidence": float(confidences[i]),
            "bbox": [bx1, by1, bx2, by2],
        })
    return detections


# ---------------------------------------------------------------------------
# SCRFD face detection + ArcFace embedding
# ---------------------------------------------------------------------------

SCRFD_INPUT = 640
SCRFD_STRIDES = (8, 16, 32)
SCRFD_ANCHORS_PER_CELL = 2
SCRFD_SCORE_THRESHOLD = 0.6
SCRFD_NMS_IOU = 0.4
SCRFD_MIN_FACE_SIZE = 40

ARCFACE_INPUT = 112

_ARCFACE_DST = np.array([
    [38.2946, 51.6963],
    [73.5318, 51.5014],
    [56.0252, 71.7366],
    [41.5493, 92.3655],
    [70.7299, 92.2041],
], dtype=np.float32)


def letterbox(frame: np.ndarray, size: int) -> tuple[np.ndarray, float, int, int]:
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


def scrfd_preprocess(crop: np.ndarray) -> tuple[np.ndarray, float, int, int]:
    canvas, scale, pad_x, pad_y = letterbox(crop, SCRFD_INPUT)
    blob = canvas[:, :, ::-1].astype(np.float32)
    blob = (blob - 127.5) / 128.0
    blob = blob.transpose(2, 0, 1)[None, ...]
    return blob, scale, pad_x, pad_y


def scrfd_postprocess(
    outputs: list[np.ndarray],
    scale: float, pad_x: int, pad_y: int,
    frame_h: int, frame_w: int,
) -> list[tuple[np.ndarray, float, np.ndarray]]:
    """Returns list of (bbox_xyxy, score, landmarks_5x2) in crop coords."""
    if len(outputs) != 9:
        return []
    scores_list = [outputs[i] for i in range(3)]
    bboxes_list = [outputs[i] for i in range(3, 6)]
    kps_list = [outputs[i] for i in range(6, 9)]

    all_boxes, all_scores, all_kps = [], [], []
    for stride, scores, bboxes, kps in zip(SCRFD_STRIDES, scores_list, bboxes_list, kps_list):
        s = scores.reshape(-1)
        b = bboxes.reshape(-1, 4)
        k = kps.reshape(-1, 10)
        feat_w = SCRFD_INPUT // stride
        feat_h = SCRFD_INPUT // stride
        ys, xs = np.mgrid[0:feat_h, 0:feat_w]
        centers = np.stack([xs.ravel(), ys.ravel()], axis=-1).astype(np.float32) * stride
        centers = np.repeat(centers, SCRFD_ANCHORS_PER_CELL, axis=0)
        if centers.shape[0] != s.shape[0]:
            continue
        mask = s >= SCRFD_SCORE_THRESHOLD
        if not np.any(mask):
            continue
        centers_f = centers[mask]
        s_f = s[mask]
        b_f = b[mask] * stride
        k_f = k[mask] * stride
        x1 = centers_f[:, 0] - b_f[:, 0]
        y1 = centers_f[:, 1] - b_f[:, 1]
        x2 = centers_f[:, 0] + b_f[:, 2]
        y2 = centers_f[:, 1] + b_f[:, 3]
        boxes = np.stack([x1, y1, x2, y2], axis=-1)
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

    boxes[:, 0::2] = (boxes[:, 0::2] - pad_x) / scale
    boxes[:, 1::2] = (boxes[:, 1::2] - pad_y) / scale
    kps[:, :, 0] = (kps[:, :, 0] - pad_x) / scale
    kps[:, :, 1] = (kps[:, :, 1] - pad_y) / scale

    boxes[:, 0::2] = np.clip(boxes[:, 0::2], 0, frame_w)
    boxes[:, 1::2] = np.clip(boxes[:, 1::2], 0, frame_h)

    keep = _nms(boxes, scores, SCRFD_NMS_IOU)
    return [(boxes[i], float(scores[i]), kps[i]) for i in keep]


def _umeyama_similarity(src: np.ndarray, dst: np.ndarray) -> np.ndarray | None:
    src = src.astype(np.float64)
    dst = dst.astype(np.float64)
    n = src.shape[0]
    src_mean = src.mean(axis=0)
    dst_mean = dst.mean(axis=0)
    src_c = src - src_mean
    dst_c = dst - dst_mean
    H = dst_c.T @ src_c / n
    U, S, Vt = np.linalg.svd(H)
    d = np.sign(np.linalg.det(U @ Vt))
    D = np.diag([1.0, d])
    R = U @ D @ Vt
    var_src = (src_c ** 2).sum() / n
    if var_src < 1e-12:
        return None
    scale = (S * np.array([1.0, d])).sum() / var_src
    t = dst_mean - scale * R @ src_mean
    M = np.zeros((2, 3), dtype=np.float32)
    M[:, :2] = scale * R
    M[:, 2] = t
    return M


def align_face(frame: np.ndarray, landmarks: np.ndarray) -> np.ndarray:
    tform = _umeyama_similarity(landmarks, _ARCFACE_DST)
    if tform is None:
        return cv2.resize(frame, (ARCFACE_INPUT, ARCFACE_INPUT))
    return cv2.warpAffine(frame, tform, (ARCFACE_INPUT, ARCFACE_INPUT), borderValue=0.0)


def arcface_preprocess(aligned_bgr: np.ndarray) -> np.ndarray:
    rgb = cv2.cvtColor(aligned_bgr, cv2.COLOR_BGR2RGB).astype(np.float32)
    rgb = (rgb - 127.5) / 127.5
    blob = rgb.transpose(2, 0, 1)
    return np.expand_dims(blob, 0)


def l2_normalize(v: np.ndarray) -> np.ndarray:
    n = np.linalg.norm(v) + 1e-9
    return v / n


def crop_person_bbox(
    frame: np.ndarray, person_bbox: tuple[int, int, int, int],
) -> tuple[np.ndarray, tuple[int, int]] | None:
    """Pad + crop a person bbox exactly like the backend's _detect_and_embed_sync."""
    x1, y1, x2, y2 = person_bbox
    h, w = frame.shape[:2]
    pad = int(0.1 * max(1, x2 - x1))
    pad_y = int(0.2 * max(1, y2 - y1))
    cx1 = max(0, x1 - pad)
    cy1 = max(0, y1 - pad_y)
    cx2 = min(w, x2 + pad)
    cy2 = min(h, y2 + pad_y)
    if cx2 <= cx1 or cy2 <= cy1:
        return None
    return frame[cy1:cy2, cx1:cx2], (cx1, cy1)
