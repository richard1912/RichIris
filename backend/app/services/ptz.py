"""PTZ control: vendor-neutral types, driver registry and the move deadman.

Whether a camera has PTZ is the `cameras.ptz_driver` column: NULL = no driver
claims the camera's RTSP URL (or it has not been probed yet), "none" = probed
and it has no PTZ, anything else = the name of a registered driver. The router
only ever talks to `PtzManager`, so another vendor is one more `PtzDriver`
subclass plus `@register_driver`.

Connection details are derived from the camera record (host and credentials
from `rtsp_url`), never stored separately.

A continuous move is always time-limited. The client re-sends `move` while a
button is held; every call re-arms a deadman here, and the driver is asked to
arm the camera's own auto-stop too where the protocol has one. If the client
vanishes, the move ends when the deadman expires.
"""

import asyncio
import logging
import time
from dataclasses import dataclass
from urllib.parse import unquote, urlsplit

logger = logging.getLogger(__name__)

NO_PTZ = "none"

SPEED_MIN = 1
SPEED_MAX = 8
DEFAULT_MOVE_TIMEOUT = 2.5
MAX_MOVE_TIMEOUT = 10.0

# direction name -> (pan, tilt, zoom), each -1, 0 or 1.
# pan +1 = view moves right, tilt +1 = view moves up, zoom +1 = zoom in.
DIRECTIONS: dict[str, tuple[int, int, int]] = {
    "left": (-1, 0, 0),
    "right": (1, 0, 0),
    "up": (0, 1, 0),
    "down": (0, -1, 0),
    "up_left": (-1, 1, 0),
    "up_right": (1, 1, 0),
    "down_left": (-1, -1, 0),
    "down_right": (1, -1, 0),
    "zoom_in": (0, 0, 1),
    "zoom_out": (0, 0, -1),
}


class PtzError(Exception):
    """A PTZ request failed. `status_code` is what the API should answer."""

    status_code = 502

    def __init__(self, detail: str, status_code: int | None = None):
        super().__init__(detail)
        self.detail = detail
        if status_code is not None:
            self.status_code = status_code


class PtzTimeout(PtzError):
    status_code = 504


class PtzNotFound(PtzError):
    status_code = 404


class PtzBadRequest(PtzError):
    status_code = 400


@dataclass(frozen=True)
class PtzTarget:
    """Where and how to reach a camera's control API."""

    host: str
    username: str
    password: str
    rtsp_url: str


@dataclass
class PtzCapabilities:
    pan_min: float
    pan_max: float
    tilt_min: float
    tilt_max: float
    zoom_min: float
    zoom_max: float
    max_presets: int
    speed_min: int = SPEED_MIN
    speed_max: int = SPEED_MAX
    absolute: bool = True
    presets: bool = True


@dataclass
class PtzStatus:
    pan: float
    tilt: float
    zoom: float
    moving: bool


@dataclass
class PtzPreset:
    id: int
    name: str
    pan: float | None = None
    tilt: float | None = None
    zoom: float | None = None


def target_from_rtsp(rtsp_url: str) -> PtzTarget | None:
    """Host and credentials from a camera's RTSP URL.

    Credentials are stored percent-encoded (see `rtsp_url.py`), so they are
    decoded here: HTTP auth needs the real characters.
    """
    try:
        parts = urlsplit(rtsp_url or "")
        host = parts.hostname
    except ValueError:
        return None
    if not host:
        return None
    return PtzTarget(
        host=host,
        username=unquote(parts.username or ""),
        password=unquote(parts.password or ""),
        rtsp_url=rtsp_url,
    )


