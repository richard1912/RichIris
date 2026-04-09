"""Settings service — DB-backed key-value store for all app configuration."""

import logging

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Setting

logger = logging.getLogger(__name__)

# Keys that require a service restart to take effect
REQUIRES_RESTART: set[str] = set()

# Keys that require stopping/restarting recording streams (not the whole service)
REQUIRES_STREAM_RESTART: set[str] = set()

# Default values for all settings, grouped by category.
# These are seeded into the DB on first run.
DEFAULTS: dict[str, dict[str, str]] = {
    "ffmpeg": {
        "hwaccel": "cuda",
        "segment_duration": "900",
        "rtsp_transport": "tcp",
    },
    "retention": {
        "max_age_days": "30",
        "max_storage_gb": "500",
    },
    "trickplay": {
        "enabled": "true",
        "interval": "1",
        "thumb_width": "384",
        "thumb_height": "216",
    },
    "logging": {
        "level": "DEBUG",
        "json_output": "false",
        "timezone": "UTC",
    },
}


def _flat_defaults() -> dict[str, tuple[str, str]]:
    """Return {category.key: (value, category)} for all defaults."""
    result = {}
    for category, settings in DEFAULTS.items():
        for key, value in settings.items():
            result[f"{category}.{key}"] = (value, category)
    return result


# Settings that were removed (auto-resolved, not user-configurable).
# Cleaned up from existing databases on startup.
_DEPRECATED_KEYS = {
    "ffmpeg.path", "ffmpeg.ffprobe_path",
    "go2rtc.host", "go2rtc.port",
    "storage.recordings_dir",
}


async def seed_defaults(session: AsyncSession, data_dir: str = "") -> None:
    """Insert default settings for any missing keys. Called from init_db."""
    existing = await session.execute(select(Setting.key))
    existing_keys = {row[0] for row in existing}

    # Remove deprecated settings from existing databases
    removed = 0
    for dep_key in _DEPRECATED_KEYS & existing_keys:
        dep = await session.get(Setting, dep_key)
        if dep:
            await session.delete(dep)
            removed += 1
    if removed:
        await session.commit()
        logger.info("Removed deprecated settings", extra={"count": removed})

    defaults = _flat_defaults()
    inserted = 0
    for full_key, (value, category) in defaults.items():
        if full_key not in existing_keys:
            # Auto-detect system timezone on first install
            if full_key == "logging.timezone":
                try:
                    import tzlocal
                    value = str(tzlocal.get_localzone())
                    logger.info("Auto-detected timezone", extra={"timezone": value})
                except Exception:
                    try:
                        from datetime import datetime, timezone
                        import time
                        utc_offset = time.timezone if time.daylight == 0 else time.altzone
                        hours = -utc_offset // 3600
                        # Map common UTC offsets to IANA timezone names
                        offset_map = {
                            10: "Australia/Sydney", 9.5: "Australia/Adelaide",
                            8: "Australia/Perth", 9: "Asia/Tokyo",
                            5.5: "Asia/Kolkata", 0: "UTC",
                            -5: "America/New_York", -6: "America/Chicago",
                            -7: "America/Denver", -8: "America/Los_Angeles",
                            1: "Europe/London", 2: "Europe/Berlin",
                        }
                        value = offset_map.get(hours, f"Etc/GMT{-hours:+.0f}" if hours == int(hours) else "UTC")
                        logger.info("Auto-detected timezone from UTC offset", extra={"timezone": value, "offset_hours": hours})
                    except Exception:
                        pass  # Fall back to UTC default
            session.add(Setting(key=full_key, value=value, category=category))
            inserted += 1

    if inserted:
        await session.commit()
        logger.info("Seeded default settings", extra={"count": inserted})


async def get_setting(session: AsyncSession, key: str, default: str | None = None) -> str | None:
    """Get a single setting value by key."""
    result = await session.execute(select(Setting.value).where(Setting.key == key))
    row = result.scalar_one_or_none()
    return row if row is not None else default


async def set_setting(session: AsyncSession, key: str, value: str) -> None:
    """Set a single setting value. Creates the row if it doesn't exist."""
    existing = await session.get(Setting, key)
    if existing:
        existing.value = value
    else:
        # Derive category from key prefix
        category = key.split(".")[0] if "." in key else "general"
        session.add(Setting(key=key, value=value, category=category))
    await session.commit()


async def get_all_settings(session: AsyncSession) -> dict[str, dict[str, dict]]:
    """Get all settings grouped by category with restart annotation.

    Returns: {category: {short_key: {"value": str, "requires_restart": bool}}}
    """
    result = await session.execute(select(Setting))
    settings = result.scalars().all()

    grouped: dict[str, dict[str, dict]] = {}
    for s in settings:
        if s.category not in grouped:
            grouped[s.category] = {}
        short_key = s.key.split(".", 1)[1] if "." in s.key else s.key
        grouped[s.category][short_key] = {
            "value": s.value,
            "requires_restart": s.key in REQUIRES_RESTART,
            "requires_stream_restart": s.key in REQUIRES_STREAM_RESTART,
        }
    return grouped


async def update_settings(session: AsyncSession, updates: dict[str, str]) -> bool:
    """Update multiple settings. Returns True if any require restart."""
    restart_needed = False
    for key, value in updates.items():
        existing = await session.get(Setting, key)
        if existing:
            existing.value = str(value)
            if key in REQUIRES_RESTART:
                restart_needed = True
        else:
            category = key.split(".")[0] if "." in key else "general"
            session.add(Setting(key=key, value=str(value), category=category))
            if key in REQUIRES_RESTART:
                restart_needed = True
    await session.commit()
    logger.info("Updated settings", extra={"keys": list(updates.keys())})
    return restart_needed


async def load_settings_dict(session: AsyncSession) -> dict[str, str]:
    """Load all settings as a flat {key: value} dict."""
    result = await session.execute(select(Setting))
    return {s.key: s.value for s in result.scalars().all()}
