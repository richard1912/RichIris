"""Phone alert feed: the alert scripts post here, RichMacro on the phone long-polls it.

Exists because the phone alarm used to hang off Gmail push alone, which stopped
arriving on 2026-11-19 with nothing to show for it. The alert scripts (`email.py`,
`loitering.py`) POST the same subject line they email; the phone holds
`GET /api/alerts/wait` open and gets it within a second. Email stays as the backup.

Alerts live in memory only. Ids are millisecond timestamps, so a client's `after`
stays valid across a service restart.
"""

import asyncio
import logging
import time
from collections import deque

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/alerts", tags=["alerts"])

KEEP = 50
# An alert older than this is not replayed to a phone that was offline: an alarm
# about something long over only teaches Richard to ignore it.
MAX_AGE_SECONDS = 15 * 60
MAX_WAIT_SECONDS = 600

_alerts: deque[dict] = deque(maxlen=KEEP)
_waiters: set[asyncio.Event] = set()


class AlertIn(BaseModel):
    subject: str
    body: str = ""


def _now_id() -> int:
    return int(time.time() * 1000)


def _since(after: int) -> list[dict]:
    oldest = _now_id() - MAX_AGE_SECONDS * 1000
    return [a for a in _alerts if a["id"] > after and a["id"] >= oldest]


@router.post("")
async def publish(alert: AlertIn, request: Request) -> dict:
    """Queue an alert for the phone. Local scripts only: anyone who can post here can ring it."""
    host = request.client.host if request.client else ""
    if host not in ("127.0.0.1", "::1"):
        raise HTTPException(status_code=403, detail="Alerts can only be posted from the box itself")
    new_id = max(_now_id(), (_alerts[-1]["id"] + 1) if _alerts else 0)
    _alerts.append({"id": new_id, "subject": alert.subject.strip(), "body": alert.body})
    for ev in list(_waiters):
        ev.set()
    logger.info("Phone alert queued", extra={"subject": alert.subject, "waiters": len(_waiters)})
    return {"id": new_id, "waiters": len(_waiters)}


@router.get("/wait")
async def wait(after: int = 0, timeout: int = 480) -> dict:
    """Long-poll. Returns as soon as an alert newer than `after` exists, or empty at `timeout`.

    `after=0` is a new client: it gets the current id back at once and nothing old.
    The client passes the returned `now` as its next `after`.
    """
    if after <= 0:
        return {"alerts": [], "now": _now_id()}
    found = _since(after)
    if not found:
        ev = asyncio.Event()
        _waiters.add(ev)
        try:
            await asyncio.wait_for(ev.wait(), max(1, min(timeout, MAX_WAIT_SECONDS)))
        except asyncio.TimeoutError:
            pass
        finally:
            _waiters.discard(ev)
        found = _since(after)
    return {"alerts": found, "now": found[-1]["id"] if found else after}


@router.get("")
async def recent() -> dict:
    """What is queued and how many phones are listening (for checking it works)."""
    return {"alerts": list(_alerts), "waiters": len(_waiters), "now": _now_id()}
