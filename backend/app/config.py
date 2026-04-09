"""Application configuration — bootstrap.yaml + DB-backed settings."""

import logging
import shutil
import sys
from dataclasses import dataclass, field
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
    recordings_dir: str = ""
    thumbnails_dir: str = ""
    database_url: str = ""


@dataclass
class FFmpegConfig:
    path: str = ""
    ffprobe_path: str = ""
    hwaccel: str = "cuda"
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


def validate_paths(config: AppConfig) -> None:
    """Ensure required directories exist."""
    for dir_name, path_str in [
        ("recordings_dir", config.storage.recordings_dir),
        ("thumbnails_dir", config.storage.thumbnails_dir),
    ]:
        if path_str:
            p = Path(path_str)
            p.mkdir(parents=True, exist_ok=True)
            logger.debug("Ensured directory exists", extra={"dir_name": dir_name, "path": str(p)})


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
