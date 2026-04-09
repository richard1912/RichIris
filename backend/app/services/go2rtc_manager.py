"""go2rtc lifecycle manager — starts/stops go2rtc as a child process."""

import asyncio
import ctypes
import logging
import subprocess
import sys
from pathlib import Path

import httpx
import yaml

from app.config import get_app_dir, get_config, resolve_binary

logger = logging.getLogger(__name__)

_process: subprocess.Popen | None = None
_log_file = None  # Keep file handle open for go2rtc's lifetime
_monitor_task: asyncio.Task | None = None
_shutting_down = False
# Actual ports assigned at startup (may differ from config defaults if ports were busy)
_api_port: int | None = None
_rtsp_port: int | None = None



def _check_port_available(port: int, label: str) -> None:
    """Check if a port is available. Logs a clear error if something else is using it."""
    import socket
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.bind(("127.0.0.1", port))
    except OSError:
        logger.error(
            "Port already in use — another process is blocking go2rtc",
            extra={"port": port, "label": label,
                   "hint": "Check for another go2rtc instance or conflicting service"},
        )


def get_api_port() -> int:
    """Return the go2rtc API port (assigned at startup)."""
    return _api_port or get_config().go2rtc.port


def get_rtsp_port() -> int:
    """Return the go2rtc RTSP port (assigned at startup)."""
    return _rtsp_port or get_config().go2rtc.rtsp_port


def _resolve_go2rtc_binary() -> str | None:
    """Find the go2rtc binary."""
    app_dir = get_app_dir()

    # Check bundled location (same path in both dev and installed layout)
    bundled = app_dir / "dependencies" / "go2rtc" / "go2rtc.exe"
    if bundled.exists():
        return str(bundled)

    # Check PATH
    import shutil
    found = shutil.which("go2rtc")
    if found:
        return found

    return None


def _get_short_path(path: str) -> str:
    """Get Windows 8.3 short path to avoid spaces (go2rtc can't handle them)."""
    if sys.platform == "win32" and " " in path:
        buf = ctypes.create_unicode_buffer(260)
        if ctypes.windll.kernel32.GetShortPathNameW(path, buf, 260) > 0:
            return buf.value
    return path


def _generate_go2rtc_config(
    binary_dir: Path, streams: dict | None = None,
    api_port: int | None = None, rtsp_port: int | None = None,
) -> Path:
    """Generate go2rtc.yaml with ffmpeg config and camera streams.

    Streams are baked into the config file so they survive go2rtc config reloads.
    go2rtc watches its config file; API-registered streams get wiped on reload.
    """
    config = get_config()
    api_port = api_port or config.go2rtc.port
    rtsp_port = rtsp_port or config.go2rtc.rtsp_port

    # Use Windows 8.3 short path for ffmpeg — go2rtc can't handle spaces in bin path
    ffmpeg_bin = _get_short_path(config.ffmpeg.path)

    go2rtc_config = {
        "api": {"listen": f":{api_port}"},
        "rtsp": {"listen": f":{rtsp_port}"},
        "ffmpeg": {
            "bin": ffmpeg_bin,
            # Use NVENC GPU encoders
            "h264": "-c:v h264_nvenc -g:v 30 -delay 0 -profile:v high -level:v auto",
            "h265": "-c:v hevc_nvenc -g:v 30 -delay 0 -profile:v main -level:v auto",
        },
    }
    if streams:
        go2rtc_config["streams"] = streams

    config_path = binary_dir / "go2rtc.yaml"
    with open(config_path, "w") as f:
        # Force block style (no inline {}) — go2rtc's YAML parser chokes on flow style
        yaml.dump(go2rtc_config, f, default_flow_style=False, default_style=None)

    logger.debug("Generated go2rtc config", extra={
        "path": str(config_path),
        "stream_count": len(streams) if streams else 0,
    })
    return config_path



def _launch_process(binary: str, config_path: Path, log_path: Path) -> subprocess.Popen:
    """Launch the go2rtc subprocess."""
    global _log_file

    binary_dir = Path(binary).parent
    cmd = [binary, "-config", str(config_path)]

    if _log_file:
        _log_file.close()
    _log_file = open(log_path, "a")

    return subprocess.Popen(
        cmd,
        stdout=_log_file,
        stderr=_log_file,
        cwd=str(binary_dir),
        creationflags=subprocess.CREATE_NEW_PROCESS_GROUP,
    )


async def _build_streams_from_db() -> dict:
    """Build go2rtc streams config from enabled cameras in DB."""
    from app.database import get_session_factory
    from app.models import Camera
    from app.services.go2rtc_client import build_streams_config
    from sqlalchemy import select

    factory = get_session_factory()
    async with factory() as session:
        result = await session.execute(select(Camera).where(Camera.enabled == True))
        cameras = result.scalars().all()

    camera_list = [(cam.name, cam.rtsp_url, cam.sub_stream_url) for cam in cameras]
    return build_streams_config(camera_list)


