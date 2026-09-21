"""Main-stream poster frames for live view, rendered from the live GOP cache.

The poster a client paints while its decoder starts used to come only from
the FrameBroker, which reads the SUB stream. On most of these cameras the sub
stream is 640x480 (4:3) while the main stream is 3840x2160 (16:9), so a grid
showing Main painted a soft, pillarboxed poster and then visibly changed shape
and sharpness when the video took over.

The live cache already holds each main stream's newest keyframe, so this turns
that keyframe into a JPEG in the background: init segment + one fragment piped
through a short-lived ffmpeg, scaled to POSTER_WIDTH. It is pre-rendered
rather than on demand because the decode costs 210-400 ms per camera on the
box (measured 2026-09-22) and a cold app launch asks for all eight at once -
the poster exists to be on screen before the video, so it cannot wait on that.

Cost: one 4K I-frame decode per camera per REFRESH_SECONDS, staggered so two
never overlap. VAAPI when the box is configured for it, software otherwise.
"""

import asyncio
import logging
import time

from app.config import get_config
from app.services.live_cache import get_live_cache

logger = logging.getLogger(__name__)

# A poster is on screen for about a second before live video (itself 1-2 s
# behind real time) replaces it, so a few seconds of staleness is invisible
# unless something is crossing the frame at that moment.
#
# Each render costs ~0.22 CPU-seconds on the box even with VAAPI (it is mostly
# process + VAAPI device setup, not the decode), so 8 cameras at 5 s would be
# ~0.35 of a core forever, for a picture shown for a second at app launch.
# 10 s is ~0.18.
REFRESH_SECONDS = 10.0
POSTER_WIDTH = 1280
RENDER_TIMEOUT = 8.0
MAIN_SUFFIX = "_s1_direct"
HW_FAILURE_LIMIT = 5


class LivePoster:
    def __init__(self) -> None:
        self._jpegs: dict[str, tuple[bytes, float]] = {}
        self._rendered_key: dict[str, float] = {}
        self._task: asyncio.Task | None = None
        self._hw_failures = 0

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

    def get(self, stream_name: str, max_age: float) -> bytes | None:
        hit = self._jpegs.get(stream_name)
        if hit is None or time.monotonic() - hit[1] > max_age:
            return None
        return hit[0]

    async def _loop(self) -> None:
        while True:
            names = [n for n in get_live_cache().stream_names() if n.endswith(MAIN_SUFFIX)]
            gap = REFRESH_SECONDS / max(len(names), 1)
            if not names:
                await asyncio.sleep(REFRESH_SECONDS)
                continue
            for name in names:
                t0 = time.monotonic()
                try:
                    await self._render(name)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    logger.warning("Live poster render failed", exc_info=True,
                                   extra={"stream": name})
                await asyncio.sleep(max(gap - (time.monotonic() - t0), 0.05))

    async def _render(self, name: str) -> None:
        snap = get_live_cache().newest_keyframe(name)
        if snap is None:
            return
        init, fragment, key_at = snap
        if self._rendered_key.get(name) == key_at:
            return  # stream stalled; nothing newer to draw
        jpeg = None
        # Give up on VAAPI only after repeated failures: one can fail for
        # transient reasons (the iGPU is busy at startup) and software costs
        # about twice the CPU for every render from then on.
        if get_config().ffmpeg.hwaccel == "vaapi" and self._hw_failures < HW_FAILURE_LIMIT:
            jpeg = await self._ffmpeg(init + fragment, hw=True)
            self._hw_failures = 0 if jpeg else self._hw_failures + 1
        if jpeg is None:
            jpeg = await self._ffmpeg(init + fragment, hw=False)
        if jpeg:
            self._jpegs[name] = (jpeg, time.monotonic())
            self._rendered_key[name] = key_at

    async def _ffmpeg(self, data: bytes, hw: bool) -> bytes | None:
        if hw:
            pre = ["-hwaccel", "vaapi", "-hwaccel_device", "/dev/dri/renderD128",
                   "-hwaccel_output_format", "vaapi"]
            vf = f"scale_vaapi=w={POSTER_WIDTH}:h=-2:format=nv12,hwdownload,format=nv12"
        else:
            pre = []
            vf = f"scale={POSTER_WIDTH}:-2"
        cmd = [get_config().ffmpeg.path, "-v", "error", "-nostdin", *pre,
               "-f", "mp4", "-i", "pipe:0", "-frames:v", "1", "-vf", vf,
               "-q:v", "4", "-f", "mjpeg", "pipe:1"]
        proc = await asyncio.create_subprocess_exec(
            *cmd, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE)
        try:
            out, err = await asyncio.wait_for(proc.communicate(data), RENDER_TIMEOUT)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            return None
        if proc.returncode == 0 and out[:2] == bytes([0xFF, 0xD8]):
            return out
        logger.warning("Live poster: ffmpeg render failed", extra={
            "hw": hw, "rc": proc.returncode,
            "stderr": err.decode("utf-8", "replace")[-300:]})
        return None


_poster: LivePoster | None = None


def get_live_poster() -> LivePoster:
    global _poster
    if _poster is None:
        _poster = LivePoster()
    return _poster