class PtzDriver:
    """One camera's PTZ, spoken in one vendor's protocol.

    Every method raises `PtzError` (or a subclass) on failure and must return
    within a few seconds: no call may hang on an unresponsive camera.
    """

    name = ""

    def __init__(self, target: PtzTarget):
        self.target = target

    @classmethod
    def matches(cls, rtsp_url: str) -> bool:
        """Cheap, offline: could this URL belong to one of this vendor's cameras?"""
        return False

    async def probe(self) -> bool:
        """Ask the camera whether this channel really has PTZ."""
        raise NotImplementedError

    async def capabilities(self) -> PtzCapabilities:
        raise NotImplementedError

    async def status(self) -> PtzStatus:
        raise NotImplementedError

    async def move(self, pan: int, tilt: int, zoom: int, speed: int, timeout: float) -> None:
        """Start (or keep alive) a continuous move. `timeout` is the caller's
        deadman in seconds; arm the camera's own auto-stop if it has one."""
        raise NotImplementedError

    async def stop(self) -> None:
        raise NotImplementedError

    async def goto(self, pan: float, tilt: float, zoom: float) -> None:
        raise NotImplementedError

    async def autofocus(self) -> None:
        """Refocus once on the current view."""
        raise PtzError("This camera's driver has no autofocus command")

    async def list_presets(self) -> list[PtzPreset]:
        raise NotImplementedError

    async def goto_preset(self, preset_id: int) -> None:
        raise NotImplementedError

    async def save_preset(self, preset_id: int, name: str | None) -> None:
        raise NotImplementedError

    async def delete_preset(self, preset_id: int) -> None:
        raise NotImplementedError

    async def close(self) -> None:
        pass


DRIVERS: dict[str, type[PtzDriver]] = {}


def register_driver(cls: type[PtzDriver]) -> type[PtzDriver]:
    DRIVERS[cls.name] = cls
    return cls


def has_ptz(driver_name: str | None) -> bool:
    return bool(driver_name) and driver_name != NO_PTZ


@dataclass
class _ActiveMove:
    direction: str
    deadline: float
    watchdog: asyncio.Task | None = None


