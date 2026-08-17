#!/usr/bin/env python3
"""Rewrite Windows paths in richiris.db for the Debian box (and back).

Forward (default): maps the Windows deployment's paths to the box layout:
    E:/recordings/...            -> /mnt/backup/RichIris-Archive/recordings/... (tier -> archive)
    F:/RichIris-Archive/...      -> /mnt/backup/RichIris-Archive/...
    G:/RichIris/... and E:/...   -> /var/lib/richiris/...   (thumbnails/exports/faces)
    C:/ProgramData/RichIris/...  -> /var/lib/richiris/...
    settings: archive_dir, hwaccel=vaapi, logging INFO, hot_max_gb=60, ai.remote_url
    cameras.motion_script(_off)/motion_scripts: Windows python + script paths -> box paths

Reverse (--reverse): maps a box DB back to the Windows layout for rollback.
Segments the box archived are on the same physical disk Windows sees as F:,
so reversed paths resolve immediately.

Usage:
    python3 migrate_db_to_linux.py --db /var/lib/richiris/database/richiris.db [--dry-run] [--reverse]
"""

import argparse
import json
import re
import shutil
import sqlite3
import sys
from collections import Counter
from pathlib import Path

# (table, column) pairs holding absolute paths
PATH_COLUMNS = [
    ("recordings", "file_path"),
    ("motion_events", "thumbnail_path"),
    ("clip_exports", "file_path"),
    ("face_embeddings", "source_thumbnail_path"),
    ("face_embeddings", "face_crop_path"),
    ("unclustered_faces", "face_crop_path"),
]

BOX_ARCHIVE = "/mnt/backup/RichIris-Archive"
BOX_DATA = "/var/lib/richiris"
WIN_PYTHON = "C:/Users/Richard/AppData/Local/Programs/Python/Python313/python.exe"
BOX_PYTHON = "/opt/richiris/venv/bin/python"
WIN_SCRIPTS = "C:/01-Self-Hosting/RichIris/Camera Light Control/"
BOX_SCRIPTS = "/opt/richiris/Camera Light Control/"
REMOTE_AI_URL = "http://192.168.8.11:8701"

# Forward prefix maps, applied in order after backslash normalization.
# G: was the data_dir before E: (bootstrap.yaml.bak-G), in two layouts:
# G:/RichIris/<sub> (older) and G:/<sub> directly — both map to the box data dir.
FORWARD_PREFIXES = [
    ("F:/RichIris-Archive/", BOX_ARCHIVE + "/"),
    ("G:/RichIris/", BOX_DATA + "/"),
    ("G:/", BOX_DATA + "/"),
    ("C:/ProgramData/RichIris/", BOX_DATA + "/"),
    ("E:/", BOX_DATA + "/"),
]

# Reverse maps (box -> Windows). Note E: hot rows were folded into the archive
# on the forward pass, so archive maps back to F: (Windows sees the same files).
REVERSE_PREFIXES = [
    (BOX_ARCHIVE + "/", "F:/RichIris-Archive/"),
    (BOX_DATA + "/", "E:/"),
]

FORWARD_SCRIPT_REPLACEMENTS = [
    (f'"{WIN_PYTHON}"', BOX_PYTHON),
    (WIN_PYTHON, BOX_PYTHON),
    (WIN_SCRIPTS, BOX_SCRIPTS),
]
REVERSE_SCRIPT_REPLACEMENTS = [
    (BOX_PYTHON, f'"{WIN_PYTHON}"'),
    (BOX_SCRIPTS, WIN_SCRIPTS),
]

FORWARD_SETTINGS = {
    "storage.archive_dir": BOX_ARCHIVE,
    "ffmpeg.hwaccel": "vaapi",
    "logging.level": "INFO",
    "storage.hot_max_gb": "60",
    "ai.remote_url": REMOTE_AI_URL,
    "ai.remote_timeout_ms": "2500",
}
REVERSE_SETTINGS = {
    "storage.archive_dir": "F:/RichIris-Archive",
    "ffmpeg.hwaccel": "cuda",
    "storage.hot_max_gb": "100",
    "ai.remote_url": "",
}


def normalize_slashes(cur: sqlite3.Cursor) -> None:
    for table, col in PATH_COLUMNS:
        cur.execute(
            f"UPDATE {table} SET {col} = REPLACE({col}, '\\', '/') WHERE {col} IS NOT NULL"
        )


def apply_prefixes(cur: sqlite3.Cursor, prefixes: list[tuple[str, str]], forward: bool) -> None:
    if forward:
        # Hot-tier recordings get folded into the archive (files are drained
        # there at cutover by robocopy; storage_flush mirrors relative paths).
        cur.execute(
            "UPDATE recordings SET "
            "  file_path = ? || SUBSTR(file_path, ?), "
            "  tier = 'archive' "
            "WHERE file_path LIKE 'E:/recordings/%'",
            (BOX_ARCHIVE + "/recordings/", len("E:/recordings/") + 1),
        )
    for table, col in PATH_COLUMNS:
        for old, new in prefixes:
            cur.execute(
                f"UPDATE {table} SET {col} = ? || SUBSTR({col}, ?) "
                f"WHERE {col} LIKE ? || '%'",
                (new, len(old) + 1, old),
            )


