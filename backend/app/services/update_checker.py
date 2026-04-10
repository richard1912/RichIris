"""Periodic GitHub release checker with app-launch notification."""

import asyncio
import logging
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import httpx

logger = logging.getLogger(__name__)

GITHUB_API_URL = "https://api.github.com/repos/richard1912/RichIris/releases"
CHECK_INTERVAL = 6 * 3600  # 6 hours
INITIAL_DELAY = 60  # Let service finish starting


def _compare_versions(a: str, b: str) -> int:
    """Compare two semver strings. Returns -1 if a < b, 0 if equal, 1 if a > b."""
    def parts(v: str) -> list[int]:
        return [int(x) for x in v.lstrip("v").split(".")]
    pa, pb = parts(a), parts(b)
    # Pad to same length
    while len(pa) < len(pb):
        pa.append(0)
    while len(pb) < len(pa):
        pb.append(0)
    for x, y in zip(pa, pb):
        if x < y:
            return -1
        if x > y:
            return 1
    return 0


class UpdateChecker:
    """Periodically checks GitHub for new RichIris releases.

    If an update is found and the Flutter app is not running,
    launches it with --update-only so the user sees the prompt
    without wasting resources on camera streams.
    """

    def __init__(self) -> None:
        self._latest_release: dict | None = None
        self._current_version: str = "0.0.0"
        self._last_check: datetime | None = None
        self._task: asyncio.Task | None = None
        self._app_exe: Path | None = None
        self._app_launched_for_version: str | None = None

    async def start(self, current_version: str, app_exe: Path) -> None:
        self._current_version = current_version
        self._app_exe = app_exe
        self._task = asyncio.create_task(self._periodic_check())
        logger.info(
            "Update checker started",
            extra={"current_version": current_version, "interval_h": CHECK_INTERVAL // 3600},
        )

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        logger.info("Update checker stopped")

    @property
    def latest_release(self) -> dict | None:
        return self._latest_release

    @property
    def last_check(self) -> datetime | None:
        return self._last_check

    @property
    def current_version(self) -> str:
        return self._current_version

    async def check_now(self) -> dict | None:
        """Force an immediate check. Returns release info if update available."""
        await self._check_github()
        return self._latest_release

    async def _periodic_check(self) -> None:
        await asyncio.sleep(INITIAL_DELAY)
        while True:
            try:
                await self._check_github()
                if self._latest_release and os.name == "nt":
                    self._maybe_launch_app()
            except Exception:
                logger.exception("Update check cycle failed")
            await asyncio.sleep(CHECK_INTERVAL)

    async def _check_github(self) -> None:
        try:
            async with httpx.AsyncClient(timeout=15) as client:
                resp = await client.get(
                    GITHUB_API_URL,
                    headers={"Accept": "application/vnd.github+json"},
                    params={"per_page": 50},
                )
            self._last_check = datetime.now(timezone.utc)

            if resp.status_code == 403:
                logger.warning("GitHub API rate limited, will retry next cycle")
                return
            if resp.status_code != 200:
                logger.warning(
                    "GitHub API returned non-200",
                    extra={"status": resp.status_code},
                )
                return

            releases = resp.json()
            if not isinstance(releases, list) or not releases:
                self._latest_release = None
                return

            # Filter to releases newer than current, sorted newest first
            newer = []
            for rel in releases:
                if rel.get("draft") or rel.get("prerelease"):
                    continue
                tag = rel.get("tag_name", "")
                ver = tag.lstrip("v")
                if _compare_versions(ver, self._current_version) > 0:
                    newer.append(rel)

            if not newer:
                self._latest_release = None
                latest_tag = releases[0].get("tag_name", "").lstrip("v")
                logger.debug(
                    "No update available",
                    extra={"current": self._current_version, "latest": latest_tag},
                )
                return

            # Latest release (newest) for version/assets/published_at
            latest = newer[0]
            latest_tag = latest.get("tag_name", "")
            latest_version = latest_tag.lstrip("v")

            # Parse assets from latest release. Two Windows installers ship
            # in each release: the full NVR installer (RichIris-Setup.exe) and
            # the client-only app installer (RichIris-Client-Setup.exe). They
            # are stored under separate keys so the Flutter updater can pick
            # the right one based on the install flavor.
            assets: dict[str, dict] = {}
            for asset in latest.get("assets", []):
                name = asset.get("name", "")
                info = {
                    "name": name,
                    "url": asset.get("browser_download_url", ""),
                    "size": asset.get("size", 0),
                }
                if name.endswith(".exe"):
                    if "Client-Setup" in name:
                        assets["windows_client"] = info
                    else:
                        assets["windows"] = info
                elif name.endswith(".apk"):
                    assets["android"] = info

            # Combine changelogs from all newer releases (newest first)
            changelog_parts = []
            for rel in newer:
                tag = rel.get("tag_name", "")
                body = (rel.get("body") or "").strip()
                if body:
                    changelog_parts.append(f"# {tag}\n{body}")
            combined_changelog = "\n\n".join(changelog_parts)

            self._latest_release = {
                "version": latest_version,
                "tag_name": latest_tag,
                "changelog": combined_changelog,
                "published_at": latest.get("published_at", ""),
                "assets": assets,
            }
            logger.info(
                "Update available",
                extra={"current": self._current_version, "latest": latest_version},
            )

        except httpx.TimeoutException:
            logger.warning("GitHub API request timed out")
        except Exception:
            logger.exception("Failed to check GitHub for updates")

    def _is_app_running(self) -> bool:
        """Check if the Flutter app (richiris.exe in app/ dir) is running."""
        if os.name != "nt":
            return False
        try:
            result = subprocess.run(
                ["tasklist", "/fi", "imagename eq richiris.exe", "/fo", "csv", "/nh"],
                capture_output=True,
                text=True,
                timeout=10,
            )
            # The backend is also richiris.exe, so count how many instances exist.
            # If more than 1, the app is likely running alongside the backend.
            count = 0
            for line in result.stdout.strip().splitlines():
                if "richiris.exe" in line.lower():
                    count += 1
            # Backend itself is 1 instance; app would be a second
            return count > 1
        except Exception:
            logger.debug("Failed to check if app is running")
            return False

    def _maybe_launch_app(self) -> None:
        """Launch the Flutter app with --update-only if not already running."""
        if not self._app_exe or not self._app_exe.exists():
            return
        if self._latest_release is None:
            return

        version = self._latest_release["version"]
        if self._app_launched_for_version == version:
            return  # Already launched for this version

        if self._is_app_running():
            return  # App already open, it will check on its own

        try:
            logger.info(
                "Launching app for update notification",
                extra={"version": version, "exe": str(self._app_exe)},
            )
            # DETACHED_PROCESS = 0x00000008
            # CREATE_NEW_PROCESS_GROUP = 0x00000200
            subprocess.Popen(
                [str(self._app_exe), "--update-only"],
                creationflags=0x00000008 | 0x00000200,
                cwd=str(self._app_exe.parent),
            )
            self._app_launched_for_version = version
        except Exception:
            logger.exception("Failed to launch app for update notification")


_update_checker: UpdateChecker | None = None


def get_update_checker() -> UpdateChecker:
    global _update_checker
    if _update_checker is None:
        _update_checker = UpdateChecker()
    return _update_checker
