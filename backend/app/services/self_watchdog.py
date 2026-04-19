"""In-process HTTP self-watchdog.

Periodically probes the local /api/health endpoint. If it fails repeatedly
the process exits hard, letting NSSM restart it. Catches the failure mode
where uvicorn's HTTP listener dies silently while the asyncio event loop
and background tasks keep running.
"""

import asyncio
import logging
import os

import httpx

logger = logging.getLogger(__name__)

PROBE_INTERVAL_S = 30
PROBE_TIMEOUT_S = 5
MAX_CONSECUTIVE_FAILURES = 3
STARTUP_GRACE_S = 60


async def run_self_watchdog(port: int) -> None:
    """Probe /api/health; kill the process after MAX_CONSECUTIVE_FAILURES."""
    await asyncio.sleep(STARTUP_GRACE_S)
    url = f"http://127.0.0.1:{port}/api/health"
    failures = 0
    logger.info("Self-watchdog started", extra={
        "url": url, "interval_s": PROBE_INTERVAL_S, "max_failures": MAX_CONSECUTIVE_FAILURES,
    })
    while True:
        try:
            async with httpx.AsyncClient(timeout=PROBE_TIMEOUT_S) as client:
                resp = await client.get(url)
            if resp.status_code == 200:
                if failures > 0:
                    logger.info("Self-watchdog recovered", extra={"prior_failures": failures})
                failures = 0
            else:
                failures += 1
                logger.warning("Self-watchdog probe non-200", extra={
                    "status": resp.status_code, "consecutive_failures": failures,
                })
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            failures += 1
            logger.warning("Self-watchdog probe failed", extra={
                "error": str(exc), "consecutive_failures": failures,
            })

        if failures >= MAX_CONSECUTIVE_FAILURES:
            logger.error("Self-watchdog triggering hard exit — NSSM will restart", extra={
                "consecutive_failures": failures,
            })
            os._exit(1)

        await asyncio.sleep(PROBE_INTERVAL_S)
