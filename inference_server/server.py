"""RichIris remote inference server.

Serves RT-DETR object detection and SCRFD+ArcFace face inference over HTTP so
the NVR backend (running on the Debian box) can use this machine's GPU.
Consumed by backend/app/services/remote_inference.py.

Run:  python server.py           (listens on 0.0.0.0:8701)
Install as a Windows service:  install-service.bat
"""

import base64
import logging
import time

import cv2
import numpy as np
import uvicorn
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import JSONResponse

from engine import Engine

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
logger = logging.getLogger("inference.server")

HOST = "0.0.0.0"
PORT = 8701

engine = Engine()
app = FastAPI(title="RichIris Inference Server", version="1.0")


@app.on_event("startup")
def _startup() -> None:
    engine.load_all()


def _decode_frame(body: bytes) -> np.ndarray:
    if not body:
        raise HTTPException(status_code=400, detail="empty body (expected JPEG bytes)")
    frame = cv2.imdecode(np.frombuffer(body, dtype=np.uint8), cv2.IMREAD_COLOR)
    if frame is None:
        raise HTTPException(status_code=400, detail="could not decode image")
    return frame


@app.get("/health")
def health() -> dict:
    models = []
    if engine.detector is not None:
        models.append("rtdetr-l")
    if engine.scrfd is not None:
        models.append("scrfd-10g")
    if engine.arcface is not None:
        models.append("arcface-w600k")
    return {
        "status": "ok" if models else "degraded",
        "provider": engine.provider,
        "models": models,
        "uptime_s": round(time.monotonic() - engine.started_at, 1),
    }


@app.post("/v1/detect")
async def detect(
    request: Request,
    threshold: float = Query(0.5, ge=0.0, le=1.0),
    classes: str = Query("", description="comma-separated COCO class ids"),
) -> JSONResponse:
    frame = _decode_frame(await request.body())
    class_set: set[int] | None = None
    if classes.strip():
        try:
            class_set = {int(c) for c in classes.split(",") if c.strip()}
        except ValueError:
            raise HTTPException(status_code=400, detail="invalid classes parameter")

    t0 = time.monotonic()
    try:
        detections = engine.detect(frame, threshold, class_set)
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc))
    infer_ms = round((time.monotonic() - t0) * 1000, 1)
    return JSONResponse({"detections": detections, "infer_ms": infer_ms})


@app.post("/v1/faces")
async def faces(
    request: Request,
    bbox: str = Query("", description="optional person bbox: x1,y1,x2,y2"),
) -> JSONResponse:
    frame = _decode_frame(await request.body())
    person_bbox: tuple[int, int, int, int] | None = None
    if bbox.strip():
        try:
            parts = [int(v) for v in bbox.split(",")]
            if len(parts) != 4:
                raise ValueError
            person_bbox = (parts[0], parts[1], parts[2], parts[3])
        except ValueError:
            raise HTTPException(status_code=400, detail="invalid bbox parameter")

    t0 = time.monotonic()
    try:
        results = engine.faces(frame, person_bbox)
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc))
    infer_ms = round((time.monotonic() - t0) * 1000, 1)

    payload = []
    for r in results:
        payload.append({
            "bbox": r["bbox"],
            "det_score": r["det_score"],
            "kps": r["kps"],
            "embedding_b64": base64.b64encode(
                np.asarray(r["embedding"], dtype=np.float32).tobytes()
            ).decode("ascii"),
        })
    return JSONResponse({"faces": payload, "infer_ms": infer_ms})


if __name__ == "__main__":
    uvicorn.run(app, host=HOST, port=PORT, log_level="info")
