"""Server-rendered reverse playback.

Renders a recording segment *backwards* into a fragmented MP4 that a client
simply plays forward: the picture runs from ``end_offset`` back to the start of
the segment. The client only needs ``setRate(|speed|)``, which is the same
thing it does for 1x-4x, so reverse works identically on mpv (Windows/Android)
and on a browser ``<video>``.

Why not decode backwards on the client: mpv's backward playback is
experimental and needs a fully indexed seekable file, which the fMP4 proxy
(``frag_keyframe+empty_moov``, no sidx) is not; the previous approach of
seeking a paused player backwards every 500ms stalled or froze on every
platform.

Why chunks: ffmpeg's ``reverse`` filter buffers *every* decoded frame of its
input in memory, so a 15-minute 4K segment is impossible in one pass. The
renderer instead walks backwards from ``end_offset`` in ``CHUNK_SECONDS``
pieces: each chunk is decoded (hardware where available), downscaled and
rate-limited on the GPU *before* it is buffered, reversed, and encoded to
raw Annex-B H.264. Chunk output is concatenated in reverse-chronological order
into one muxing ffmpeg that stamps constant ``OUT_FPS`` timestamps - so no
per-chunk timestamp offsets, no non-monotonic DTS, and the stitched stream is
a single continuous video.

Measured on the box (i5-8600 + UHD 630, 4K HEVC 15fps): a 10s chunk renders in
~1.3s, i.e. ~8x realtime, comfortably ahead of the -4x maximum.
"""

from __future__ import annotations

import asyncio
import logging
import time
from pathlib import Path

from app.services.job_object import assign_to_job

logger = logging.getLogger(__name__)

CHUNK_SECONDS = 8.0
OUT_FPS = 10
OUT_WIDTH = 1280
# How many chunk encoders may run at once (each x264 capped at 2 threads, so
# 4 cores worst case). Two keeps the pipeline ahead of
# playback without hogging the iGPU that live transcodes share.
CONCURRENCY = 2
CHUNK_TIMEOUT = 60.0
# Render only this far ahead of what the client has actually read. Without
# a bound every session eagerly rendered the whole rest of the segment
# (up to 15 min, ~2 min of two busy cores + the iGPU) even when the user
# tapped a different speed two seconds later. ~24 MB is about 90 s of the
# 720p/10fps output, plenty of runway at -4x; an unread session stalls
# here and the 30 s idle sweep then tears it down.
MAX_LEAD_BYTES = 24 * 1024 * 1024


def _chunk_plan(end_offset: float) -> list[tuple[float, float]]:
    """(seek, duration) pairs covering [0, end_offset], latest first."""
    plan: list[tuple[float, float]] = []
    hi = max(0.0, end_offset)
    while hi > 0.05:
        lo = max(0.0, hi - CHUNK_SECONDS)
        plan.append((lo, hi - lo))
        hi = lo
    return plan


def _decode_args(hwaccel: str, use_hw: bool) -> tuple[list[str], str]:
    """Pre-input args and the video filter chain for one chunk.

    Full-GPU path on VAAPI: decode + fps drop + scale happen on the iGPU and
    only 720p NV12 is downloaded to system memory (measured 2x faster than
    downloading 4K and scaling in software).
    """
    reverse = f"reverse,format=yuv420p"
    if use_hw and hwaccel == "vaapi":
        pre = ["-hwaccel", "vaapi", "-hwaccel_device", "/dev/dri/renderD128",
               "-hwaccel_output_format", "vaapi"]
        vf = (f"fps={OUT_FPS},scale_vaapi=w={OUT_WIDTH}:h=-2:format=nv12,"
              f"hwdownload,format=nv12,{reverse}")
        return pre, vf
    if use_hw and hwaccel == "cuda":
        pre = ["-hwaccel", "cuda"]
        return pre, f"fps={OUT_FPS},scale={OUT_WIDTH}:-2,{reverse}"
    return [], f"fps={OUT_FPS},scale={OUT_WIDTH}:-2,{reverse}"


