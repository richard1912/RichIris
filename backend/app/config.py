"""Application configuration — bootstrap.yaml + DB-backed settings."""

import logging
import shutil
import sys
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import yaml

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# App directory resolution
# ---------------------------------------------------------------------------

def get_app_dir() -> Path:
    """Return the application root directory.

    PyInstaller frozen: directory containing the exe (not _MEIPASS, which is
    the temp extraction folder — dependencies/ and bootstrap.yaml live next
    to the exe, not inside the temp dir).
    Development: repo root (3 levels up from this file).
    """
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent.parent


# ---------------------------------------------------------------------------
# Bootstrap config (tiny YAML: just data_dir + port)
# ---------------------------------------------------------------------------

@dataclass
class BootstrapConfig:
    data_dir: str = ""
    port: int = 8700


def _default_data_dir() -> str:
    """Platform-appropriate default data directory."""
    import os
    if os.name == "nt":
        return str(Path(os.environ.get("PROGRAMDATA", "C:/ProgramData")) / "RichIris")
    return str(Path.home() / ".richiris")


def load_bootstrap() -> BootstrapConfig:
    """Load bootstrap.yaml from app_dir. Creates it with defaults if missing."""
    app_dir = get_app_dir()
    bootstrap_path = app_dir / "bootstrap.yaml"

    if bootstrap_path.exists():
        try:
            with open(bootstrap_path, "r") as f:
                data = yaml.safe_load(f) or {}
            cfg = BootstrapConfig(
                data_dir=data.get("data_dir", _default_data_dir()),
                port=data.get("port", 8700),
            )
            logger.debug("Loaded bootstrap config", extra={"path": str(bootstrap_path)})
            return cfg
        except Exception:
            logger.exception("Failed to load bootstrap.yaml, using defaults")

    # Fall back: check for legacy config.yaml at the app dir
    legacy_path = app_dir / "config.yaml"
    if legacy_path.exists():
        try:
            with open(legacy_path, "r") as f:
                data = yaml.safe_load(f) or {}
            server = data.get("server", {})
            storage = data.get("storage", {})
            # Derive data_dir from legacy database_url if available
            db_url = storage.get("database_url", "")
            data_dir = _default_data_dir()
            if "///" in db_url:
                db_path = Path(db_url.split("///", 1)[1])
                data_dir = str(db_path.parent)
            cfg = BootstrapConfig(
                data_dir=data_dir,
                port=server.get("port", 8700),
            )
            logger.info("Using legacy config.yaml for bootstrap values", extra={"path": str(legacy_path)})
            return cfg
        except Exception:
            logger.exception("Failed to parse legacy config.yaml")

    # Create default bootstrap.yaml
    default_dir = _default_data_dir()
    cfg = BootstrapConfig(data_dir=default_dir, port=8700)
    try:
        with open(bootstrap_path, "w") as f:
            yaml.dump({"data_dir": default_dir, "port": 8700}, f, default_flow_style=False)
        logger.info("Created default bootstrap.yaml", extra={"path": str(bootstrap_path)})
    except Exception:
        logger.warning("Could not write bootstrap.yaml", extra={"path": str(bootstrap_path)})
    return cfg


# ---------------------------------------------------------------------------
# Binary resolution (bundled → PATH → DB setting)
# ---------------------------------------------------------------------------

def resolve_binary(name: str) -> str:
    """Find a binary: bundled dependencies/ → system PATH → bare name fallback."""
    # Check bundled dependencies
    app_dir = get_app_dir()
    bundled = app_dir / "dependencies" / name
    if bundled.exists():
        logger.debug("Resolved binary from bundled dependencies", extra={"name": name, "path": str(bundled)})
        return str(bundled)

    # Check system PATH
    found = shutil.which(name)
    if found:
        logger.debug("Resolved binary from PATH", extra={"name": name, "path": found})
        return found

    # Fallback: return bare name (will fail at runtime with a clear error)
    logger.debug("Binary not found, using bare name", extra={"name": name})
    return name


# ---------------------------------------------------------------------------
# Config dataclasses
# ---------------------------------------------------------------------------

@dataclass
class ServerConfig:
    host: str = "0.0.0.0"
    port: int = 8700


@dataclass
class StorageConfig:
    recordings_dir: str = ""        # HOT tier (= data_dir/recordings): recorder writes here
    thumbnails_dir: str = ""
    database_url: str = ""
    # Two-tier storage (archive). archive_recordings_dir is where the background
    # flusher moves finalized segments. When two-tier is off (or archive == hot)
    # it equals recordings_dir, making the flusher a no-op.
    archive_recordings_dir: str = ""
    two_tier_enabled: bool = False
    hot_retention_minutes: int = 60
    hot_max_gb: int = 0


