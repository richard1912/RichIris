"""Clip export service - extracts time ranges from recordings into MP4 files."""

import asyncio
import json
import logging
import math
from datetime import datetime, timedelta
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_config
from app.models import Camera, ClipExport, Recording
from app.services.ffmpeg import sanitize_camera_name
from app.services.job_object import assign_to_job

logger = logging.getLogger(__name__)

# Per-cell size for grid composites (16:9). Sources are downscaled into cells.
GRID_CELL_W = 960
GRID_CELL_H = 540
GRID_FPS = 15

def get_exports_dir() -> Path:
    """Return and ensure the exports directory exists under data_dir."""
    from app.config import get_bootstrap
    exports_dir = Path(get_bootstrap().data_dir) / "exports"
    exports_dir.mkdir(parents=True, exist_ok=True)
    return exports_dir


async def find_overlapping_segments(
    session: AsyncSession, camera_id: int, start: datetime, end: datetime
) -> list[Recording]:
    """Find all recording segments that overlap with [start, end].

    Filters by date range in SQL for efficiency and deduplicates by file_path
    to handle duplicate DB entries (same file registered twice).
    """
    # Query a generous date window to catch segments that span midnight
    query_start = start - timedelta(hours=1)
    result = await session.execute(
        select(Recording)
        .where(
            Recording.camera_id == camera_id,
            Recording.start_time >= query_start,
            Recording.start_time < end,
        )
        .order_by(Recording.start_time)
    )
    all_segs = result.scalars().all()

    # Deduplicate by file_path — keep the first (earliest start_time) entry
    seen_paths: set[str] = set()
    overlapping = []
    for seg in all_segs:
        if seg.file_path in seen_paths:
            continue
        seen_paths.add(seg.file_path)

        seg_start = seg.start_time
        if seg.end_time:
            seg_end = seg.end_time
        elif seg.duration:
            seg_end = seg_start + timedelta(seconds=seg.duration)
        else:
            seg_end = seg_start + timedelta(seconds=900)

        if seg_start < end and seg_end > start:
            overlapping.append(seg)

    return overlapping


async def _build_camera_concat(
    session: AsyncSession,
    camera_id: int,
    start: datetime,
    end: datetime,
    exports_dir: Path,
    tag: str,
) -> tuple[Path, float] | None:
    """Build a concat list file for one camera's overlapping segments.

    Returns (concat_file_path, ss_offset_seconds) or None if no footage exists.
    The caller owns the concat file and must unlink it when done.
    """
    segments = await find_overlapping_segments(session, camera_id, start, end)
    if not segments:
        return None

    concat_lines = []
    for seg in segments:
        seg_path = Path(seg.file_path)
        if seg_path.exists():
            concat_lines.append(f"file '{seg_path.as_posix()}'")
    if not concat_lines:
        return None

    concat_file = exports_dir / f"_concat_{tag}.txt"
    concat_file.write_text("\n".join(concat_lines), encoding="utf-8")
    ss_offset = max(0.0, (start - segments[0].start_time).total_seconds())
    return concat_file, ss_offset


MP4_TIMESCALE = 90000  # fine enough that VFR camera timestamps never collide


async def _probe_mp4_cadence(path: Path, config) -> tuple[str | None, int | None]:
    """Return (codec_name, 90kHz ticks per frame) for a finished MP4.

    Camera .ts streams are badly VFR — Front Door averages 12fps but delivers
    frames anywhere from 12ms to 770ms apart — and mpegts gives ffmpeg nothing
    to go on, so it guesses `r_frame_rate` (40, against a true 12). A player
    honours the jittery timestamps literally, which is the visible "keeps
    getting stuck" stutter; worse, anything downstream that re-encodes to CFR
    reads the 40 and replays the clip ~3x too fast. Knowing the real cadence
    lets the export re-time the clip with the `setts` bitstream filter, which
    rewrites packet timestamps only — the coded frames are copied untouched.

    The cadence is measured from the exported clip rather than sampled from the
    source on purpose: frame rate drifts within a recording (Front South 47
    runs 7fps in one stretch and 10fps in another), and a sample of the wrong
    stretch stretched a 10-minute clip to 14:32. MP4 keeps the frame count in
    its moov atom, so this is a metadata read, not a decode.

    Returns (codec, None) if the cadence can't be trusted; the caller then
    keeps the plain copy, exactly as before.
    """
    cmd = [
        config.ffmpeg.ffprobe_path,
        "-v", "error",
        "-select_streams", "v:0",
        "-show_entries", "stream=codec_name,nb_frames",
        "-show_entries", "format=duration",
        "-of", "default=nw=1",
        str(path),
    ]
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        out, _ = await proc.communicate()
    except OSError:
        logger.warning("Clip cadence probe could not run", extra={"file": str(path)})
        return None, None
    if proc.returncode != 0:
        return None, None

    # Parse by key rather than position — ffprobe emits stream fields in its own
    # struct order, not the order they were asked for.
    fields: dict[str, str] = {}
    for line in out.decode(errors="replace").splitlines():
        key, _, value = line.strip().partition("=")
        if value:
            fields.setdefault(key, value)
    codec = fields.get("codec_name")
    try:
        frames = int(fields["nb_frames"])
        duration = float(fields["duration"])
    except (KeyError, ValueError):
        return codec, None
    if frames < 2 or duration <= 0:
        return codec, None

    fps = frames / duration
    if not 1.0 <= fps <= 60.0:
        logger.warning(
            "Clip cadence probe gave an implausible rate, leaving timestamps as-is",
            extra={"file": str(path), "fps": round(fps, 3)},
        )
        return codec, None
    return codec, max(1, round(MP4_TIMESCALE * duration / frames))


