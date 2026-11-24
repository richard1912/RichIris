#!/usr/bin/env python3
"""Prius Cam (Pixel 6a "Car Cam" app) health monitor. Run every 5 min by
richiris-carcam-monitor.timer. Polls the phone's admin API over WireGuard and emails
Richard (Gmail, same creds as the other RichIris alerts) when something needs attention:

  - unreachable for > OFFLINE_MIN (phone dead, rebooted to PIN screen, no signal, VPN down)
  - local storage free < FREE_GB_WARN (the app purges oldest at its own cap; this catches
    the cap being misconfigured or another app filling the disk)
  - local recording not advancing (newest segment unchanged for > STALL_MIN)
  - the last finished segment is nearly empty (the recorder is writing files with no video
    in them; this happened silently for a whole night on 2026-11-14)
  - battery < BATTERY_WARN while unplugged, or battery temp > TEMP_WARN
  - pipeline not running

One email per condition when it starts and one when it clears. State in STATE_FILE.
Test: --dry-run prints what it would send; --status prints the parsed status.
"""
import json
import os
import smtplib
import sys
import time
import urllib.request
from email.message import EmailMessage
from pathlib import Path

ADMIN = os.environ.get("CARCAM_ADMIN", "http://10.10.10.16:8080")
MAIL_ENV = Path("/opt/richiris/secrets/mail.env")
STATE_FILE = Path("/var/lib/richiris/carcam/monitor_state.json")
OFFLINE_MIN = 15
FREE_GB_WARN = 10
STALL_MIN = 15
BATTERY_WARN = 25
TEMP_WARN = 50
EMPTY_BYTES = 1_000_000   # a real 5 min 4K segment is ~190 MB; a broken one was 564 bytes
SUBJECT = "Prius Cam Alert:"


def fetch_status():
    with urllib.request.urlopen(f"{ADMIN}/status", timeout=20) as r:
        return json.load(r)


def last_finished_segment():
    """(name, bytes) of the newest segment that is no longer being written, or None."""
    try:
        with urllib.request.urlopen(f"{ADMIN}/recordings", timeout=20) as r:
            files = sorted(json.load(r).get("files", []), key=lambda f: f["name"])
        return (files[-2]["name"], files[-2]["bytes"]) if len(files) >= 2 else None
    except Exception:
        return None


def load_state():
    try:
        return json.loads(STATE_FILE.read_text())
    except Exception:
        return {"active": {}, "last_ok": time.time(), "newest": "", "newest_since": time.time()}


def save_state(st):
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(st))


def conditions(st, status, now):
    """Return {key: message} for every condition currently true."""
    out = {}
    if status is None:
        mins = (now - st["last_ok"]) / 60
        if mins > OFFLINE_MIN:
            out["offline"] = (f"Prius Cam has been unreachable for {mins:.0f} minutes. "
                              "Likely causes: no mobile signal, VPN down, flat battery, or a reboot "
                              "waiting on the PIN screen.")
        return out
    b, store = status.get("battery", {}), status.get("storage", {})
    if not status.get("running"):
        out["pipeline"] = f"Camera pipeline is not running. Last error: {status.get('lastError') or 'none'}."
    free = store.get("freeGb", 999)
    if free < FREE_GB_WARN:
        out["storage"] = (f"Phone storage is low: {free:.1f} GB free, recordings use "
                          f"{store.get('usedGb', 0):.1f} GB in {store.get('files', 0)} files.")
    if st["newest_stalled_min"] > STALL_MIN:
        out["recording"] = (f"Local recording has not started a new segment for "
                            f"{st['newest_stalled_min']:.0f} minutes (newest: {store.get('newest')}).")
    seg = st.get("last_finished")
    if seg and seg[1] < EMPTY_BYTES:
        out["empty"] = (f"The last finished recording on the phone is empty ({seg[0]}, {seg[1]} bytes). "
                        "The camera is running but nothing is being saved. Restart the app pipeline: "
                        f"{ADMIN}/restart")
    if not b.get("plugged") and b.get("level", 100) < BATTERY_WARN:
        out["battery"] = f"Phone battery is at {b.get('level')}% and not charging. Swap the car battery."
    if b.get("tempC", 0) > TEMP_WARN:
        out["heat"] = f"Phone battery temperature is {b.get('tempC')} C. The car is getting too hot."
    return out


def send(subject, body, dry):
    if dry:
        print(f"[dry-run] {subject}\n{body}\n")
        return
    env = dict(l.split("=", 1) for l in MAIL_ENV.read_text().splitlines() if "=" in l)
    msg = EmailMessage()
    msg["From"] = msg["To"] = env["GMAIL_USERNAME"]
    msg["Subject"] = subject
    msg.set_content(body)
    msg.add_alternative("".join(f"<p>{p}</p>" for p in body.split("\n\n")), subtype="html")
    with smtplib.SMTP_SSL("smtp.gmail.com", 465, local_hostname="richiris", timeout=30) as s:
        s.login(env["GMAIL_USERNAME"], env["GMAIL_APP_PASSWORD"])
        s.send_message(msg)


def main():
    dry = "--dry-run" in sys.argv
    now = time.time()
    st = load_state()
    st.setdefault("newest_stalled_min", 0)
    try:
        status = fetch_status()
    except Exception:
        status = None
    if "--status" in sys.argv:
        print(json.dumps(status, indent=1))
        return
    if status is not None:
        st["last_ok"] = now
        newest = status.get("storage", {}).get("newest", "")
        if newest != st.get("newest"):
            st["newest"], st["newest_since"] = newest, now
        st["newest_stalled_min"] = (now - st["newest_since"]) / 60
        st["last_finished"] = last_finished_segment()
    current = conditions(st, status, now)
    active = st["active"]
    for key, text in current.items():
        if key not in active:
            send(f"{SUBJECT} {key}", text + f"\n\nAdmin API: {ADMIN}/status", dry)
            active[key] = now
    for key in list(active):
        if key not in current:
            send(f"{SUBJECT} {key} cleared", f"The {key} condition on Prius Cam has cleared.", dry)
            del active[key]
    if not dry:
        save_state(st)


if __name__ == "__main__":
    main()
