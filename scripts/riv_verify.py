#!/usr/bin/env python3
"""Compare RIV recordings with the MPEG-TS ones for cameras in shadow mode.

For every camera whose rec_format is "shadow", over the last --hours:
  * coverage: seconds of footage in riv_files (s1) vs recordings (.ts), and
    the longest stretch the .ts has that RIV does not (missed footage);
  * decode: one random minute of RIV fetched from the API and run through
    `ffmpeg -f null` (decode errors other than the camera's own repeated-dts
    warning count as a failure);
  * accuracy: the first frame of that minute must start at or before the
    requested time and within one GOP (<= 5 s) of it.

Read-only. Exit status 1 when any camera fails, so it can gate a cutover.
Run on the box: /opt/richiris/venv/bin/python scripts/riv_verify.py --hours 24
"""

from __future__ import annotations

import argparse
import json
import random
import sqlite3
import subprocess
import sys
import tempfile
import urllib.request
from datetime import datetime, timedelta

DB = "/var/lib/richiris/database/richiris.db"
API = "http://127.0.0.1:8700"


def spans(rows):
    out = []
    for s, e in rows:
        if s and e:
            out.append((datetime.fromisoformat(s), datetime.fromisoformat(e)))
    out.sort()
    merged = []
    for s, e in out:
        if merged and s <= merged[-1][1] + timedelta(seconds=5):
            merged[-1] = (merged[-1][0], max(merged[-1][1], e))
        else:
            merged.append((s, e))
    return merged


def total(sp):
    return sum((e - s).total_seconds() for s, e in sp)


def uncovered(ts_spans, riv_spans, t0):
    """Longest .ts stretch (after t0) not covered by RIV, in seconds."""
    worst = 0.0
    for s, e in ts_spans:
        s = max(s, t0)
        cur = s
        for rs, re_ in riv_spans:
            if re_ <= cur or rs >= e:
                continue
            if rs > cur:
                worst = max(worst, (min(rs, e) - cur).total_seconds())
            cur = max(cur, re_)
            if cur >= e:
                break
        if cur < e:
            worst = max(worst, (e - cur).total_seconds())
    return worst


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--hours", type=float, default=24)
    a = ap.parse_args()
    db = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    now = datetime.now()
    t0 = now - timedelta(hours=a.hours)
    failed = False
    cams = db.execute("select id, name from cameras where rec_format='shadow'").fetchall()
    if not cams:
        print("no cameras in shadow mode")
    for cam_id, name in cams:
        riv = spans(db.execute(
            "select start_time, coalesce(end_time, ?) from riv_files where camera_id=? and stream='s1' "
            "and coalesce(end_time, ?) > ?", (now.isoformat(" "), cam_id, now.isoformat(" "),
                                              t0.isoformat(" "))).fetchall())
        if not riv:
            print(f"{name}: no RIV files yet")
            failed = True
            continue
        first_riv = riv[0][0]
        ts = spans(db.execute(
            "select start_time, end_time from recordings where camera_id=? and end_time > ?",
            (cam_id, max(t0, first_riv).isoformat(" "))).fetchall())
        miss = uncovered(ts, riv, first_riv)
        # decode + accuracy on a random closed minute
        lo = max(first_riv, t0) + timedelta(minutes=1)
        hi = now - timedelta(minutes=3)
        verdict = "ok"
        if hi > lo:
            at = lo + timedelta(seconds=random.uniform(0, (hi - lo).total_seconds()))
            at = at.replace(microsecond=0)
            url = (f"{API}/api/riv/{cam_id}/stream.mp4?start={at.isoformat()}"
                   f"&end={(at + timedelta(minutes=1)).isoformat()}")
            with tempfile.NamedTemporaryFile(suffix=".mp4") as tmp:
                with urllib.request.urlopen(url, timeout=60) as r:
                    start_hdr = r.headers.get("X-Riv-Start")
                    tmp.write(r.read())
                tmp.flush()
                p = subprocess.run(["ffmpeg", "-v", "error", "-i", tmp.name, "-fps_mode",
                                    "passthrough", "-f", "null", "-"], capture_output=True, text=True)
                bad = [l for l in p.stderr.splitlines()
                       if l.strip() and "monotonically" not in l and "repeated" not in l]
                lead = (at - datetime.fromisoformat(start_hdr)).total_seconds() if start_hdr else -1
                if p.returncode != 0 or bad:
                    verdict = f"DECODE ERRORS at {at:%H:%M:%S}: {bad[:2]}"
                elif not 0 <= lead <= 5:
                    verdict = f"START OFF by {lead:.2f}s at {at:%H:%M:%S}"
                else:
                    verdict = f"ok (minute at {at:%H:%M:%S} decodes, starts {lead:.2f}s early)"
        ok = miss <= 30 and verdict.startswith("ok")
        failed |= not ok
        print(f"{name}: riv {total(riv)/3600:.2f} h, ts {total(ts)/3600:.2f} h since "
              f"{first_riv:%m-%d %H:%M}, longest ts-only stretch {miss:.0f}s; {verdict}"
              f"{'' if ok else '  <-- FAIL'}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