# NVENC on Windows (NVIDIA), VAAPI elsewhere (Intel iGPU on the Debian box)
DEFAULT_HWACCEL = "cuda" if sys.platform == "win32" else "vaapi"


@dataclass
class FFmpegConfig:
    path: str = ""
    ffprobe_path: str = ""
    hwaccel: str = DEFAULT_HWACCEL
    segment_duration: int = 900
    rtsp_transport: str = "tcp"
    rtsp_timeout_us: int = 30_000_000


@dataclass
class Go2rtcConfig:
    host: str = "localhost"
    port: int = 18700      # Unique port — avoids conflict with standalone go2rtc (default 1984)
    rtsp_port: int = 18554  # Unique port — avoids conflict with standalone go2rtc (default 8554)


@dataclass
class RetentionConfig:
    max_age_days: int = 30
    max_storage_gb: int = 500


@dataclass
class TrickplayConfig:
    enabled: bool = True
    interval: int = 1
    thumb_width: int = 384
    thumb_height: int = 216


@dataclass
class LoggingConfig:
    level: str = "DEBUG"
    json_output: bool = False
    timezone: str = "UTC"


@dataclass
class AIConfig:
    # Remote inference server (e.g. http://192.168.8.11:8701 — the Windows RTX
    # 4080 box). Empty = local in-process ONNX inference.
    remote_url: str = ""
    remote_timeout_ms: int = 2500
    # Global kill switch for ALL face work: SCRFD detection, ArcFace embedding
    # and the background clusterer. The per-camera `face_recognition` flag only
    # chooses recognition vs detect-only — SCRFD still runs on every person
    # event when it is off — so this is the only way to stop the work entirely.
    # Defaults off so it survives a settings wipe; set `ai.face_enabled` to
    # true in settings to turn face detection back on.
    face_enabled: bool = False
    # Crop a square region around the motion and detect on that, instead of
    # letterboxing the whole frame (see object_detector.compute_motion_region).
    # Costs the same per inference; markedly better on small/distant objects.
    region_crop_enabled: bool = True
    # Filename of the local detection model to prefer, from dependencies/models.
    # Empty = use the built-in _MODEL_FILENAMES priority order. Measured on the
    # i5-8600 against rtdetr-l as reference (recall with region cropping):
    #   yolo11n-320  20 ms  51%   <- default; fastest
    #   yolo11s-320  58 ms  60%
    #   yolo11m-320 169 ms  71%
    # Speed and accuracy trade directly here; pick per what the box has spare.
    local_model: str = ""


@dataclass
class CameraConfig:
    name: str = ""
    rtsp_url: str = ""
    sub_stream_url: str = ""
    enabled: bool = True
    rotation: int = 0


@dataclass
class AppConfig:
    server: ServerConfig = field(default_factory=ServerConfig)
    storage: StorageConfig = field(default_factory=StorageConfig)
    ffmpeg: FFmpegConfig = field(default_factory=FFmpegConfig)
    go2rtc: Go2rtcConfig = field(default_factory=Go2rtcConfig)
    retention: RetentionConfig = field(default_factory=RetentionConfig)
    trickplay: TrickplayConfig = field(default_factory=TrickplayConfig)
    logging: LoggingConfig = field(default_factory=LoggingConfig)
    ai: AIConfig = field(default_factory=AIConfig)
    cameras: list[CameraConfig] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Config loading
# ---------------------------------------------------------------------------

def _populate_from_bootstrap(config: AppConfig, bootstrap: BootstrapConfig) -> None:
    """Set config fields derived from bootstrap values."""
    config.server.port = bootstrap.port

    data_dir = Path(bootstrap.data_dir)

    # Database lives in {data_dir}/database/
    db_dir = data_dir / "database"
    db_dir.mkdir(parents=True, exist_ok=True)

    # Auto-migrate: move old richiris.db from data_dir root into database/ subdir
    old_db = data_dir / "richiris.db"
    new_db = db_dir / "richiris.db"
    if old_db.exists() and not new_db.exists():
        import shutil as _shutil
        _shutil.move(str(old_db), str(new_db))
        logger.info("Migrated database to database/ subdirectory",
                     extra={"from": str(old_db), "to": str(new_db)})

    config.storage.database_url = f"sqlite+aiosqlite:///{new_db}"

    # Recordings always live under {data_dir}/recordings/
    config.storage.recordings_dir = str(data_dir / "recordings")

    # Thumbnails are always under {data_dir}/thumbnails/
    config.storage.thumbnails_dir = str(data_dir / "thumbnails")

    # Archive defaults to the hot recordings dir until DB settings enable two-tier.
    config.storage.archive_recordings_dir = config.storage.recordings_dir

    # Resolve binaries
    if not config.ffmpeg.path or config.ffmpeg.path == "ffmpeg":
        config.ffmpeg.path = resolve_binary("ffmpeg.exe" if sys.platform == "win32" else "ffmpeg")
    if not config.ffmpeg.ffprobe_path or config.ffmpeg.ffprobe_path == "ffprobe":
        config.ffmpeg.ffprobe_path = resolve_binary("ffprobe.exe" if sys.platform == "win32" else "ffprobe")


