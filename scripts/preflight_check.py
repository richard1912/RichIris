#!/usr/bin/env python3
"""Pre-flight gate before the first RichIris start on the Debian box.

Verifies storage mounts, DB path integrity (the cleanup_missing_recordings
landmine gate), VAAPI capability, and binaries. Exits non-zero on any hard
failure — DO NOT start the service until this passes.

Usage:
    python3 preflight_check.py [--db /var/lib/richiris/database/richiris.db]
"""

import argparse
import os
import random
import sqlite3
import subprocess
import sys
from pathlib import Path

DATA_DIR = Path("/var/lib/richiris")
ARCHIVE_DIR = Path("/mnt/backup/RichIris-Archive")
GO2RTC = Path("/opt/richiris/dependencies/go2rtc/go2rtc")
BOOTSTRAP = Path("/opt/richiris/bootstrap.yaml")

failures: list[str] = []
warnings: list[str] = []


def ok(msg: str) -> None:
    print(f"  [OK]   {msg}")


def fail(msg: str) -> None:
    failures.append(msg)
    print(f"  [FAIL] {msg}")


def warn(msg: str) -> None:
    warnings.append(msg)
    print(f"  [WARN] {msg}")


def check_mounts() -> None:
    print("\n== Mounts ==")
    for path, name in [(DATA_DIR, "data dir (loop image)"), (Path("/mnt/backup"), "archive volume")]:
        if not path.exists():
            fail(f"{name} {path} does not exist")
            continue
        try:
            if path.stat().st_dev == path.parent.stat().st_dev:
                fail(f"{name} {path} is NOT a separate mount (st_dev matches parent) — unmounted?")
            else:
                ok(f"{name} {path} is mounted")
        except OSError as e:
            fail(f"{name} {path}: {e}")
    if not ARCHIVE_DIR.is_dir():
        fail(f"archive dir {ARCHIVE_DIR} missing")
    else:
        ok(f"archive dir {ARCHIVE_DIR} present")


def check_db(db_path: Path) -> None:
    print("\n== Database ==")
    if not db_path.exists():
        fail(f"DB not found at {db_path}")
        return
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    cur = con.cursor()
    (integrity,) = cur.execute("PRAGMA integrity_check").fetchone()
    if integrity != "ok":
        fail(f"integrity_check: {integrity}")
        return
    ok("integrity_check: ok")

    (nonposix,) = cur.execute("SELECT COUNT(*) FROM recordings WHERE file_path NOT LIKE '/%'").fetchone()
    if nonposix:
        fail(f"{nonposix} recordings.file_path rows are not POSIX paths — migration not run?")

    # THE landmine gate: every recording row must resolve on disk.
    rows = cur.execute("SELECT file_path, tier FROM recordings").fetchall()
    missing = {"archive": 0, "hot": 0}
    total = {"archive": 0, "hot": 0}
    for file_path, tier in rows:
        t = tier if tier in missing else "archive"
        total[t] += 1
        if not Path(file_path).exists():
            missing[t] += 1
    for tier in ("archive", "hot"):
        if missing[tier] == 0:
            ok(f"{tier}: all {total[tier]} recording files present")
        else:
            msg = f"{tier}: {missing[tier]}/{total[tier]} recording files MISSING"
            if tier == "archive" or missing[tier] > 5:
                fail(msg + " — cleanup_missing_recordings would purge these rows")
            else:
                warn(msg)

    # Thumbnail spot check (warn-only; stale thumbs just 404 in the UI)
    thumb_rows = cur.execute(
        "SELECT thumbnail_path FROM motion_events WHERE thumbnail_path IS NOT NULL"
    ).fetchall()
    if thumb_rows:
        sample = random.sample(thumb_rows, min(20, len(thumb_rows)))
        missing_thumbs = sum(1 for (p,) in sample if not Path(p).exists())
        if missing_thumbs:
            warn(f"{missing_thumbs}/{len(sample)} sampled motion-event thumbnails missing (cosmetic)")
        else:
            ok(f"all {len(sample)} sampled motion-event thumbnails present")

    # Settings sanity
    settings = dict(cur.execute("SELECT key, value FROM settings").fetchall())
    for key, expect in [("storage.archive_dir", str(ARCHIVE_DIR)), ("ffmpeg.hwaccel", "vaapi")]:
        got = settings.get(key)
        if got == expect:
            ok(f"settings {key} = {got}")
        else:
            fail(f"settings {key} = {got!r}, expected {expect!r}")
    if settings.get("ai.remote_url"):
        ok(f"settings ai.remote_url = {settings['ai.remote_url']}")
    else:
        warn("settings ai.remote_url empty — AI will run on local CPU")
    con.close()