def rewrite_scripts(cur: sqlite3.Cursor, replacements: list[tuple[str, str]]) -> int:
    changed = 0
    cur.execute("SELECT id, motion_script, motion_script_off, motion_scripts FROM cameras")
    for cam_id, script_on, script_off, scripts_json in cur.fetchall():
        def _sub(text):
            if not text:
                return text
            for old, new in replacements:
                text = text.replace(old, new)
            return text

        new_on = _sub(script_on)
        new_off = _sub(script_off)
        new_json = scripts_json
        if scripts_json:
            try:
                entries = json.loads(scripts_json)
                for entry in entries:
                    for key in ("on", "off"):
                        if entry.get(key):
                            entry[key] = _sub(entry[key])
                new_json = json.dumps(entries)
            except (json.JSONDecodeError, AttributeError, TypeError):
                new_json = _sub(scripts_json)

        if (new_on, new_off, new_json) != (script_on, script_off, scripts_json):
            cur.execute(
                "UPDATE cameras SET motion_script = ?, motion_script_off = ?, motion_scripts = ? WHERE id = ?",
                (new_on, new_off, new_json, cam_id),
            )
            changed += 1
    return changed


def apply_settings(cur: sqlite3.Cursor, settings: dict[str, str]) -> None:
    for key, value in settings.items():
        category = key.split(".")[0]
        cur.execute(
            "INSERT INTO settings (key, value, category) VALUES (?, ?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value, category),
        )


def prefix_histogram(cur: sqlite3.Cursor) -> dict[str, Counter]:
    hist: dict[str, Counter] = {}
    for table, col in PATH_COLUMNS:
        c: Counter = Counter()
        cur.execute(f"SELECT {col} FROM {table} WHERE {col} IS NOT NULL")
        for (val,) in cur.fetchall():
            m = re.match(r"^([A-Za-z]:/|/mnt/|/var/|/opt/|//|\\\\)", val.replace("\\", "/"))
            c[m.group(1) if m else val[:12]] += 1
        hist[f"{table}.{col}"] = c
    return hist


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--db", required=True, help="path to richiris.db")
    ap.add_argument("--dry-run", action="store_true", help="report only, no changes")
    ap.add_argument("--reverse", action="store_true", help="map box paths back to Windows")
    args = ap.parse_args()

    db_path = Path(args.db)
    if not db_path.exists():
        print(f"ERROR: {db_path} not found", file=sys.stderr)
        return 1

    if not args.dry_run:
        backup = db_path.with_suffix(".db.pre-migration" if not args.reverse else ".db.pre-reverse")
        shutil.copyfile(db_path, backup)
        print(f"Backup written: {backup}")

    con = sqlite3.connect(db_path)
    cur = con.cursor()

    (integrity,) = cur.execute("PRAGMA integrity_check").fetchone()
    if integrity != "ok":
        print(f"ERROR: integrity_check failed: {integrity}", file=sys.stderr)
        return 1
    print("integrity_check: ok")

    print("\n== BEFORE ==")
    for name, counter in prefix_histogram(cur).items():
        print(f"  {name}: {dict(counter)}")

    if args.dry_run:
        print("\nDry run — no changes made.")
        return 0

    try:
        cur.execute("BEGIN")
        normalize_slashes(cur)
        if args.reverse:
            apply_prefixes(cur, REVERSE_PREFIXES, forward=False)
            cams = rewrite_scripts(cur, REVERSE_SCRIPT_REPLACEMENTS)
            apply_settings(cur, REVERSE_SETTINGS)
        else:
            apply_prefixes(cur, FORWARD_PREFIXES, forward=True)
            cams = rewrite_scripts(cur, FORWARD_SCRIPT_REPLACEMENTS)
            apply_settings(cur, FORWARD_SETTINGS)
        con.commit()
    except Exception:
        con.rollback()
        raise
    print(f"\nRewrote motion scripts on {cams} cameras")

    print("\n== AFTER ==")
    for name, counter in prefix_histogram(cur).items():
        print(f"  {name}: {dict(counter)}")

    # Hard gate: forward migration must leave no non-absolute recording paths
    if not args.reverse:
        (bad,) = cur.execute(
            "SELECT COUNT(*) FROM recordings WHERE file_path NOT LIKE '/%'"
        ).fetchone()
        if bad:
            print(f"\nERROR: {bad} recordings.file_path rows are not absolute POSIX paths — "
                  f"DO NOT start the service. Restore from the .pre-migration backup.",
                  file=sys.stderr)
            return 1
        print("\nAll recordings.file_path rows are absolute POSIX paths. OK.")

    con.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