class PtzManager:
    """Drivers per camera plus the server-side deadman for continuous moves."""

    STOP_ATTEMPTS = 5
    STOP_RETRY_SECONDS = 1.0

    def __init__(self) -> None:
        self._drivers: dict[int, PtzDriver] = {}
        self._locks: dict[int, asyncio.Lock] = {}
        self._active: dict[int, _ActiveMove] = {}
        self._retry_tasks: dict[int, asyncio.Task] = {}

    # -- drivers -----------------------------------------------------------

    def _lock(self, camera_id: int) -> asyncio.Lock:
        return self._locks.setdefault(camera_id, asyncio.Lock())

    async def driver_for(self, camera) -> PtzDriver:
        """The driver for a camera row, rebuilt if its URL or driver changed."""
        if not has_ptz(camera.ptz_driver):
            raise PtzBadRequest("Camera has no PTZ")
        cls = DRIVERS.get(camera.ptz_driver)
        if cls is None:
            raise PtzBadRequest(f"Unknown PTZ driver '{camera.ptz_driver}'")
        cached = self._drivers.get(camera.id)
        if cached is not None:
            if type(cached) is cls and cached.target.rtsp_url == camera.rtsp_url:
                return cached
            await self.forget(camera.id)
        target = target_from_rtsp(camera.rtsp_url)
        if target is None:
            raise PtzBadRequest("Camera URL has no host to send PTZ commands to")
        driver = cls(target)
        self._drivers[camera.id] = driver
        return driver

    async def forget(self, camera_id: int) -> None:
        """Drop a camera's driver (URL changed, camera deleted)."""
        self._cancel_watchdog(camera_id)
        driver = self._drivers.pop(camera_id, None)
        if driver is not None:
            await driver.close()

    async def detect(self, rtsp_url: str) -> str | None:
        """Which driver, if any, controls the camera behind this URL.

        Returns a driver name, "none" when a driver recognises the URL but the
        camera reports no PTZ, or None when no driver recognises the URL.
        Raises `PtzError` when the camera could not be asked, so a camera that
        is merely offline is not written off as fixed.
        """
        target = target_from_rtsp(rtsp_url)
        if target is None:
            return None
        claimed = False
        for name, cls in DRIVERS.items():
            if not cls.matches(rtsp_url):
                continue
            claimed = True
            driver = cls(target)
            try:
                if await driver.probe():
                    return name
            finally:
                await driver.close()
        return NO_PTZ if claimed else None

    # -- continuous moves --------------------------------------------------

    async def move(self, camera, direction: str, speed: int, timeout: float) -> float:
        """Start or keep alive a continuous move; returns the deadman in seconds."""
        vector = DIRECTIONS.get(direction)
        if vector is None:
            raise PtzBadRequest(f"Unknown direction '{direction}'")
        speed = max(SPEED_MIN, min(SPEED_MAX, int(speed)))
        timeout = max(0.5, min(MAX_MOVE_TIMEOUT, float(timeout)))
        driver = await self.driver_for(camera)
        async with self._lock(camera.id):
            try:
                await driver.move(*vector, speed, timeout)
            except PtzError:
                # The camera may or may not have started. Make sure it stops.
                self._cancel_watchdog(camera.id)
                self._stop_in_background(camera.id, driver)
                raise
            self._arm(camera.id, driver, direction, timeout)
        return timeout

    async def stop(self, camera) -> None:
        driver = await self.driver_for(camera)
        async with self._lock(camera.id):
            self._cancel_watchdog(camera.id)
            try:
                await driver.stop()
            except PtzError:
                self._stop_in_background(camera.id, driver)
                raise

    async def autofocus(self, camera) -> None:
        driver = await self.driver_for(camera)
        async with self._lock(camera.id):
            await driver.autofocus()

    async def _end_move(self, camera_id: int, driver: PtzDriver) -> None:
        """Halt a continuous move before a positioning command replaces it,
        so neither deadman cuts the new move short."""
        if camera_id in self._active:
            self._cancel_watchdog(camera_id)
            await driver.stop()

    # -- positioning and presets -------------------------------------------

    async def goto(self, camera, pan: float | None, tilt: float | None,
                   zoom: float | None) -> PtzStatus:
        """Absolute move. Missing axes keep their current value; everything is
        clamped to the camera's range. Returns the target actually sent."""
        driver = await self.driver_for(camera)
        caps = await driver.capabilities()
        if None in (pan, tilt, zoom):
            now = await driver.status()
            pan = now.pan if pan is None else pan
            tilt = now.tilt if tilt is None else tilt
            zoom = now.zoom if zoom is None else zoom
        pan = max(caps.pan_min, min(caps.pan_max, pan))
        tilt = max(caps.tilt_min, min(caps.tilt_max, tilt))
        zoom = max(caps.zoom_min, min(caps.zoom_max, zoom))
        async with self._lock(camera.id):
            await self._end_move(camera.id, driver)
            await driver.goto(pan, tilt, zoom)
        return PtzStatus(pan=pan, tilt=tilt, zoom=zoom, moving=True)

    async def goto_preset(self, camera, preset_id: int) -> None:
        driver = await self.driver_for(camera)
        async with self._lock(camera.id):
            await self._end_move(camera.id, driver)
            await driver.goto_preset(preset_id)

    async def save_preset(self, camera, preset_id: int | None, name: str | None) -> PtzPreset:
        """Save the current position. Without an id, the lowest free one."""
        driver = await self.driver_for(camera)
        caps = await driver.capabilities()
        if preset_id is None:
            used = {p.id for p in await driver.list_presets()}
            preset_id = next((i for i in range(1, caps.max_presets + 1) if i not in used), None)
            if preset_id is None:
                raise PtzBadRequest("No free preset slot on the camera")
        elif not 1 <= preset_id <= caps.max_presets:
            raise PtzBadRequest(f"Preset id must be 1 to {caps.max_presets}")
        name = (name or "").strip() or None
        await driver.save_preset(preset_id, name)
        for preset in await driver.list_presets():
            if preset.id == preset_id:
                return preset
        raise PtzError("Camera did not keep the preset")

    def _arm(self, camera_id: int, driver: PtzDriver, direction: str, timeout: float) -> None:
        deadline = time.monotonic() + timeout
        active = self._active.get(camera_id)
        if active is not None and active.watchdog is not None and not active.watchdog.done():
            active.direction = direction
            active.deadline = deadline
            return
        active = _ActiveMove(direction=direction, deadline=deadline)
        active.watchdog = asyncio.create_task(self._watchdog(camera_id, driver))
        self._active[camera_id] = active

    def _cancel_watchdog(self, camera_id: int) -> None:
        active = self._active.pop(camera_id, None)
        if active is not None and active.watchdog is not None:
            active.watchdog.cancel()

    async def _watchdog(self, camera_id: int, driver: PtzDriver) -> None:
        """Stop the camera once nobody has renewed the move in time."""
        while True:
            active = self._active.get(camera_id)
            if active is None:
                return
            remaining = active.deadline - time.monotonic()
            if remaining > 0:
                await asyncio.sleep(remaining)
                continue
            async with self._lock(camera_id):
                active = self._active.get(camera_id)
                if active is None:
                    return
                if active.deadline > time.monotonic():
                    continue  # renewed while we waited for the lock
                self._active.pop(camera_id, None)
                logger.warning("PTZ move not renewed, stopping", extra={
                    "camera_id": camera_id, "direction": active.direction,
                })
                if not await self._try_stop(camera_id, driver):
                    self._stop_in_background(camera_id, driver)
                return

    async def _try_stop(self, camera_id: int, driver: PtzDriver) -> bool:
        try:
            await driver.stop()
            return True
        except PtzError as e:
            logger.error("PTZ stop failed", extra={"camera_id": camera_id, "error": e.detail})
            return False

    def _stop_in_background(self, camera_id: int, driver: PtzDriver) -> None:
        """Keep trying to stop a camera whose stop (or start) just failed."""
        existing = self._retry_tasks.get(camera_id)
        if existing is not None and not existing.done():
            return
        self._retry_tasks[camera_id] = asyncio.create_task(self._retry_stop(camera_id, driver))

    async def _retry_stop(self, camera_id: int, driver: PtzDriver) -> None:
        for _ in range(self.STOP_ATTEMPTS):
            await asyncio.sleep(self.STOP_RETRY_SECONDS)
            if camera_id in self._active:
                return  # a newer move took over and has its own deadman
            if await self._try_stop(camera_id, driver):
                return
        logger.error("PTZ stop gave up; the camera's own move timeout is the last resort",
                     extra={"camera_id": camera_id})

    # -- lifecycle ---------------------------------------------------------

    async def shutdown(self) -> None:
        """Stop anything still moving and close every driver."""
        for task in self._retry_tasks.values():
            task.cancel()
        for camera_id in list(self._active):
            driver = self._drivers.get(camera_id)
            self._cancel_watchdog(camera_id)
            if driver is not None:
                await self._try_stop(camera_id, driver)
        for driver in self._drivers.values():
            await driver.close()
        self._drivers.clear()


