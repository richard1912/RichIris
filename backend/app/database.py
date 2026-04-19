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
    """Create all tables and run migrations."""
    # Import models to register them with Base.metadata before create_all
    import app.models  # noqa: F401

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
    # Migrate: add sort_order column to cameras
    async with engine.begin() as conn:
        try:
            await conn.execute(
                text("ALTER TABLE cameras ADD COLUMN sort_order INTEGER NOT NULL DEFAULT 0")
            )
            logger.info("Migration: added sort_order column to cameras")
            # Seed sort_order = id for existing cameras so current order is preserved
            await conn.execute(text("UPDATE cameras SET sort_order = id"))
            logger.info("Migration: seeded sort_order from camera ids")
        except Exception:
            pass  # Column already exists
    # Migrate: add group_id column to cameras
    async with engine.begin() as conn:
        try:
            await conn.execute(
                text("ALTER TABLE cameras ADD COLUMN group_id INTEGER REFERENCES camera_groups(id)")
            )
            logger.info("Migration: added group_id column to cameras")
        except Exception:
            pass  # Column already exists
    # Migrate: add face_recognition column to cameras
    async with engine.begin() as conn:
        try:
            await conn.execute(
                text("ALTER TABLE cameras ADD COLUMN face_recognition BOOLEAN NOT NULL DEFAULT 0")
            )
            logger.info("Migration: added face_recognition column to cameras")
        except Exception:
            pass
    # Migrate: add face_match_threshold column to cameras
    async with engine.begin() as conn:
        try:
            await conn.execute(
                text("ALTER TABLE cameras ADD COLUMN face_match_threshold INTEGER NOT NULL DEFAULT 50")
            )
            logger.info("Migration: added face_match_threshold column to cameras")
        except Exception:
            pass
    # Migrate: add face_matches column to motion_events
    async with engine.begin() as conn:
        try:
            await conn.execute(
                text("ALTER TABLE motion_events ADD COLUMN face_matches TEXT")
            )
            logger.info("Migration: added face_matches column to motion_events")
        except Exception:
            pass
    # Migrate: add face_unknown column to motion_events
    async with engine.begin() as conn:
        try:
            await conn.execute(
                text("ALTER TABLE motion_events ADD COLUMN face_unknown BOOLEAN NOT NULL DEFAULT 0")
            )
            logger.info("Migration: added face_unknown column to motion_events")
        except Exception:
            pass
    # Migrate: add face_detected column to motion_events (SCRFD-only hit,
    # populated on every person event regardless of per-camera FR setting).
    async with engine.begin() as conn:
        try:
            await conn.execute(
                text("ALTER TABLE motion_events ADD COLUMN face_detected BOOLEAN NOT NULL DEFAULT 0")
            )
            logger.info("Migration: added face_detected column to motion_events")
        except Exception:
            pass
    # Migrate: add scripts_fired column to motion_events (JSON list of display-name
    # snapshots captured at firing time; None for pre-migration events).
    async with engine.begin() as conn:
        try:
            await conn.execute(
                text("ALTER TABLE motion_events ADD COLUMN scripts_fired TEXT")
            )
            logger.info("Migration: added scripts_fired column to motion_events")
        except Exception:
            pass
    # Migrate: add zones_triggered column to motion_events (JSON list of zone-name
    # snapshots; None for events outside any zone or pre-migration rows).
    async with engine.begin() as conn:
        try:
            await conn.execute(
                text("ALTER TABLE motion_events ADD COLUMN zones_triggered TEXT")
            )
            logger.info("Migration: added zones_triggered column to motion_events")
        except Exception:
            pass
    # Index the enrollment-picker query path so growth past 10k+ events doesn't
    # drag the unlabeled-thumbs endpoint.
    async with engine.begin() as conn:
        for name, ddl in [
            ("ix_motion_events_label_start",
             "CREATE INDEX IF NOT EXISTS ix_motion_events_label_start "
             "ON motion_events (detection_label, start_time)"),
            ("ix_face_embeddings_source_path",
             "CREATE INDEX IF NOT EXISTS ix_face_embeddings_source_path "
             "ON face_embeddings (source_thumbnail_path)"),
        ]:
            try:
                await conn.execute(text(ddl))
                logger.info(f"Migration: ensured index {name}")
            except Exception:
                logger.exception(f"Migration: failed to create index {name}")
    # Migrate: add face_crop_path to face_embeddings (for existing deployments where table was created without it)
    async with engine.begin() as conn:
        try:
            await conn.execute(
                text("ALTER TABLE face_embeddings ADD COLUMN face_crop_path VARCHAR(500)")
            )
            logger.info("Migration: added face_crop_path column to face_embeddings")
        except Exception:
            pass
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

    # Migrate face_embeddings: add clustering columns (source, detection_score, source_motion_event_id)
    async with engine.begin() as conn:
        try:
            await conn.execute(
                text("ALTER TABLE face_embeddings ADD COLUMN source VARCHAR(20) NOT NULL DEFAULT 'user_enrolled'")
            )
            logger.info("Migration: added source column to face_embeddings")
        except Exception:
            pass
    async with engine.begin() as conn:
        try:
            await conn.execute(
                text("ALTER TABLE face_embeddings ADD COLUMN detection_score FLOAT")
            )
            logger.info("Migration: added detection_score column to face_embeddings")
        except Exception:
            pass
    async with engine.begin() as conn:
        try:
            await conn.execute(
                text("ALTER TABLE face_embeddings ADD COLUMN source_motion_event_id INTEGER REFERENCES motion_events(id) ON DELETE SET NULL")
            )
            logger.info("Migration: added source_motion_event_id column to face_embeddings")
        except Exception:
            pass

    # Seed default settings into the settings table
    from app.config import get_bootstrap
    from app.services.settings import get_setting, seed_defaults, set_setting
    factory = get_session_factory()
    async with factory() as session:
        await seed_defaults(session, data_dir=get_bootstrap().data_dir)

    # One-shot migration: bump cameras still on the old face_match_threshold
    # default (50, cosine 0.50) up to the new default (60, cosine 0.60) once.
    # Guarded by a settings marker so user tweaks post-migration are preserved.
    MARKER = "migration_face_threshold_60_v1"
    async with factory() as session:
        if not await get_setting(session, MARKER):
            res = await session.execute(
                text("UPDATE cameras SET face_match_threshold = 60 WHERE face_match_threshold = 50")
            )
            await set_setting(session, MARKER, "1")
            await session.commit()
            if res.rowcount:
                logger.info("Migration: bumped face_match_threshold 50→60 on %d cameras", res.rowcount)

    # One-shot migration: rebuild `faces` table to make `name` nullable
    # (SQLite can't ALTER COLUMN). Null name = auto-clustered suggestion.
    # Partial unique index preserves uniqueness for named people.
    FACES_MARKER = "migration_faces_nullable_name_v1"
    async with factory() as session:
        already = await get_setting(session, FACES_MARKER)
    if not already:
        async with engine.begin() as conn:
            # Detect current nullability — skip if already migrated (fresh install)
            info = (await conn.execute(text("PRAGMA table_info(faces)"))).fetchall()
            name_col = next((r for r in info if r[1] == "name"), None)
            needs_rebuild = name_col is not None and int(name_col[3]) == 1
            if needs_rebuild:
                await conn.execute(text("PRAGMA foreign_keys = OFF"))
                await conn.execute(text("ALTER TABLE faces RENAME TO _faces_legacy"))
                await conn.execute(text(
                    "CREATE TABLE faces ("
                    "id INTEGER PRIMARY KEY AUTOINCREMENT, "
                    "name VARCHAR(100), "
                    "notes VARCHAR(500), "
                    "created_at DATETIME DEFAULT CURRENT_TIMESTAMP"
                    ")"
                ))
                await conn.execute(text(
                    "INSERT INTO faces (id, name, notes, created_at) "
                    "SELECT id, name, notes, created_at FROM _faces_legacy"
                ))
                await conn.execute(text("DROP TABLE _faces_legacy"))
                await conn.execute(text("PRAGMA foreign_keys = ON"))
                logger.info("Migration: rebuilt faces table with nullable name")
            try:
                await conn.execute(text(
                    "CREATE UNIQUE INDEX IF NOT EXISTS idx_faces_name_unique_non_null "
                    "ON faces(name) WHERE name IS NOT NULL"
                ))
            except Exception:
                logger.exception("Migration: failed to create partial unique index on faces.name")
        async with factory() as session:
            await set_setting(session, FACES_MARKER, "1")
            await session.commit()

    logger.info("Database tables created")


async def close_db() -> None:
    """Dispose of the engine."""
    global _engine, _session_factory
    if _engine:
        await _engine.dispose()
        logger.info("Database engine disposed")
    _engine = None
    _session_factory = None