def check_binaries() -> None:
    print("\n== Binaries / VAAPI ==")
    # ffmpeg
    try:
        out = subprocess.run(["ffmpeg", "-version"], capture_output=True, text=True, timeout=10)
        ok("ffmpeg: " + out.stdout.splitlines()[0])
    except Exception as e:
        fail(f"ffmpeg not runnable: {e}")
        return
    enc = subprocess.run(["ffmpeg", "-hide_banner", "-encoders"], capture_output=True, text=True, timeout=10)
    for codec in ("hevc_vaapi", "h264_vaapi"):
        if codec in enc.stdout:
            ok(f"ffmpeg encoder {codec} available")
        else:
            fail(f"ffmpeg encoder {codec} MISSING")
    # vainfo
    try:
        va = subprocess.run(["vainfo"], capture_output=True, text=True, timeout=10)
        va_out = va.stdout + va.stderr
        for profile, label in [("VAProfileHEVCMain", "HEVC"), ("VAProfileH264High", "H264")]:
            lines = [ln for ln in va_out.splitlines() if profile in ln and "EncSlice" in ln]
            if lines:
                ok(f"VAAPI {label} encode entrypoint present")
            else:
                fail(f"VAAPI {label} EncSlice entrypoint MISSING (vainfo)")
    except FileNotFoundError:
        fail("vainfo not installed")
    # /dev/dri access
    render = Path("/dev/dri/renderD128")
    if render.exists() and os.access(render, os.R_OK | os.W_OK):
        ok("renderD128 accessible by current user")
    elif render.exists():
        warn("renderD128 exists but not accessible by current user (systemd unit adds video/render groups)")
    else:
        fail("/dev/dri/renderD128 missing")
    # go2rtc
    if GO2RTC.exists() and os.access(GO2RTC, os.X_OK):
        try:
            v = subprocess.run([str(GO2RTC), "--version"], capture_output=True, text=True, timeout=10)
            ok(f"go2rtc: {(v.stdout or v.stderr).strip()}")
        except Exception as e:
            warn(f"go2rtc present but --version failed: {e}")
    else:
        fail(f"go2rtc binary missing or not executable at {GO2RTC}")


def check_bootstrap() -> None:
    print("\n== Bootstrap ==")
    if not BOOTSTRAP.exists():
        fail(f"{BOOTSTRAP} missing")
        return
    text = BOOTSTRAP.read_text()
    if str(DATA_DIR) in text:
        ok(f"bootstrap.yaml data_dir points at {DATA_DIR}")
    else:
        fail(f"bootstrap.yaml does not reference {DATA_DIR}: {text.strip()!r}")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--db", default=str(DATA_DIR / "database" / "richiris.db"))
    args = ap.parse_args()

    check_mounts()
    check_db(Path(args.db))
    check_binaries()
    check_bootstrap()

    print(f"\n{'='*50}")
    if failures:
        print(f"PRE-FLIGHT FAILED: {len(failures)} failure(s), {len(warnings)} warning(s)")
        for f in failures:
            print(f"  - {f}")
        print("DO NOT start the service.")
        return 1
    print(f"PRE-FLIGHT PASSED ({len(warnings)} warning(s)). Safe to start richiris.service.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