def _apply_db_settings(config: AppConfig, settings: dict[str, str]) -> None:
    """Apply DB settings dict onto AppConfig fields."""
    def _get(key: str, default: str = "") -> str:
        return settings.get(key, default)

    def _get_int(key: str, default: int = 0) -> int:
        try:
            return int(settings[key])
        except (KeyError, ValueError):
            return default

    def _get_bool(key: str, default: bool = False) -> bool:
        v = settings.get(key, "").lower()
        if v in ("true", "1", "yes"):
            return True
        if v in ("false", "0", "no"):
            return False
        return default

    # FFmpeg (path/ffprobe_path auto-resolved from dependencies/ — not user-configurable)
    config.ffmpeg.hwaccel = _get("ffmpeg.hwaccel") or config.ffmpeg.hwaccel
    config.ffmpeg.segment_duration = _get_int("ffmpeg.segment_duration", config.ffmpeg.segment_duration)
    config.ffmpeg.rtsp_transport = _get("ffmpeg.rtsp_transport") or config.ffmpeg.rtsp_transport

    # go2rtc (host/port hardcoded — managed child process, not user-configurable)

    # Retention
    config.retention.max_age_days = _get_int("retention.max_age_days", config.retention.max_age_days)
    config.retention.max_storage_gb = _get_int("retention.max_storage_gb", config.retention.max_storage_gb)

    # Trickplay
    config.trickplay.enabled = _get_bool("trickplay.enabled", config.trickplay.enabled)
    config.trickplay.interval = _get_int("trickplay.interval", config.trickplay.interval)
    config.trickplay.thumb_width = _get_int("trickplay.thumb_width", config.trickplay.thumb_width)
    config.trickplay.thumb_height = _get_int("trickplay.thumb_height", config.trickplay.thumb_height)

    # Logging
    config.logging.level = _get("logging.level") or config.logging.level
    config.logging.json_output = _get_bool("logging.json_output", config.logging.json_output)
    config.logging.timezone = _get("logging.timezone") or config.logging.timezone

    # AI (remote inference)
    config.ai.remote_url = _get("ai.remote_url").strip() or config.ai.remote_url
    config.ai.remote_timeout_ms = _get_int("ai.remote_timeout_ms", config.ai.remote_timeout_ms)
    config.ai.face_enabled = _get_bool("ai.face_enabled", config.ai.face_enabled)
    config.ai.region_crop_enabled = _get_bool("ai.region_crop_enabled", config.ai.region_crop_enabled)
    config.ai.local_model = _get("ai.local_model").strip() or config.ai.local_model

    # Storage (two-tier). recordings_dir is the HOT tier (= data_dir/recordings),
    # set by _populate_from_bootstrap and left untouched here. archive_dir is the
    # root of the archive tier; finalized segments are flushed to
    # {archive_dir}/recordings. Two-tier is active only when enabled AND a distinct
    # archive_dir is given — otherwise archive == hot and the flusher is a no-op.
    config.storage.two_tier_enabled = _get_bool("storage.two_tier_enabled", config.storage.two_tier_enabled)
    config.storage.hot_retention_minutes = _get_int("storage.hot_retention_minutes", config.storage.hot_retention_minutes)
    config.storage.hot_max_gb = _get_int("storage.hot_max_gb", config.storage.hot_max_gb)

    archive_dir = _get("storage.archive_dir").strip()
    archive_recordings = config.storage.recordings_dir  # default: single-tier
    if config.storage.two_tier_enabled and archive_dir:
        candidate = str(Path(archive_dir) / "recordings")
        try:
            same = Path(candidate).resolve() == Path(config.storage.recordings_dir).resolve()
        except Exception:
            same = candidate == config.storage.recordings_dir
        if not same:
            archive_recordings = candidate
    config.storage.archive_recordings_dir = archive_recordings


