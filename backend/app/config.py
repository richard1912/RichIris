"""Application configuration loaded from config.yaml."""

import logging
from dataclasses import dataclass, field
from pathlib import Path
from zoneinfo import ZoneInfo

import yaml

logger = logging.getLogger(__name__)

CONFIG_PATH = Path("C:/01-Self-Hosting/RichIris/config.yaml")


@dataclass
class ServerConfig:
    host: str = "0.0.0.0"
    port: int = 8700


@dataclass
class StorageConfig:
    recordings_dir: str = "G:/RichIris"
    database_url: str = "sqlite+aiosqlite:///C:/01-Self-Hosting/RichIris/data/richiris.db"


@dataclass
class FFmpegConfig:
    path: str = "ffmpeg"
    ffprobe_path: str = "ffprobe"
    hwaccel: str = "cuda"
    segment_duration: int = 900
    rtsp_transport: str = "tcp"
    rtsp_timeout_us: int = 30_000_000  # 30s socket I/O timeout (microseconds)


@dataclass
class Go2rtcConfig:
    host: str = "localhost"
    port: int = 1984
    rtsp_port: int = 8554


@dataclass
class RetentionConfig:
    max_age_days: int = 30
    max_storage_gb: int = 500


@dataclass
class TrickplayConfig:
    enabled: bool = True
    interval: int = 60
    thumb_width: int = 192
    thumb_height: int = 108


@dataclass
class LoggingConfig:
    level: str = "DEBUG"
    json_output: bool = False
    timezone: str = "Australia/Sydney"


@dataclass
class CameraConfig:
    name: str = ""
    rtsp_url: str = ""
    sub_stream_url: str = ""
    enabled: bool = True


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


def load_yaml(path: Path) -> dict:
    """Load and parse a YAML file."""
    logger.debug("Loading config", extra={"path": str(path)})
    with open(path, "r") as f:
        return yaml.safe_load(f) or {}


def parse_cameras(raw: list[dict] | None) -> list[CameraConfig]:
    """Parse camera entries from raw config data."""
    if not raw:
        return []
    return [CameraConfig(**cam) for cam in raw]


def build_config(data: dict) -> AppConfig:
    """Build AppConfig from raw dictionary."""
    return AppConfig(
        server=ServerConfig(**data.get("server", {})),
        storage=StorageConfig(**data.get("storage", {})),
        ffmpeg=FFmpegConfig(**data.get("ffmpeg", {})),
        go2rtc=Go2rtcConfig(**data.get("go2rtc", {})),
        retention=RetentionConfig(**data.get("retention", {})),
        trickplay=TrickplayConfig(**{k: v for k, v in data.get("trickplay", {}).items() if k in TrickplayConfig.__dataclass_fields__}),
        logging=LoggingConfig(**data.get("logging", {})),
        cameras=parse_cameras(data.get("cameras")),
    )


def validate_paths(config: AppConfig) -> None:
    """Ensure required directories exist, creating them if needed."""
    p = Path(config.storage.recordings_dir)
    p.mkdir(parents=True, exist_ok=True)
    logger.debug("Ensured directory exists", extra={"path": str(p)})


def load_config(path: Path | None = None) -> AppConfig:
    """Load, parse, and validate the full application config."""
    path = path or CONFIG_PATH
    data = load_yaml(path)
    config = build_config(data)
    validate_paths(config)
    logger.info(
        "Configuration loaded",
        extra={"cameras_count": len(config.cameras), "config_path": str(path)},
    )
    return config


# Singleton
_config: AppConfig | None = None


def get_config() -> AppConfig:
    """Return the cached config, loading it if needed."""
    global _config
    if _config is None:
        _config = load_config()
    return _config


def get_tz() -> ZoneInfo:
    """Return the configured timezone as a ZoneInfo object."""
    return ZoneInfo(get_config().logging.timezone)
