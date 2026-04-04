"""Storage migration service — moves/copies recordings to a new directory."""

import asyncio
import logging
import os
import shutil
import tempfile
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from app.config import get_config

logger = logging.getLogger(__name__)


@dataclass
class MigrationProgress:
    migration_id: str
    source: str
    target: str
    mode: str  # "move" or "copy"
    status: str = "pending"  # pending, scanning, copying, finalizing, completed, failed, cancelled
    files_total: int = 0
    files_done: int = 0
    bytes_total: int = 0
    bytes_done: int = 0
    current_file: str = ""
    error: str = ""
    started_at: float = field(default_factory=time.time)
    _cancel_requested: bool = field(default=False, repr=False)


def validate_target(path: str) -> dict:
    """Validate a target directory for storage migration.

    Returns dict with: valid, writable, free_space_gb, source_size_gb, error.
    """
    config = get_config()
    source = config.storage.recordings_dir
    result = {
        "valid": False,
        "writable": False,
        "free_space_gb": 0.0,
        "source_size_gb": 0.0,
        "source_path": source,
        "error": "",
    }

    target = Path(path)

    # Reject same path
    try:
        if Path(source).resolve() == target.resolve():
            result["error"] = "Target is the same as the current recordings directory."
            return result
    except Exception:
        pass

    # Check parent exists (we'll create the target dir itself)
    if not target.parent.exists():
        result["error"] = f"Parent directory does not exist: {target.parent}"
        return result

    # Create target dir if needed
    try:
        target.mkdir(parents=True, exist_ok=True)
    except Exception as e:
        result["error"] = f"Cannot create directory: {e}"
        return result

    # Write test (important: SYSTEM account may lack access to network shares)
    try:
        test_file = target / f".richiris_write_test_{uuid.uuid4().hex[:8]}"
        test_file.write_bytes(b"write_test")
        test_file.unlink()
        result["writable"] = True
    except Exception as e:
        result["error"] = f"Directory is not writable: {e}"
        return result

    # Free space on target
    try:
        usage = shutil.disk_usage(str(target))
        result["free_space_gb"] = round(usage.free / (1024 ** 3), 2)
    except Exception:
        pass

    # Source size
    source_path = Path(source)
    if source_path.exists():
        total_bytes = 0
        try:
            for entry in source_path.rglob("*"):
                if entry.is_file():
                    total_bytes += entry.stat().st_size
        except Exception:
            pass
        result["source_size_gb"] = round(total_bytes / (1024 ** 3), 2)

    result["valid"] = True
    return result


class StorageMigrationManager:
    """Manages storage migration operations (one at a time)."""

    def __init__(self) -> None:
        self._current: MigrationProgress | None = None
        self._task: asyncio.Task | None = None
        self._lock = asyncio.Lock()

    @property
    def is_running(self) -> bool:
        return self._current is not None and self._current.status in ("scanning", "copying")

    def get_progress(self, migration_id: str) -> MigrationProgress | None:
        if self._current and self._current.migration_id == migration_id:
            return self._current
        return None

    async def start_migration(self, target: str, mode: str) -> MigrationProgress:
        """Start migrating recordings to a new directory.

        Caller must stop streams before calling this.
        """
        if self.is_running:
            raise RuntimeError("A migration is already in progress.")

        config = get_config()
        source = config.storage.recordings_dir

        migration_id = uuid.uuid4().hex[:12]
        self._current = MigrationProgress(
            migration_id=migration_id,
            source=source,
            target=target,
            mode=mode,
        )

        self._task = asyncio.create_task(self._run_migration(self._current))
        return self._current

    def cancel(self, migration_id: str) -> bool:
        if self._current and self._current.migration_id == migration_id:
            self._current._cancel_requested = True
            return True
        return False

    async def _run_migration(self, progress: MigrationProgress) -> None:
        """Core migration loop: scan files, copy/move them."""
        source = Path(progress.source)
        target = Path(progress.target)
        loop = asyncio.get_event_loop()

        try:
            # Phase 1: Scan
            progress.status = "scanning"
            logger.info("Migration scanning source", extra={
                "migration_id": progress.migration_id,
                "source": progress.source,
            })

            file_list: list[tuple[Path, int]] = []
            total_bytes = 0

            for entry in source.rglob("*"):
                if entry.is_file():
                    size = entry.stat().st_size
                    file_list.append((entry, size))
                    total_bytes += size

            progress.files_total = len(file_list)
            progress.bytes_total = total_bytes

            logger.info("Migration scan complete", extra={
                "migration_id": progress.migration_id,
                "files": len(file_list),
                "bytes": total_bytes,
            })

            if progress._cancel_requested:
                progress.status = "cancelled"
                return

            # Phase 2: Copy
            progress.status = "copying"

            for file_path, file_size in file_list:
                if progress._cancel_requested:
                    progress.status = "cancelled"
                    logger.info("Migration cancelled by user", extra={
                        "migration_id": progress.migration_id,
                        "files_done": progress.files_done,
                    })
                    return

                # Compute relative path and destination
                rel = file_path.relative_to(source)
                dest = target / rel
                progress.current_file = str(rel)

                # Create parent dirs
                dest.parent.mkdir(parents=True, exist_ok=True)

                # Copy in executor to avoid blocking the event loop
                await loop.run_in_executor(None, shutil.copy2, str(file_path), str(dest))
                progress.files_done += 1
                progress.bytes_done += file_size

            # Phase 3: Delete source files if mode is "move"
            if progress.mode == "move":
                progress.status = "finalizing"
                logger.info("Migration removing source files", extra={
                    "migration_id": progress.migration_id,
                })
                try:
                    await loop.run_in_executor(None, shutil.rmtree, str(source))
                except Exception:
                    logger.warning("Failed to remove source directory after move", extra={
                        "source": progress.source,
                    })

            progress.status = "completed"
            progress.current_file = ""
            logger.info("Migration completed", extra={
                "migration_id": progress.migration_id,
                "mode": progress.mode,
                "files": progress.files_done,
                "bytes": progress.bytes_done,
            })

        except Exception as e:
            progress.status = "failed"
            progress.error = str(e)
            logger.exception("Migration failed", extra={
                "migration_id": progress.migration_id,
            })


_manager: StorageMigrationManager | None = None


def get_migration_manager() -> StorageMigrationManager:
    global _manager
    if _manager is None:
        _manager = StorageMigrationManager()
    return _manager