async def _monitor_go2rtc() -> None:
    """Watch the go2rtc process and restart it if it crashes."""
    global _process

    while not _shutting_down:
        await asyncio.sleep(5)

        if _process is None or _shutting_down:
            break

        if _process.poll() is None:
            continue  # Still running

        # go2rtc died
        returncode = _process.returncode
        logger.error("go2rtc crashed, restarting", extra={"returncode": returncode})
        _process = None

        binary = _resolve_go2rtc_binary()
        if not binary:
            logger.error("go2rtc binary not found, cannot restart")
            break

        binary_dir = Path(binary).parent

        # Bake all streams into config
        streams = await _build_streams_from_db()
        config_path = _generate_go2rtc_config(
            binary_dir, streams=streams,
            api_port=_api_port, rtsp_port=_rtsp_port,
        )

        from app.config import get_bootstrap
        log_path = Path(get_bootstrap().data_dir) / "logs" / "go2rtc.log"

        try:
            _process = _launch_process(binary, config_path, log_path)
            await asyncio.sleep(2)
            if _process.poll() is not None:
                logger.error("go2rtc failed to restart", extra={"returncode": _process.returncode})
                _process = None
                await asyncio.sleep(10)
                continue

            from app.services.go2rtc_client import notify_go2rtc_restart, get_go2rtc_client
            notify_go2rtc_restart()

            # Re-register all streams sequentially
            client = get_go2rtc_client()
            await client.register_streams_from_config(streams)

            logger.info("go2rtc restarted", extra={"pid": _process.pid, "streams": len(streams)})

        except Exception:
            logger.exception("Failed to restart go2rtc")
            _process = None
            await asyncio.sleep(10)


async def start_go2rtc(streams: dict | None = None) -> bool:
    """Start go2rtc as a child process on dynamically assigned free ports.

    Always launches our own instance — finds free ports at startup to avoid
    conflicts with any standalone go2rtc or other services.

    Args:
        streams: Stream definitions to bake into go2rtc.yaml.

    Returns True if started successfully, False on failure.
    """
    global _process, _monitor_task, _shutting_down, _api_port, _rtsp_port

    _shutting_down = False
    config = get_config()

    _api_port = config.go2rtc.port
    _rtsp_port = config.go2rtc.rtsp_port
    logger.info("go2rtc ports", extra={"api_port": _api_port, "rtsp_port": _rtsp_port})
    _check_port_available(_api_port, "go2rtc API")
    _check_port_available(_rtsp_port, "go2rtc RTSP")

    binary = _resolve_go2rtc_binary()
    if not binary:
        logger.error("go2rtc binary not found — live view will not work")
        return False

    binary_dir = Path(binary).parent
    config_path = _generate_go2rtc_config(
        binary_dir, streams=streams,
        api_port=_api_port, rtsp_port=_rtsp_port,
    )

    from app.config import get_bootstrap
    data_dir = Path(get_bootstrap().data_dir)
    log_dir = data_dir / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / "go2rtc.log"

    logger.info("Starting go2rtc", extra={"binary": binary, "log": str(log_path)})
    try:
        _process = _launch_process(binary, config_path, log_path)

        # Wait briefly and check it didn't crash immediately
        await asyncio.sleep(1)
        if _process.poll() is not None:
            logger.error("go2rtc exited immediately", extra={"returncode": _process.returncode})
            _process = None
            return False

        from app.services.go2rtc_client import notify_go2rtc_restart
        notify_go2rtc_restart()
        logger.info("go2rtc started", extra={"pid": _process.pid})

        # Start crash monitor
        _monitor_task = asyncio.create_task(_monitor_go2rtc())

        return True

    except Exception:
        logger.exception("Failed to start go2rtc")
        _process = None
        return False


async def restart_go2rtc() -> bool:
    """Restart go2rtc with fresh config from database (e.g. after camera add/remove)."""
    from app.services.go2rtc_client import reset_go2rtc_client
    streams = await _build_streams_from_db()
    await stop_go2rtc()
    reset_go2rtc_client()  # Ports may change on restart
    return await start_go2rtc(streams=streams)


async def stop_go2rtc() -> None:
    """Stop the go2rtc child process."""
    global _process, _log_file, _monitor_task, _shutting_down

    _shutting_down = True

    if _monitor_task:
        _monitor_task.cancel()
        _monitor_task = None

    if _process is None:
        return

    logger.info("Stopping go2rtc", extra={"pid": _process.pid})
    try:
        _process.terminate()
        try:
            _process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            _process.kill()
            _process.wait(timeout=3)
        logger.info("go2rtc stopped")
    except Exception:
        logger.exception("Error stopping go2rtc")
    finally:
        _process = None
        if _log_file:
            _log_file.close()
            _log_file = None


def is_managed() -> bool:
    """Return True if go2rtc was started as a managed child process."""
    return _process is not None and _process.poll() is None
