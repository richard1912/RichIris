"""SQLAlchemy async engine and session management."""

import logging

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase

from app.config import get_config

logger = logging.getLogger(__name__)


class Base(DeclarativeBase):
    pass


_engine = None
_session_factory = None


def get_engine():
    """Create or return the cached async engine."""
    global _engine
    if _engine is None:
        url = get_config().storage.database_url
        logger.info("Creating database engine", extra={"url": url})
        _engine = create_async_engine(url, echo=False)
    return _engine


def get_session_factory() -> async_sessionmaker[AsyncSession]:
    """Create or return the cached session factory."""
    global _session_factory
    if _session_factory is None:
        _session_factory = async_sessionmaker(
            get_engine(), class_=AsyncSession, expire_on_commit=False
        )
    return _session_factory


async def get_db() -> AsyncSession:
    """FastAPI dependency that yields a database session."""
    factory = get_session_factory()
    async with factory() as session:
        yield session


async def init_db() -> None:
    """Create all tables."""
    engine = get_engine()
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    # Migrate: add rotation column if missing (SQLite ALTER TABLE)
    async with engine.begin() as conn:
        try:
            await conn.execute(
                text("ALTER TABLE cameras ADD COLUMN rotation INTEGER NOT NULL DEFAULT 0")
            )
            logger.info("Migration: added rotation column to cameras")
        except Exception:
            pass  # Column already exists
    # Migrate: add has_thumbnail column if missing
    async with engine.begin() as conn:
        try:
            await conn.execute(
                text("ALTER TABLE recordings ADD COLUMN has_thumbnail BOOLEAN NOT NULL DEFAULT 0")
            )
            logger.info("Migration: added has_thumbnail column to recordings")
        except Exception:
            pass  # Column already exists
    # Migrate: add in_progress column if missing
    async with engine.begin() as conn:
        try:
            await conn.execute(
                text("ALTER TABLE recordings ADD COLUMN in_progress BOOLEAN NOT NULL DEFAULT 0")
            )
            logger.info("Migration: added in_progress column to recordings")
        except Exception:
            pass  # Column already exists
    # Migrate: add sub_stream_url column if missing
    async with engine.begin() as conn:
        try:
            await conn.execute(
                text("ALTER TABLE cameras ADD COLUMN sub_stream_url VARCHAR(500)")
            )
            logger.info("Migration: added sub_stream_url column to cameras")
        except Exception:
            pass  # Column already exists
    # Migrate: add motion_sensitivity column if missing
    async with engine.begin() as conn:
        try:
            await conn.execute(
                text("ALTER TABLE cameras ADD COLUMN motion_sensitivity INTEGER NOT NULL DEFAULT 0")
            )
            logger.info("Migration: added motion_sensitivity column to cameras")
        except Exception:
            pass  # Column already exists
    # Migrate: add motion_script column if missing
    async with engine.begin() as conn:
        try:
            await conn.execute(
                text("ALTER TABLE cameras ADD COLUMN motion_script VARCHAR(500)")
            )
            logger.info("Migration: added motion_script column to cameras")
        except Exception:
            pass  # Column already exists
    # Migrate: add motion_script_off column if missing
    async with engine.begin() as conn:
        try:
            await conn.execute(
                text("ALTER TABLE cameras ADD COLUMN motion_script_off VARCHAR(500)")
            )
            logger.info("Migration: added motion_script_off column to cameras")
        except Exception:
            pass  # Column already exists
    # Migrate: add ai_detection column if missing
    async with engine.begin() as conn:
        try:
            await conn.execute(
                text("ALTER TABLE cameras ADD COLUMN ai_detection BOOLEAN NOT NULL DEFAULT 0")
            )
            logger.info("Migration: added ai_detection column to cameras")
        except Exception:
            pass  # Column already exists
    # Migrate: add ai_confidence_threshold column if missing
    async with engine.begin() as conn:
        try:
            await conn.execute(
                text("ALTER TABLE cameras ADD COLUMN ai_confidence_threshold INTEGER NOT NULL DEFAULT 50")
            )
            logger.info("Migration: added ai_confidence_threshold column to cameras")
        except Exception:
            pass  # Column already exists
    # Migrate: add ai_detect_persons column if missing
    async with engine.begin() as conn:
        try:
            await conn.execute(
                text("ALTER TABLE cameras ADD COLUMN ai_detect_persons BOOLEAN NOT NULL DEFAULT 1")
            )
            logger.info("Migration: added ai_detect_persons column to cameras")
        except Exception:
            pass  # Column already exists
    # Migrate: add ai_detect_vehicles column if missing
    async with engine.begin() as conn:
        try:
            await conn.execute(
                text("ALTER TABLE cameras ADD COLUMN ai_detect_vehicles BOOLEAN NOT NULL DEFAULT 0")
            )
            logger.info("Migration: added ai_detect_vehicles column to cameras")
        except Exception:
            pass  # Column already exists
    # Migrate: add ai_detect_animals column if missing
    async with engine.begin() as conn:
        try:
            await conn.execute(
                text("ALTER TABLE cameras ADD COLUMN ai_detect_animals BOOLEAN NOT NULL DEFAULT 0")
            )
            logger.info("Migration: added ai_detect_animals column to cameras")
        except Exception:
            pass  # Column already exists
    # Migrate: add detection_label column if missing
    async with engine.begin() as conn:
        try:
            await conn.execute(
                text("ALTER TABLE motion_events ADD COLUMN detection_label VARCHAR(50)")
            )
            logger.info("Migration: added detection_label column to motion_events")
        except Exception:
            pass  # Column already exists
    # Migrate: add detection_confidence column if missing
    async with engine.begin() as conn:
        try:
            await conn.execute(
                text("ALTER TABLE motion_events ADD COLUMN detection_confidence FLOAT")
            )
            logger.info("Migration: added detection_confidence column to motion_events")
        except Exception:
            pass  # Column already exists
    # Migrate: add motion_scripts JSON column if missing
    async with engine.begin() as conn:
        try:
            await conn.execute(
                text("ALTER TABLE cameras ADD COLUMN motion_scripts TEXT")
            )
            logger.info("Migration: added motion_scripts column to cameras")
        except Exception:
            pass  # Column already exists
    # Migrate: convert legacy motion_script/motion_script_off to motion_scripts JSON
    async with engine.begin() as conn:
        try:
            rows = (await conn.execute(
                text("SELECT id, motion_script, motion_script_off FROM cameras WHERE motion_script IS NOT NULL AND (motion_scripts IS NULL OR motion_scripts = '')")
            )).fetchall()
            import json
            for row in rows:
                cam_id, script_on, script_off = row
                entry = {"on": script_on, "off": script_off or None,
                         "persons": True, "vehicles": True, "animals": True, "motion_only": True}
                await conn.execute(
                    text("UPDATE cameras SET motion_scripts = :scripts WHERE id = :id"),
                    {"scripts": json.dumps([entry]), "id": cam_id},
                )
            if rows:
                logger.info("Migration: converted %d cameras from legacy motion_script to motion_scripts", len(rows))
        except Exception:
            logger.exception("Migration: failed to convert legacy motion scripts")
    logger.info("Database tables created")


async def close_db() -> None:
    """Dispose of the engine."""
    global _engine, _session_factory
    if _engine:
        await _engine.dispose()
        logger.info("Database engine disposed")
    _engine = None
    _session_factory = None
