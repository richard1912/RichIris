"""Backup and restore service — creates/restores .richiris backup archives."""

import asyncio
import json
import logging
import os
import shutil
import sqlite3
import tempfile
import time
import uuid
import zipfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_bootstrap, get_config
from app.models import Camera, Setting

logger = logging.getLogger(__name__)

BACKUP_VERSION = 1


@dataclass
class BackupProgress:
    backup_id: str
    operation: str  # "backup" or "restore"
    status: str = "pending"  # pending, scanning, archiving, extracting, completed, failed, cancelled
    files_total: int = 0
    files_done: int = 0
    bytes_total: int = 0
    bytes_done: int = 0
    current_file: str = ""
    error: str = ""
    started_at: float = field(default_factory=time.time)
    target_path: str = ""
    _cancel_requested: bool = field(default=False, repr=False)


def _camera_to_dict(cam: Camera) -> dict:
    """Serialize a Camera ORM object to a plain dict."""
    return {
        "name": cam.name,
        "rtsp_url": cam.rtsp_url,
        "sub_stream_url": cam.sub_stream_url,
        "enabled": cam.enabled,
        "width": cam.width,
        "height": cam.height,
        "codec": cam.codec,
        "fps": cam.fps,
        "rotation": cam.rotation,
        "motion_sensitivity": cam.motion_sensitivity,
        "motion_script": cam.motion_script,
        "motion_script_off": cam.motion_script_off,
        "motion_scripts": cam.motion_scripts,
        "ai_detection": cam.ai_detection,
        "ai_detect_persons": cam.ai_detect_persons,
        "ai_detect_vehicles": cam.ai_detect_vehicles,
        "ai_detect_animals": cam.ai_detect_animals,
        "ai_confidence_threshold": cam.ai_confidence_threshold,
    }


def _walk_dir_sizes(directory: str) -> tuple[int, int]:
    """Walk a directory and return (file_count, total_bytes)."""
    p = Path(directory)
    if not p.exists():
        return 0, 0
    count = 0
    total = 0
    for entry in p.rglob("*"):
        if entry.is_file():
            try:
                total += entry.stat().st_size
                count += 1
            except OSError:
                pass
    return count, total