class ReverseRenderer:
    """Drives the chunk encoders and the muxer for one playback session."""

    def __init__(
        self, *, ffmpeg: str, hwaccel: str, source: str,
        end_offset: float, output_path: Path,
    ) -> None:
        self._ffmpeg = ffmpeg
        self._hwaccel = hwaccel
        self._source = source
        self._end_offset = end_offset
        self._output_path = output_path
        self._plan = _chunk_plan(end_offset)
        self._procs: set[asyncio.subprocess.Process] = set()
        self._stopped = False
        self.mux: asyncio.subprocess.Process | None = None
        self.task: asyncio.Task | None = None
        self.chunks_done = 0
        self.error: str | None = None
        self.bytes_written = 0
        # Updated by the playback.mp4 endpoint as it streams to the client.
        self.bytes_consumed = 0

    @property
    def chunk_count(self) -> int:
        return len(self._plan)

    async def start(self) -> asyncio.subprocess.Process:
        """Spawn the muxer and kick off chunk rendering; returns the muxer."""
        self.mux = await asyncio.create_subprocess_exec(
            self._ffmpeg, "-y", "-v", "error",
            "-fflags", "+genpts",
            "-r", str(OUT_FPS), "-f", "h264", "-i", "pipe:0",
            "-c:v", "copy", "-an",
            "-movflags", "frag_keyframe+empty_moov+default_base_moof",
            "-f", "mp4", str(self._output_path),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
        assign_to_job(self.mux.pid)
        self.task = asyncio.create_task(self._run())
        return self.mux

    async def _wait_for_headroom(self) -> None:
        """Block while the client is more than MAX_LEAD_BYTES behind."""
        while not self._stopped and                 self.bytes_written - self.bytes_consumed > MAX_LEAD_BYTES:
            await asyncio.sleep(0.25)

    async def _run(self) -> None:
        started = time.monotonic()
        # A sliding window of CONCURRENCY chunk tasks: the next chunk is
        # only launched once there is headroom, so rendering paces itself
        # to consumption instead of racing to the end of the segment.
        pending: list[asyncio.Task] = []
        try:
            next_index = 0
            while next_index < len(self._plan) or pending:
                while len(pending) < CONCURRENCY and next_index < len(self._plan):
                    await self._wait_for_headroom()
                    if self._stopped:
                        break
                    ss, t = self._plan[next_index]
                    pending.append(asyncio.create_task(
                        self._render_chunk(next_index, ss, t)))
                    next_index += 1
                if self._stopped or not pending:
                    break
                data = await pending.pop(0)
                if self._stopped:
                    break
                if data:
                    self.mux.stdin.write(data)
                    await self.mux.stdin.drain()
                    self.bytes_written += len(data)
                self.chunks_done += 1
        except (asyncio.CancelledError, BrokenPipeError, ConnectionResetError):
            pass
        except Exception as exc:  # noqa: BLE001
            self.error = str(exc)
            logger.exception("Reverse render failed", extra={"source": self._source})
        finally:
            for t in pending:
                t.cancel()
            if self.mux and self.mux.stdin and not self.mux.stdin.is_closing():
                try:
                    self.mux.stdin.close()
                except Exception:  # noqa: BLE001
                    pass
            logger.info(
                "Reverse render finished",
                extra={"source": self._source, "chunks": self.chunks_done,
                       "of": len(self._plan), "seconds": round(time.monotonic() - started, 1),
                       "stopped": self._stopped},
            )

    async def _render_chunk(self, index: int, seek: float, duration: float) -> bytes:
        if self._stopped:
            return b""
        data, err = await self._encode(seek, duration, use_hw=True)
        if data or self._stopped:
            return data
        # A seek that lands mid-GOP can make the hardware decoder bail
        # ("Could not find ref with POC"); software decode tolerates it.
        logger.warning(
            "Reverse chunk failed on hw decode, retrying in software",
            extra={"index": index, "seek": seek, "stderr": err[-300:]},
        )
        data, err = await self._encode(seek, duration, use_hw=False)
        if not data and not self._stopped:
            logger.error("Reverse chunk failed", extra={"index": index, "seek": seek,
                                                        "stderr": err[-300:]})
        return data

    async def _encode(self, seek: float, duration: float, *, use_hw: bool) -> tuple[bytes, str]:
        pre, vf = _decode_args(self._hwaccel, use_hw)
        cmd = [
            self._ffmpeg, "-v", "error", "-nostdin",
            *pre,
            "-ss", f"{seek:.3f}", "-t", f"{duration:.3f}",
            "-i", self._source,
            "-an", "-sn", "-dn",
            "-vf", vf,
            "-c:v", "libx264", "-preset", "veryfast", "-tune", "zerolatency", "-threads", "2",
            "-g", str(OUT_FPS * 2), "-bf", "0",
            "-crf", "26", "-maxrate", "3M", "-bufsize", "3M",
            "-f", "h264", "pipe:1",
        ]
        proc = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        assign_to_job(proc.pid)
        self._procs.add(proc)
        try:
            out, err = await asyncio.wait_for(proc.communicate(), timeout=CHUNK_TIMEOUT)
        except asyncio.TimeoutError:
            proc.kill()
            return b"", "timeout"
        finally:
            self._procs.discard(proc)
        if proc.returncode != 0:
            return b"", err.decode("utf-8", errors="replace")
        return out, err.decode("utf-8", errors="replace")

    def stop(self) -> None:
        self._stopped = True
        if self.task:
            self.task.cancel()
        for proc in list(self._procs):
            if proc.returncode is None:
                proc.kill()
        if self.mux and self.mux.returncode is None:
            self.mux.kill()
