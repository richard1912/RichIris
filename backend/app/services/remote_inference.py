"""Client for the remote inference server (GPU box, see inference_server/).

Offloads RT-DETR object detection and SCRFD+ArcFace face inference to a
remote HTTP server (e.g. the Windows PC's RTX 4080 at http://192.168.8.11:8701)
instead of running ONNX in-process. Configured via the `ai.remote_url` setting;
empty means fully local.

Failure semantics: every public call returns None on any failure, which tells
the caller to fall back to local inference. A simple circuit breaker stops
hammering a dead server — after FAILURE_THRESHOLD consecutive failures, calls
short-circuit to None for COOLDOWN_SECONDS, then the next call is let through
as a probe (half-open).
"""

import asyncio
import base64
import logging
import time

import cv2
import httpx
import numpy as np

from app.config import get_config

logger = logging.getLogger(__name__)

FAILURE_THRESHOLD = 3
COOLDOWN_SECONDS = 30.0
JPEG_QUALITY = 90


def _encode_jpeg(frame: np.ndarray) -> bytes:
    ok, buf = cv2.imencode(".jpg", frame, [int(cv2.IMWRITE_JPEG_QUALITY), JPEG_QUALITY])
    if not ok:
        raise ValueError("JPEG encode failed")
    return buf.tobytes()


class RemoteInference:
    """Singleton HTTP client with circuit breaker for the inference server."""

    def __init__(self) -> None:
        self._client: httpx.AsyncClient | None = None
        self._failures = 0
        self._open_until = 0.0
        self._was_remote = False  # for logging transitions

    @property
    def enabled(self) -> bool:
        """True when a remote URL is configured (regardless of circuit state)."""
        return bool(get_config().ai.remote_url)

    def _circuit_open(self) -> bool:
        return time.monotonic() < self._open_until

    def _get_client(self) -> httpx.AsyncClient:
        timeout = max(get_config().ai.remote_timeout_ms, 500) / 1000.0
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(timeout=timeout)
        return self._client

    def _record_success(self) -> None:
        if self._failures >= FAILURE_THRESHOLD or not self._was_remote:
            logger.info("Remote inference active", extra={"url": get_config().ai.remote_url})
        self._failures = 0
        self._was_remote = True

    def _record_failure(self, what: str, exc: Exception | None = None) -> None:
        self._failures += 1
        if self._failures == FAILURE_THRESHOLD:
            self._open_until = time.monotonic() + COOLDOWN_SECONDS
            logger.warning(
                "Remote inference circuit opened — falling back to local",
                extra={"what": what, "failures": self._failures,
                       "cooldown_s": COOLDOWN_SECONDS,
                       "error": repr(exc) if exc else ""},
            )
            self._was_remote = False
        elif self._failures < FAILURE_THRESHOLD:
            logger.debug("Remote inference call failed", extra={"what": what, "error": repr(exc) if exc else ""})
        else:
            # Re-open after a failed half-open probe
            self._open_until = time.monotonic() + COOLDOWN_SECONDS

    async def _post(self, path: str, params: dict, frame: np.ndarray) -> dict | None:
        """POST a JPEG-encoded frame; return parsed JSON or None on failure."""
        if not self.enabled:
            return None
        if self._circuit_open():
            return None
        url = get_config().ai.remote_url.rstrip("/") + path
        try:
            payload = await asyncio.to_thread(_encode_jpeg, frame)
            client = self._get_client()
            resp = await client.post(
                url, params=params, content=payload,
                headers={"Content-Type": "image/jpeg"},
            )
            resp.raise_for_status()
            data = resp.json()
            self._record_success()
            return data
        except Exception as exc:
            self._record_failure(path, exc)
            return None

    async def detect(
        self, frame: np.ndarray, threshold: float, classes: list[int] | None,
    ) -> list | None:
        """Remote object detection. Returns list[Detection] or None (= use local)."""
        from app.services.object_detector import Detection

        params: dict = {"threshold": threshold}
        if classes:
            params["classes"] = ",".join(str(c) for c in classes)
        data = await self._post("/v1/detect", params, frame)
        if data is None:
            return None
        try:
            detections = []
            for d in data.get("detections", []):
                x1, y1, x2, y2 = d["bbox"]
                detections.append(Detection(
                    label=str(d["label"]),
                    confidence=float(d["confidence"]),
                    x1=int(x1), y1=int(y1), x2=int(x2), y2=int(y2),
                ))
            return detections
        except Exception as exc:
            self._record_failure("/v1/detect parse", exc)
            return None

    async def faces(
        self, frame: np.ndarray, person_bbox: tuple[int, int, int, int] | None,
    ) -> list | None:
        """Remote face detect+embed. Returns list[FaceHit] or None (= use local).

        The server replicates the local crop/pad logic and returns coordinates
        in full-frame space.
        """
        from app.services.face_recognizer import FaceHit

        params: dict = {}
        if person_bbox is not None:
            params["bbox"] = ",".join(str(int(v)) for v in person_bbox)
        data = await self._post("/v1/faces", params, frame)
        if data is None:
            return None
        try:
            hits = []
            for f in data.get("faces", []):
                x1, y1, x2, y2 = f["bbox"]
                embedding = np.frombuffer(
                    base64.b64decode(f["embedding_b64"]), dtype=np.float32,
                ).copy()
                if embedding.shape[0] != 512:
                    continue
                hits.append(FaceHit(
                    x1=int(x1), y1=int(y1), x2=int(x2), y2=int(y2),
                    score=float(f["det_score"]),
                    landmarks=np.asarray(f["kps"], dtype=np.float32).reshape(5, 2),
                    embedding=embedding,
                ))
            return hits
        except Exception as exc:
            self._record_failure("/v1/faces parse", exc)
            return None

    async def close(self) -> None:
        if self._client is not None and not self._client.is_closed:
            await self._client.aclose()
        self._client = None


_remote: RemoteInference | None = None


def get_remote_inference() -> RemoteInference:
    global _remote
    if _remote is None:
        _remote = RemoteInference()
    return _remote