class BackupManager:
    """Manages backup and restore operations (one at a time)."""

    def __init__(self) -> None:
        self._current: BackupProgress | None = None
        self._task: asyncio.Task | None = None
        self._lock = asyncio.Lock()

    @property
    def is_running(self) -> bool:
        return self._current is not None and self._current.status in (
            "pending", "scanning", "archiving", "extracting",
        )

    def get_progress(self, backup_id: str) -> BackupProgress | None:
        if self._current and self._current.backup_id == backup_id:
            return self._current
        return None

    def cancel(self, backup_id: str) -> bool:
        if self._current and self._current.backup_id == backup_id and self.is_running:
            self._current._cancel_requested = True
            return True
        return False

    async def preview_sizes(self, session: AsyncSession) -> dict:
        """Return size estimates for each backup component."""
        config = get_config()
        loop = asyncio.get_event_loop()

        # Settings JSON size
        result = await session.execute(select(Setting))
        settings = {s.key: s.value for s in result.scalars().all()}
        settings_json = json.dumps(settings, indent=2).encode()
        settings_size = len(settings_json)

        # Cameras JSON size
        cam_result = await session.execute(select(Camera))
        cameras = [_camera_to_dict(c) for c in cam_result.scalars().all()]
        cameras_json = json.dumps(cameras, indent=2).encode()
        cameras_size = len(cameras_json)

        # Database file size
        db_url = config.storage.database_url
        db_path = db_url.replace("sqlite+aiosqlite:///", "")
        db_size = 0
        if os.path.exists(db_path):
            db_size = os.path.getsize(db_path)

        # Recordings and thumbnails (run in executor — can be slow for large dirs)
        rec_count, rec_size = await loop.run_in_executor(
            None, _walk_dir_sizes, config.storage.recordings_dir
        )
        thumb_count, thumb_size = await loop.run_in_executor(
            None, _walk_dir_sizes, config.storage.thumbnails_dir
        )

        return {
            "settings": {"size": settings_size, "files": 1},
            "cameras": {"size": cameras_size, "files": 1},
            "database": {"size": db_size, "files": 1},
            "recordings": {"size": rec_size, "files": rec_count},
            "thumbnails": {"size": thumb_size, "files": thumb_count},
        }

    async def start_backup(
        self, components: list[str], target_path: str, session: AsyncSession
    ) -> BackupProgress:
        """Start a backup operation. Returns progress for polling."""
        if self.is_running:
            raise RuntimeError("A backup or restore is already in progress.")

        backup_id = uuid.uuid4().hex[:12]
        self._current = BackupProgress(
            backup_id=backup_id,
            operation="backup",
            target_path=target_path,
        )

        # Pre-export settings and cameras JSON while we have a session
        settings_json = None
        cameras_json = None
        if "settings" in components:
            result = await session.execute(select(Setting))
            settings = {s.key: s.value for s in result.scalars().all()}
            settings_json = json.dumps(settings, indent=2)

        if "cameras" in components:
            result = await session.execute(select(Camera))
            cameras = [_camera_to_dict(c) for c in result.scalars().all()]
            cameras_json = json.dumps(cameras, indent=2)

        self._task = asyncio.create_task(
            self._run_backup(self._current, components, settings_json, cameras_json)
        )
        return self._current

    async def _run_backup(
        self,
        progress: BackupProgress,
        components: list[str],
        settings_json: str | None,
        cameras_json: str | None,
    ) -> None:
        """Core backup loop: scan files, create ZIP archive."""
        config = get_config()
        loop = asyncio.get_event_loop()

        try:
            # Phase 1: Scan
            progress.status = "scanning"
            logger.info("Backup scanning", extra={"backup_id": progress.backup_id, "components": components})

            file_list: list[tuple[str, Path, int]] = []  # (arcname_prefix, path, size)
            total_bytes = 0
            total_files = 0

            # Count small items
            if settings_json:
                total_bytes += len(settings_json.encode())
                total_files += 1
            if cameras_json:
                total_bytes += len(cameras_json.encode())
                total_files += 1

            # Database
            db_path = None
            if "database" in components:
                db_url = config.storage.database_url
                db_file = db_url.replace("sqlite+aiosqlite:///", "")
                if os.path.exists(db_file):
                    db_path = db_file
                    total_bytes += os.path.getsize(db_file)
                    total_files += 1

            # Recordings
            if "recordings" in components:
                rec_dir = Path(config.storage.recordings_dir)
                if rec_dir.exists():
                    for entry in rec_dir.rglob("*"):
                        if entry.is_file():
                            try:
                                size = entry.stat().st_size
                                file_list.append(("recordings", entry, size))
                                total_bytes += size
                                total_files += 1
                            except OSError:
                                pass

            # Thumbnails
            if "thumbnails" in components:
                thumb_dir = Path(config.storage.thumbnails_dir)
                if thumb_dir.exists():
                    for entry in thumb_dir.rglob("*"):
                        if entry.is_file():
                            try:
                                size = entry.stat().st_size
                                file_list.append(("thumbnails", entry, size))
                                total_bytes += size
                                total_files += 1
                            except OSError:
                                pass

            progress.files_total = total_files
            progress.bytes_total = total_bytes

            logger.info("Backup scan complete", extra={
                "backup_id": progress.backup_id,
                "files": total_files,
                "bytes": total_bytes,
            })

            if progress._cancel_requested:
                progress.status = "cancelled"
                return

            # Phase 2: Create ZIP archive
            progress.status = "archiving"
            target = Path(progress.target_path)
            target.parent.mkdir(parents=True, exist_ok=True)

            # Build manifest
            manifest = {
                "version": BACKUP_VERSION,
                "created_at": datetime.now(timezone.utc).isoformat(),
                "components": components,
                "data_dir": get_bootstrap().data_dir,
                "component_sizes": {},
            }

            def _write_zip() -> None:
                """Blocking ZIP creation — runs in executor."""
                with zipfile.ZipFile(str(target), "w", allowZip64=True) as zf:
                    # Settings
                    if settings_json and not progress._cancel_requested:
                        zf.writestr("settings.json", settings_json, compress_type=zipfile.ZIP_DEFLATED)
                        manifest["component_sizes"]["settings"] = len(settings_json.encode())
                        progress.files_done += 1
                        progress.bytes_done += len(settings_json.encode())
                        progress.current_file = "settings.json"

                    # Cameras
                    if cameras_json and not progress._cancel_requested:
                        zf.writestr("cameras.json", cameras_json, compress_type=zipfile.ZIP_DEFLATED)
                        manifest["component_sizes"]["cameras"] = len(cameras_json.encode())
                        progress.files_done += 1
                        progress.bytes_done += len(cameras_json.encode())
                        progress.current_file = "cameras.json"

                    # Database — use SQLite backup API for a consistent snapshot
                    # (handles WAL mode safely, unlike raw file copy)
                    if db_path and not progress._cancel_requested:
                        tmp_fd, tmp_path = tempfile.mkstemp(suffix=".db")
                        os.close(tmp_fd)
                        try:
                            src_conn = sqlite3.connect(db_path)
                            dst_conn = sqlite3.connect(tmp_path)
                            src_conn.backup(dst_conn)
                            src_conn.close()
                            dst_conn.close()
                            db_size = os.path.getsize(tmp_path)
                            zf.write(tmp_path, "database/richiris.db", compress_type=zipfile.ZIP_STORED)
                            manifest["component_sizes"]["database"] = db_size
                            progress.files_done += 1
                            progress.bytes_done += db_size
                            progress.current_file = "database/richiris.db"
                        finally:
                            os.unlink(tmp_path)

                    # Recordings and thumbnails
                    for prefix, file_path, file_size in file_list:
                        if progress._cancel_requested:
                            break

                        if prefix == "recordings":
                            base_dir = Path(config.storage.recordings_dir)
                        else:
                            base_dir = Path(config.storage.thumbnails_dir)

                        rel = file_path.relative_to(base_dir)
                        arcname = f"{prefix}/{rel}"
                        progress.current_file = arcname

                        try:
                            zf.write(str(file_path), arcname, compress_type=zipfile.ZIP_STORED)
                            progress.files_done += 1
                            progress.bytes_done += file_size
                        except OSError as e:
                            logger.warning("Skipping file", extra={"file": arcname, "error": str(e)})

                    # Write manifest last (includes final sizes)
                    if not progress._cancel_requested:
                        manifest_json = json.dumps(manifest, indent=2)
                        zf.writestr("manifest.json", manifest_json, compress_type=zipfile.ZIP_DEFLATED)

            await loop.run_in_executor(None, _write_zip)

            if progress._cancel_requested:
                progress.status = "cancelled"
                # Clean up partial file
                try:
                    target.unlink(missing_ok=True)
                except OSError:
                    pass
                logger.info("Backup cancelled", extra={"backup_id": progress.backup_id})
                return

            progress.status = "completed"
            progress.current_file = ""
            logger.info("Backup completed", extra={
                "backup_id": progress.backup_id,
                "files": progress.files_done,
                "bytes": progress.bytes_done,
                "target": str(target),
            })

        except Exception as e:
            progress.status = "failed"
            progress.error = str(e)
            # Clean up partial file on failure
            try:
                Path(progress.target_path).unlink(missing_ok=True)
            except OSError:
                pass
            logger.exception("Backup failed", extra={"backup_id": progress.backup_id})

    async def inspect_backup(self, file_path: str) -> dict:
        """Open a .richiris file and return its manifest."""
        loop = asyncio.get_event_loop()

        def _inspect() -> dict:
            p = Path(file_path)
            if not p.exists():
                raise FileNotFoundError(f"File not found: {file_path}")

            with zipfile.ZipFile(str(p), "r") as zf:
                names = zf.namelist()

                if "manifest.json" not in names:
                    raise ValueError("Invalid backup file: missing manifest.json")

                manifest = json.loads(zf.read("manifest.json"))

                # Enrich with actual sizes from ZIP
                available_components = []
                component_details = {}

                if "settings.json" in names:
                    available_components.append("settings")
                    component_details["settings"] = {
                        "size": zf.getinfo("settings.json").file_size,
                        "files": 1,
                    }

                if "cameras.json" in names:
                    available_components.append("cameras")
                    component_details["cameras"] = {
                        "size": zf.getinfo("cameras.json").file_size,
                        "files": 1,
                    }

                if "database/richiris.db" in names:
                    available_components.append("database")
                    component_details["database"] = {
                        "size": zf.getinfo("database/richiris.db").file_size,
                        "files": 1,
                    }

                # Count recordings
                rec_files = [n for n in names if n.startswith("recordings/") and not n.endswith("/")]
                if rec_files:
                    available_components.append("recordings")
                    rec_size = sum(zf.getinfo(n).file_size for n in rec_files)
                    component_details["recordings"] = {
                        "size": rec_size,
                        "files": len(rec_files),
                    }

                # Count thumbnails
                thumb_files = [n for n in names if n.startswith("thumbnails/") and not n.endswith("/")]
                if thumb_files:
                    available_components.append("thumbnails")
                    thumb_size = sum(zf.getinfo(n).file_size for n in thumb_files)
                    component_details["thumbnails"] = {
                        "size": thumb_size,
                        "files": len(thumb_files),
                    }

                manifest["available_components"] = available_components
                manifest["component_details"] = component_details
                manifest["file_size"] = p.stat().st_size

                return manifest

        return await loop.run_in_executor(None, _inspect)

    async def start_restore(
        self, file_path: str, components: list[str],
    ) -> BackupProgress:
        """Start a restore operation. Returns progress for polling."""
        if self.is_running:
            raise RuntimeError("A backup or restore is already in progress.")

        backup_id = uuid.uuid4().hex[:12]
        self._current = BackupProgress(
            backup_id=backup_id,
            operation="restore",
            target_path=file_path,
        )

        self._task = asyncio.create_task(
            self._run_restore(self._current, file_path, components)
        )
        return self._current

    async def _run_restore(
        self,
        progress: BackupProgress,
        file_path: str,
        components: list[str],
    ) -> None:
        """Core restore loop: extract from ZIP archive."""
        config = get_config()
        loop = asyncio.get_event_loop()

        try:
            progress.status = "scanning"
            logger.info("Restore scanning", extra={
                "backup_id": progress.backup_id,
                "file": file_path,
                "components": components,
            })

            def _scan_zip() -> tuple[list[tuple[str, str, int]], int, int]:
                """Scan ZIP for files to extract. Returns (entries, total_files, total_bytes).
                Each entry is (component, arcname, size).
                """
                entries: list[tuple[str, str, int]] = []
                total_files = 0
                total_bytes = 0

                with zipfile.ZipFile(file_path, "r") as zf:
                    if "settings" in components and "settings.json" in zf.namelist():
                        info = zf.getinfo("settings.json")
                        entries.append(("settings", "settings.json", info.file_size))
                        total_files += 1
                        total_bytes += info.file_size

                    if "cameras" in components and "cameras.json" in zf.namelist():
                        info = zf.getinfo("cameras.json")
                        entries.append(("cameras", "cameras.json", info.file_size))
                        total_files += 1
                        total_bytes += info.file_size

                    if "database" in components and "database/richiris.db" in zf.namelist():
                        info = zf.getinfo("database/richiris.db")
                        entries.append(("database", "database/richiris.db", info.file_size))
                        total_files += 1
                        total_bytes += info.file_size

                    if "recordings" in components:
                        for name in zf.namelist():
                            if name.startswith("recordings/") and not name.endswith("/"):
                                info = zf.getinfo(name)
                                entries.append(("recordings", name, info.file_size))
                                total_files += 1
                                total_bytes += info.file_size

                    if "thumbnails" in components:
                        for name in zf.namelist():
                            if name.startswith("thumbnails/") and not name.endswith("/"):
                                info = zf.getinfo(name)
                                entries.append(("thumbnails", name, info.file_size))
                                total_files += 1
                                total_bytes += info.file_size

                return entries, total_files, total_bytes

            entries, total_files, total_bytes = await loop.run_in_executor(None, _scan_zip)
            progress.files_total = total_files
            progress.bytes_total = total_bytes

            logger.info("Restore scan complete", extra={
                "backup_id": progress.backup_id,
                "files": total_files,
                "bytes": total_bytes,
            })

            if progress._cancel_requested:
                progress.status = "cancelled"
                return

            # Phase 2: Extract
            progress.status = "extracting"

            def _extract() -> None:
                """Blocking extraction — runs in executor."""
                with zipfile.ZipFile(file_path, "r") as zf:
                    for component, arcname, size in entries:
                        if progress._cancel_requested:
                            break

                        progress.current_file = arcname

                        if component == "settings":
                            # Settings are restored via DB in the async phase after
                            data = zf.read(arcname)
                            # Store for async processing
                            progress._settings_data = data  # type: ignore[attr-defined]
                            progress.files_done += 1
                            progress.bytes_done += size

                        elif component == "cameras":
                            data = zf.read(arcname)
                            progress._cameras_data = data  # type: ignore[attr-defined]
                            progress.files_done += 1
                            progress.bytes_done += size

                        elif component == "database":
                            # Extract DB to system temp dir — NOT the DB
                            # directory, which may be locked by other code
                            # that re-opened the engine (e.g. polling).
                            # The actual file swap happens in async code
                            # after we re-close the DB engine.
                            tmp_fd, tmp_path = tempfile.mkstemp(suffix=".db")
                            os.close(tmp_fd)
                            try:
                                with zf.open(arcname) as src, open(tmp_path, "wb") as dst:
                                    shutil.copyfileobj(src, dst)
                                progress._db_temp_path = tmp_path  # type: ignore[attr-defined]
                            except Exception:
                                if os.path.exists(tmp_path):
                                    os.unlink(tmp_path)
                                raise

                            progress.files_done += 1
                            progress.bytes_done += size

                        elif component in ("recordings", "thumbnails"):
                            if component == "recordings":
                                base_dir = config.storage.recordings_dir
                                # Strip "recordings/" prefix
                                rel = arcname[len("recordings/"):]
                            else:
                                base_dir = config.storage.thumbnails_dir
                                rel = arcname[len("thumbnails/"):]

                            dest = os.path.join(base_dir, rel)

                            # Skip if file already exists (merge behavior)
                            if os.path.exists(dest):
                                progress.files_done += 1
                                progress.bytes_done += size
                                continue

                            os.makedirs(os.path.dirname(dest), exist_ok=True)

                            with zf.open(arcname) as src, open(dest, "wb") as dst:
                                shutil.copyfileobj(src, dst)

                            progress.files_done += 1
                            progress.bytes_done += size

            await loop.run_in_executor(None, _extract)

            if progress._cancel_requested:
                progress.status = "cancelled"
                # Clean up temp DB if it was extracted
                if hasattr(progress, "_db_temp_path"):
                    try:
                        os.unlink(progress._db_temp_path)  # type: ignore[attr-defined]
                    except OSError:
                        pass
                logger.info("Restore cancelled", extra={"backup_id": progress.backup_id})
                return

            # Phase 3: Swap the database file if we extracted one.
            # Must re-close the DB engine because other code (e.g. camera
            # polling) may have re-opened it since the router's close_db().
            if hasattr(progress, "_db_temp_path"):
                await self._swap_database(progress._db_temp_path, config)  # type: ignore[attr-defined]

            # Phase 4: Apply settings and cameras via DB (async)
            if hasattr(progress, "_settings_data"):
                await self._restore_settings(progress._settings_data)  # type: ignore[attr-defined]

            if hasattr(progress, "_cameras_data"):
                await self._restore_cameras(progress._cameras_data)  # type: ignore[attr-defined]

            progress.status = "completed"
            progress.current_file = ""
            logger.info("Restore completed", extra={
                "backup_id": progress.backup_id,
                "files": progress.files_done,
                "bytes": progress.bytes_done,
            })

        except Exception as e:
            progress.status = "failed"
            progress.error = str(e)
            logger.exception("Restore failed", extra={"backup_id": progress.backup_id})

    async def _swap_database(self, tmp_path: str, config) -> None:
        """Replace the live database file with the restored copy.

        Re-closes the DB engine (polling may have re-opened it), deletes
        stale WAL/SHM journals, then swaps the file.
        """
        from app.database import close_db

        db_url = config.storage.database_url
        db_file = db_url.replace("sqlite+aiosqlite:///", "")
        db_dir = os.path.dirname(db_file)
        os.makedirs(db_dir, exist_ok=True)

        # Close the engine again — polling endpoints may have re-created it
        await close_db()

        # Small delay to let Windows release file handles
        await asyncio.sleep(0.5)

        def _do_swap() -> None:
            # Delete stale WAL/SHM journals
            for suffix in ("-wal", "-shm"):
                journal = db_file + suffix
                if os.path.exists(journal):
                    os.unlink(journal)
                    logger.info("Deleted stale journal", extra={"path": journal})

            # Replace the DB file (shutil.move handles cross-drive moves)
            shutil.move(tmp_path, db_file)
            logger.info("Database file replaced", extra={"path": db_file})

        loop = asyncio.get_event_loop()
        try:
            await loop.run_in_executor(None, _do_swap)
        except Exception:
            # Clean up temp file on failure
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise

    async def _restore_settings(self, data: bytes) -> None:
        """Restore settings from JSON data into the database."""
        from app.database import get_session_factory
        from app.services.settings import update_settings

        settings = json.loads(data)
        factory = get_session_factory()
        async with factory() as session:
            await update_settings(session, settings)
        logger.info("Restored settings", extra={"count": len(settings)})

    async def _restore_cameras(self, data: bytes) -> None:
        """Restore cameras from JSON data into the database (upsert by name)."""
        from app.database import get_session_factory

        cameras = json.loads(data)
        factory = get_session_factory()
        async with factory() as session:
            for cam_data in cameras:
                name = cam_data.get("name")
                if not name:
                    continue

                # Find existing camera by name
                result = await session.execute(
                    select(Camera).where(Camera.name == name)
                )
                existing = result.scalar_one_or_none()

                if existing:
                    # Update existing camera
                    for field_name, value in cam_data.items():
                        if field_name != "name" and hasattr(existing, field_name):
                            setattr(existing, field_name, value)
                else:
                    # Create new camera
                    cam = Camera(**{
                        k: v for k, v in cam_data.items()
                        if hasattr(Camera, k)
                    })
                    session.add(cam)

            await session.commit()
        logger.info("Restored cameras", extra={"count": len(cameras)})


_manager: BackupManager | None = None


def get_backup_manager() -> BackupManager:
    global _manager
    if _manager is None:
        _manager = BackupManager()
    return _manager
