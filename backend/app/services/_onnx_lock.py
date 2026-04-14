"""Shared asyncio lock serializing all ONNX Runtime inference calls.

DirectML's GPU provider can native-crash (returncode 255, no Python traceback)
when multiple ONNX sessions run concurrently on the same device. Our detectors
each own a ThreadPoolExecutor(max_workers=1) which serializes their own calls,
but RT-DETR + SCRFD + ArcFace are three separate sessions that would otherwise
fire in parallel during a busy person event.

Acquire this lock in any coroutine that dispatches an ONNX inference job to an
executor, so the scheduler sees at most one in-flight run across the whole
backend. Latency cost is negligible (~10–50 ms per call on GPU) compared to
the blast radius of a native crash that kills the FastAPI server.
"""

import asyncio

_lock = asyncio.Lock()


def get_onnx_lock() -> asyncio.Lock:
    return _lock
