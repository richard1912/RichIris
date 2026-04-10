"""Shared frame broker: one persistent ffmpeg per camera pulling MJPEG frames
from the local go2rtc RTSP relay, exposed to any consumer (motion detector,
thumbnail capture, etc.) via a per-camera latest-frame buffer.

Replaces the previous "spawn ffmpeg per frame" pattern that was both slow
(2-4s per snapshot due to RTSP handshake + keyframe wait) and wasteful.
Also replaces the stream_manager s2_direct keepalive — the persistent ffmpeg
here acts as a permanent consumer, keeping go2rtc's sub-stream warm.
"""

import asyncio
import logging
import time

import cv2
import numpy as np

from app.config import get_config
from app.services.go2rtc_client import get_stream_name

logger = logging.getLogger(__name__)

# How often each broker pulls a fresh frame from go2rtc. 2 fps gives the AI
# motion pipeline 6 frames across its 3-frame confirmation window.
CAPTURE_FPS = 2
# Frames older than this are considered stale (broker probably reconnecting).
MAX_FRAME_AGE_SECONDS = 4.0
# Backoff on ffmpeg exit / crash.
RECONNECT_DELAY_SECONDS = 3.0
# JPEG markers for MJPEG frame boundary parsing.
_JPEG_SOI = b"\xff\xd8\xff"
_JPEG_EOI = b"\xff\xd9"


class FrameBroker:
    """Keeps one latest JPEG-decoded frame per camera, updated continuously."""

    def __init__(self) -> None:
        self._tasks: dict[int, asyncio.Task] = {}
        self._latest: dict[int, tuple[np.ndarray, float]] = {}
        self._running = False

    async def start(self, cameras: list) -> None:
        self._running = True
        stagger = 0.0
        for cam in cameras:
            # Only spin up a reader for cameras that have a usable stream.
            if not (cam.sub_stream_url or cam.rtsp_url):
                continue
            self._tasks[cam.id] = asyncio.create_task(
                self._reader_loop(cam.id, cam.name, startup_delay=stagger)
            )
            stagger += 0.5
        if self._tasks:
            logger.info("Frame broker started", extra={"cameras": len(self._tasks)})

    async def stop(self) -> None:
        self._running = False
        for task in self._tasks.values():
            task.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks.values(), return_exceptions=True)
        self._tasks.clear()
        self._latest.clear()
        logger.info("Frame broker stopped")

    async def add_camera(self, camera) -> None:
        if not self._running or camera.id in self._tasks:
            return
        if not (camera.sub_stream_url or camera.rtsp_url):
            return
        self._tasks[camera.id] = asyncio.create_task(
            self._reader_loop(camera.id, camera.name, startup_delay=1.0)
        )
        logger.info("Frame broker added camera", extra={"camera": camera.name})

    async def remove_camera(self, camera_id: int) -> None:
        task = self._tasks.pop(camera_id, None)
        if task:
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
        self._latest.pop(camera_id, None)

    def get_latest(self, camera_id: int, max_age: float = MAX_FRAME_AGE_SECONDS) -> np.ndarray | None:
        """Return the most recent frame for a camera, or None if missing/stale."""
        entry = self._latest.get(camera_id)
        if entry is None:
            return None
        frame, ts = entry
        if time.monotonic() - ts > max_age:
            return None
        return frame

    async def get_fresh(
        self, camera_id: int, max_wait: float = 5.0, poll_interval: float = 0.1
    ) -> np.ndarray | None:
        """Wait for a frame captured after this call. Returns None on timeout."""
        deadline = time.monotonic() + max_wait
        prev_ts = self._latest.get(camera_id, (None, 0.0))[1]
        while time.monotonic() < deadline:
            entry = self._latest.get(camera_id)
            if entry is not None and entry[1] > prev_ts:
                return entry[0]
            await asyncio.sleep(poll_interval)
        return None

    def _relay_url(self, camera_name: str) -> str:
        from app.services.go2rtc_manager import get_rtsp_port
        stream_name = get_stream_name(camera_name)
        return f"rtsp://127.0.0.1:{get_rtsp_port()}/{stream_name}_s2_direct"

    async def _reader_loop(self, cam_id: int, cam_name: str, startup_delay: float = 0) -> None:
        if startup_delay > 0:
            await asyncio.sleep(startup_delay)

        config = get_config()
        relay_url = self._relay_url(cam_name)
        logger.info(
            "Frame broker reader starting",
            extra={"camera": cam_name, "relay": relay_url, "fps": CAPTURE_FPS},
        )

        while self._running:
            proc = None
            try:
                cmd = [
                    config.ffmpeg.path,
                    "-hide_banner",
                    "-loglevel", "error",
                    "-rtsp_transport", "tcp",
                    "-i", relay_url,
                    "-vf", f"fps={CAPTURE_FPS}",
                    "-f", "image2pipe",
                    "-vcodec", "mjpeg",
                    "-q:v", "5",
                    "-",
                ]
                proc = await asyncio.create_subprocess_exec(
                    *cmd,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.DEVNULL,
                )
                await self._consume_mjpeg(cam_id, cam_name, proc.stdout)
            except asyncio.CancelledError:
                if proc and proc.returncode is None:
                    try:
                        proc.kill()
                    except Exception:
                        pass
                return
            except Exception:
                logger.exception("Frame broker reader error", extra={"camera": cam_name})
            finally:
                if proc and proc.returncode is None:
                    try:
                        proc.kill()
                        await proc.wait()
                    except Exception:
                        pass

            if not self._running:
                return
            await asyncio.sleep(RECONNECT_DELAY_SECONDS)

    async def _consume_mjpeg(
        self, cam_id: int, cam_name: str, reader: asyncio.StreamReader
    ) -> None:
        """Parse a concatenated MJPEG stream from ffmpeg's stdout into frames."""
        buf = bytearray()
        frames_seen = 0
        while self._running:
            chunk = await reader.read(65536)
            if not chunk:
                logger.debug(
                    "Frame broker stdout closed",
                    extra={"camera": cam_name, "frames_seen": frames_seen},
                )
                return
            buf.extend(chunk)
            # Extract all complete JPEGs from the buffer.
            while True:
                start = buf.find(_JPEG_SOI)
                if start < 0:
                    # No start marker at all — discard (keep last 2 bytes in case of
                    # a marker split across chunks).
                    if len(buf) > 2:
                        del buf[:-2]
                    break
                if start > 0:
                    del buf[:start]
                end = buf.find(_JPEG_EOI, 3)
                if end < 0:
                    # Incomplete frame, wait for more bytes.
                    break
                jpeg = bytes(buf[: end + 2])
                del buf[: end + 2]
                frame = cv2.imdecode(np.frombuffer(jpeg, np.uint8), cv2.IMREAD_COLOR)
                if frame is not None:
                    self._latest[cam_id] = (frame, time.monotonic())
                    frames_seen += 1
                    if frames_seen == 1:
                        logger.info(
                            "Frame broker first frame",
                            extra={
                                "camera": cam_name,
                                "shape": f"{frame.shape[1]}x{frame.shape[0]}",
                            },
                        )


_broker: FrameBroker | None = None


def get_frame_broker() -> FrameBroker:
    global _broker
    if _broker is None:
        _broker = FrameBroker()
    return _broker