async def _run_ffmpeg(cmd: list[str]) -> tuple[int, str]:
    """Run an ffmpeg command, returning (returncode, last_stderr_tail)."""
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    assign_to_job(proc.pid)
    _, stderr = await proc.communicate()
    return proc.returncode, stderr.decode(errors="replace")[-800:]


async def export_grid_clip(clip_id: int, session_factory) -> None:
    """Export a synchronized side-by-side grid composite from several cameras.

    Each selected camera is downscaled into a cell of a tiled canvas; all cells
    share the same time window so the footage plays back in sync. Cameras with
    no footage in the window render as black cells so the grid stays consistent
    with the user's selection. Re-encodes to HEVC (NVENC, with libx264 fallback).
    """
    config = get_config()

    async with session_factory() as session:
        clip = await session.get(ClipExport, clip_id)
        if not clip:
            logger.error("Clip not found", extra={"clip_id": clip_id})
            return

        clip.status = "processing"
        await session.commit()

        concat_files: list[Path] = []
        try:
            camera_ids: list[int] = json.loads(clip.camera_ids or "[]")
            if not camera_ids:
                clip.status = "failed"
                await session.commit()
                logger.warning("Grid clip has no cameras", extra={"clip_id": clip_id})
                return

            exports_dir = get_exports_dir()
            clip_duration = (clip.end_time - clip.start_time).total_seconds()

            # Build one cell per selected camera (real footage or black placeholder)
            cells = []  # each: {"concat": Path|None, "ss": float, "name": str}
            for idx, cam_id in enumerate(camera_ids):
                camera = await session.get(Camera, cam_id)
                cam_name = camera.name if camera else f"Camera {cam_id}"
                prepared = await _build_camera_concat(
                    session, cam_id, clip.start_time, clip.end_time,
                    exports_dir, f"{clip_id}_{idx}",
                )
                if prepared:
                    concat_files.append(prepared[0])
                    cells.append({"concat": prepared[0], "ss": prepared[1], "name": cam_name})
                else:
                    cells.append({"concat": None, "ss": 0.0, "name": cam_name})

            if all(c["concat"] is None for c in cells):
                clip.status = "failed"
                await session.commit()
                logger.warning("No footage for any grid camera", extra={"clip_id": clip_id})
                return

            n = len(cells)
            cols = math.ceil(math.sqrt(n))
            rows = math.ceil(n / cols)
            canvas_w = cols * GRID_CELL_W
            canvas_h = rows * GRID_CELL_H

            # Input 0 is the black base canvas; cells follow.
            cmd_inputs: list[str] = [
                "-f", "lavfi", "-t", f"{clip_duration:.3f}",
                "-i", f"color=c=black:s={canvas_w}x{canvas_h}:r={GRID_FPS}",
            ]
            filters: list[str] = []
            overlay_chain = "[0:v]"
            input_index = 1
            for k, cell in enumerate(cells):
                if cell["concat"] is not None:
                    cmd_inputs += [
                        "-f", "concat", "-safe", "0",
                        "-ss", f"{cell['ss']:.3f}",
                        "-i", str(cell["concat"]),
                    ]
                    filters.append(
                        f"[{input_index}:v]scale={GRID_CELL_W}:{GRID_CELL_H}:"
                        f"force_original_aspect_ratio=decrease,"
                        f"pad={GRID_CELL_W}:{GRID_CELL_H}:(ow-iw)/2:(oh-ih)/2,"
                        f"setsar=1,fps={GRID_FPS}[c{k}]"
                    )
                else:
                    cmd_inputs += [
                        "-f", "lavfi", "-t", f"{clip_duration:.3f}",
                        "-i", f"color=c=black:s={GRID_CELL_W}x{GRID_CELL_H}:r={GRID_FPS}",
                    ]
                    filters.append(f"[{input_index}:v]setsar=1[c{k}]")
                col = k % cols
                row = k // cols
                x = col * GRID_CELL_W
                y = row * GRID_CELL_H
                out_label = "[bg]" if k == n - 1 else f"[s{k}]"
                filters.append(f"{overlay_chain}[c{k}]overlay={x}:{y}{out_label}")
                overlay_chain = out_label
                input_index += 1
            hwaccel = config.ffmpeg.hwaccel
            # Composite always happens in software; the sink filter differs:
            # vaapi uploads the final frames for GPU encode, everything else
            # (nvenc/x264) takes yuv420p directly.
            filter_complex_sw = ";".join(filters + ["[bg]format=yuv420p[out]"])
            filter_complex_hw = ";".join(filters + ["[bg]format=nv12,hwupload[out]"])

            date_str = clip.start_time.strftime("%Y-%m-%d")
            start_str = clip.start_time.strftime("%H.%M")
            end_str = clip.end_time.strftime("%H.%M")
            output_file = exports_dir / f"Grid {len(camera_ids)}cam {date_str} {start_str} - {end_str}.mp4"

            def _build_cmd(filter_graph: str, device_args: list[str]) -> list[str]:
                return [
                    config.ffmpeg.path, "-y",
                    *device_args,
                    *cmd_inputs,
                    "-filter_complex", filter_graph,
                    "-map", "[out]",
                    "-t", f"{clip_duration:.3f}",
                    "-an",
                ]

            if hwaccel == "cuda":
                hw_cmd = _build_cmd(filter_complex_sw, []) + [
                    "-c:v", "hevc_nvenc", "-preset", "p4", "-rc", "vbr", "-cq", "26",
                    "-tag:v", "hvc1",
                    "-movflags", "+faststart", str(output_file),
                ]
            elif hwaccel == "vaapi":
                hw_cmd = _build_cmd(
                    filter_complex_hw,
                    ["-init_hw_device", "vaapi=va:/dev/dri/renderD128", "-filter_hw_device", "va"],
                ) + [
                    "-c:v", "hevc_vaapi", "-rc_mode", "CQP", "-qp", "26",
                    "-tag:v", "hvc1",
                    "-movflags", "+faststart", str(output_file),
                ]
            else:
                hw_cmd = None
            x264_cmd = _build_cmd(filter_complex_sw, []) + [
                "-c:v", "libx264", "-preset", "veryfast", "-crf", "26", "-pix_fmt", "yuv420p",
                "-movflags", "+faststart", str(output_file),
            ]

            logger.info(
                "Starting grid clip export",
                extra={"clip_id": clip_id, "cameras": n, "grid": f"{cols}x{rows}", "output": str(output_file)},
            )

            if hw_cmd is not None:
                rc, err = await _run_ffmpeg(hw_cmd)
                if rc != 0:
                    logger.warning(
                        "Hardware grid export failed, retrying with libx264",
                        extra={"clip_id": clip_id, "hwaccel": hwaccel, "stderr": err},
                    )
                    rc, err = await _run_ffmpeg(x264_cmd)
            else:
                rc, err = await _run_ffmpeg(x264_cmd)

            if rc != 0:
                logger.error("Grid clip export failed", extra={"clip_id": clip_id, "stderr": err})
                clip.status = "failed"
                await session.commit()
                return

            clip.status = "done"
            clip.file_path = str(output_file)
            await session.commit()
            logger.info("Grid clip export completed", extra={"clip_id": clip_id, "file": str(output_file)})

        except Exception:
            logger.exception("Grid clip export error", extra={"clip_id": clip_id})
            clip.status = "failed"
            await session.commit()
        finally:
            for cf in concat_files:
                cf.unlink(missing_ok=True)