_manager: PtzManager | None = None


def get_ptz_manager() -> PtzManager:
    global _manager
    if _manager is None:
        # Importing the vendor modules registers their drivers.
        from app.services import ptz_dahua  # noqa: F401
        _manager = PtzManager()
    return _manager


# -- capability detection ------------------------------------------------------

async def refresh_camera_ptz(camera) -> bool:
    """Probe a camera and update `camera.ptz_driver` (caller commits).

    Returns True when the value changed. A camera that cannot be reached keeps
    its current value, so an outage never turns a PTZ camera into a fixed one.
    """
    mgr = get_ptz_manager()
    try:
        detected = await mgr.detect(camera.rtsp_url)
    except PtzError as e:
        logger.warning("PTZ detection failed, keeping current value", extra={
            "camera_id": camera.id, "camera_name": camera.name, "error": e.detail,
        })
        return False
    if detected == camera.ptz_driver:
        return False
    camera.ptz_driver = detected
    await mgr.forget(camera.id)
    logger.info("PTZ capability detected", extra={
        "camera_id": camera.id, "camera_name": camera.name, "ptz_driver": detected,
    })
    return True


async def detect_unprobed_cameras() -> None:
    """Startup pass: probe cameras whose `ptz_driver` is still NULL.

    Only cameras whose URL a driver recognises are contacted, so cameras from
    other vendors never see a request. Runs in the background: nothing at
    startup waits on a camera's HTTP server.
    """
    from sqlalchemy import select

    from app.database import get_session_factory
    from app.models import Camera

    get_ptz_manager()  # registers the drivers
    try:
        async with get_session_factory()() as session:
            rows = (await session.execute(
                select(Camera).where(Camera.ptz_driver.is_(None))
            )).scalars().all()
            changed = False
            for camera in rows:
                if any(cls.matches(camera.rtsp_url) for cls in DRIVERS.values()):
                    changed = await refresh_camera_ptz(camera) or changed
            if changed:
                await session.commit()
    except Exception:
        logger.exception("PTZ startup detection failed")