def validate_paths(config: AppConfig) -> None:
    """Ensure required directories exist."""
    dirs = [
        ("recordings_dir", config.storage.recordings_dir),
        ("thumbnails_dir", config.storage.thumbnails_dir),
    ]
    # Only create the archive dir when it's a distinct, active two-tier target;
    # don't touch a (possibly offline) archive drive otherwise.
    if (
        config.storage.two_tier_enabled
        and config.storage.archive_recordings_dir
        and config.storage.archive_recordings_dir != config.storage.recordings_dir
    ):
        dirs.append(("archive_recordings_dir", config.storage.archive_recordings_dir))

    for dir_name, path_str in dirs:
        if path_str:
            p = Path(path_str)
            try:
                p.mkdir(parents=True, exist_ok=True)
                logger.debug("Ensured directory exists", extra={"dir_name": dir_name, "path": str(p)})
            except Exception:
                # Archive drive may be offline — flusher handles this as degraded mode.
                logger.warning("Could not ensure directory", extra={"dir_name": dir_name, "path": str(p)})


# ---------------------------------------------------------------------------
# Legacy config.yaml migration
# ---------------------------------------------------------------------------

async def migrate_legacy_config(session) -> None:
    """One-time migration: read config.yaml values into the DB settings table."""
    from app.services.settings import get_setting, set_setting

    app_dir = get_app_dir()
    legacy_path = app_dir / "config.yaml"
    if not legacy_path.exists():
        return

    # Check if we already migrated
    marker = await get_setting(session, "_migrated_from_yaml")
    if marker:
        return

    try:
        with open(legacy_path, "r") as f:
            data = yaml.safe_load(f) or {}
    except Exception:
        logger.exception("Failed to read legacy config.yaml for migration")
        return

    # Map yaml sections to settings keys
    mappings = {
        "ffmpeg": ["hwaccel", "segment_duration", "rtsp_transport"],
        "retention": ["max_age_days", "max_storage_gb"],
        "trickplay": ["enabled", "interval", "thumb_width", "thumb_height"],
        "logging": ["level", "json_output", "timezone"],
    }

    count = 0
    for section, keys in mappings.items():
        section_data = data.get(section, {})
        for key in keys:
            if key in section_data:
                full_key = f"{section}.{key}"
                await set_setting(session, full_key, str(section_data[key]))
                count += 1

    # Mark as migrated
    await set_setting(session, "_migrated_from_yaml", "true")

    logger.info(
        "Migrated legacy config.yaml settings to database",
        extra={"count": count, "path": str(legacy_path)},
    )


# ---------------------------------------------------------------------------
# Singleton
# ---------------------------------------------------------------------------

_config: AppConfig | None = None
_bootstrap: BootstrapConfig | None = None


def get_bootstrap() -> BootstrapConfig:
    """Return the cached bootstrap config."""
    global _bootstrap
    if _bootstrap is None:
        _bootstrap = load_bootstrap()
    return _bootstrap


def get_config() -> AppConfig:
    """Return the cached config. Initially has only bootstrap values;
    call load_settings_from_db() during lifespan to populate DB settings.
    """
    global _config
    if _config is None:
        bootstrap = get_bootstrap()
        _config = AppConfig()
        _populate_from_bootstrap(_config, bootstrap)
        validate_paths(_config)
        logger.info("Configuration loaded from bootstrap", extra={"data_dir": bootstrap.data_dir, "port": bootstrap.port})
    return _config


async def load_settings_from_db(session) -> None:
    """Populate the config singleton with DB settings. Called during lifespan."""
    from app.services.settings import load_settings_dict

    config = get_config()
    settings = await load_settings_dict(session)
    _apply_db_settings(config, settings)
    validate_paths(config)
    logger.info("Configuration updated from database settings", extra={"setting_count": len(settings)})


async def reload_from_db(session) -> None:
    """Reload settings from DB into the live config singleton (after PUT /api/settings)."""
    await load_settings_from_db(session)


def get_tz() -> ZoneInfo:
    """Return the configured timezone as a ZoneInfo object."""
    return ZoneInfo(get_config().logging.timezone)


def local_now() -> datetime:
    """Current wall-clock time in the configured timezone, as a naive datetime.

    Every timestamp this app persists is naive local time (recording filenames,
    segment start/end, motion event times), so `created_at` must match. SQLite's
    CURRENT_TIMESTAMP is UTC, which is what `server_default=func.now()` compiles
    to — that put a clip created at 01:03 local into the list as 15:03.
    """
    return datetime.now(get_tz()).replace(tzinfo=None)