async def export_clip(clip_id: int, session_factory) -> None:
    """Run the ffmpeg export for a clip. Called as a background task."""
    config = get_config()

    async with session_factory() as session:
        clip = await session.get(ClipExport, clip_id)
        if not clip:
            logger.error("Clip not found", extra={"clip_id": clip_id})
            return

        clip.status = "processing"
        await session.commit()

        try:
            segments = await find_overlapping_segments(
                session, clip.camera_id, clip.start_time, clip.end_time
            )

            if not segments:
                clip.status = "failed"
                await session.commit()
                logger.warning("No segments found for clip", extra={"clip_id": clip_id})
                return

            exports_dir = get_exports_dir()
            camera = await session.get(Camera, clip.camera_id)
            cam_name = camera.name if camera else f"Camera {clip.camera_id}"
            date_str = clip.start_time.strftime("%Y-%m-%d")
            start_str = clip.start_time.strftime("%H.%M")
            end_str = clip.end_time.strftime("%H.%M")
            output_file = exports_dir / f"{cam_name} {date_str} {start_str} - {end_str}.mp4"

            # Build concat file
            concat_lines = []
            for seg in segments:
                seg_path = Path(seg.file_path)
                if seg_path.exists():
                    concat_lines.append(f"file '{seg_path.as_posix()}'")

            if not concat_lines:
                clip.status = "failed"
                await session.commit()
                logger.warning("No segment files on disk", extra={"clip_id": clip_id})
                return

            concat_file = exports_dir / f"_concat_{clip_id}.txt"
            concat_file.write_text("\n".join(concat_lines), encoding="utf-8")

            try:
                # Trim offsets relative to concatenated stream
                ss_offset = max(0, (clip.start_time - segments[0].start_time).total_seconds())
                clip_duration = (clip.end_time - clip.start_time).total_seconds()

                # Pass 1: window out the requested range with a plain copy.
                # Output-side -ss is deliberate — input-side seek on the concat
                # demuxer does not honour -t here (a 60s request produced 649s).
                staged_file = exports_dir / f"_staged_{clip_id}.mp4"
                stage_cmd = [
                    config.ffmpeg.path,
                    "-y",
                    "-f", "concat",
                    "-safe", "0",
                    "-i", str(concat_file),
                    "-ss", f"{ss_offset:.3f}",
                    "-t", f"{clip_duration:.3f}",
                    "-c", "copy",
                    "-avoid_negative_ts", "make_zero",
                    "-video_track_timescale", str(MP4_TIMESCALE),
                    str(staged_file),
                ]

                logger.info(
                    "Starting clip export",
                    extra={"clip_id": clip_id, "segments": len(segments), "output": str(output_file)},
                )

                rc, err = await _run_ffmpeg(stage_cmd)
                if rc != 0:
                    logger.error(
                        "FFmpeg clip export failed",
                        extra={"clip_id": clip_id, "stderr": err},
                    )
                    clip.status = "failed"
                    await session.commit()
                    return

                # Pass 2: re-time to a constant cadence measured off pass 1, so
                # the clip plays smoothly and downstream transcodes read the real
                # frame rate instead of ffmpeg's mpegts guess. Still a copy.
                codec, frame_ticks = await _probe_mp4_cadence(staged_file, config)
                retime_cmd = [
                    config.ffmpeg.path, "-y", "-i", str(staged_file), "-c", "copy",
                ]
                if frame_ticks:
                    retime_cmd += ["-bsf:v", f"setts=ts=N*{frame_ticks}"]
                if codec == "hevc":
                    # `hev1` (what mpegts copies out as) is rejected by Safari and
                    # by Chrome without a fallback; `hvc1` is the interoperable tag.
                    retime_cmd += ["-tag:v", "hvc1"]
                retime_cmd += [
                    "-video_track_timescale", str(MP4_TIMESCALE),
                    "-movflags", "+faststart",
                    str(output_file),
                ]

                rc, err = await _run_ffmpeg(retime_cmd)
                if rc != 0:
                    # Never lose a finished clip over the cosmetic pass — keep the
                    # staged copy under the final name and carry on.
                    logger.warning(
                        "Clip re-time pass failed, keeping the raw copy",
                        extra={"clip_id": clip_id, "stderr": err},
                    )
                    output_file.unlink(missing_ok=True)
                    staged_file.replace(output_file)
                else:
                    logger.info(
                        "Clip re-timed to constant cadence",
                        extra={
                            "clip_id": clip_id,
                            "codec": codec,
                            "fps": round(MP4_TIMESCALE / frame_ticks, 2) if frame_ticks else None,
                        },
                    )

                clip.status = "done"
                clip.file_path = str(output_file)
                await session.commit()
                logger.info("Clip export completed", extra={"clip_id": clip_id, "file": str(output_file)})

            finally:
                concat_file.unlink(missing_ok=True)
                (exports_dir / f"_staged_{clip_id}.mp4").unlink(missing_ok=True)

        except Exception:
            logger.exception("Clip export error", extra={"clip_id": clip_id})
            clip.status = "failed"
            await session.commit()
