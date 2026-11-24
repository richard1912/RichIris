"""Dahua PTZ driver (HTTP CGI `ptz.cgi`, Digest auth).

Written against a DH-SDT3E410 dual-lens camera; every call below was checked
on that unit (2026-11-16). Things worth knowing:

- The channel is the `channel=` parameter of the RTSP URL, counted from 1. On
  a dual-lens unit only one channel is the PTZ; the fixed lens still answers
  `getStatus` (with zeros), so `probe()` reads `caps.Pan/Tile/Zoom` instead.
- Continuous moves use `code=Continuously`: arg1 pan speed (-8..8, negative =
  left), arg2 tilt speed (-8..8, positive = up), arg3 zoom speed (-100..100,
  positive = in), arg4 = seconds until the CAMERA stops by itself. Re-sending
  it mid-move is seamless and restarts that timer, so the camera stops on its
  own even if this process dies.
- Absolute moves use `code=PositionABS`: pan and tilt in degrees, zoom as an
  integer 1..128 that maps linearly onto the 1x..10x the camera reports in
  `status.AbsPosition[2]` (x100). `status.ZoomValue` is a different scale
  (569 at full zoom) and is not used.
- Tilt is reported in degrees with UP as negative (range -10..90).
- One-shot autofocus is the JSON-RPC call `LensFunc.autoBackFocus {channel}`
  with the channel counted from ZERO (RTSP channel minus 1). It needs an RPC
  session (`/RPC2_Login`, see `_rpc`); there is no CGI form of it (501).
  Tested by defocusing with `FocusNear`: `status.Focus.Status` goes `Focusing`
  and the lens returns to its sharp position within about a second.
  What does NOT work on this unit: `devVideoInput.cgi?action=autoFocus`
  (answers OK, does nothing), RPC `devVideoInput.autoFocus`, `ptz.setFocusMode`
  and `ptz.focusRegion` (all error). The lens is in semi-auto mode: it also
  refocuses by itself after a zoom change, but not after a pan or tilt alone.
"""

import hashlib
import logging
import math
import re
from urllib.parse import parse_qs, quote, urlsplit

import httpx

from app.services.ptz import (
    PtzCapabilities,
    PtzDriver,
    PtzError,
    PtzNotFound,
    PtzPreset,
    PtzStatus,
    PtzTarget,
    PtzTimeout,
    register_driver,
)

logger = logging.getLogger(__name__)

_TIMEOUT = httpx.Timeout(4.0, connect=2.5)
_ZOOM_STEPS = 128  # PositionABS arg3 and preset Position[2] run 1..128
_ZOOM_SPEED_PER_STEP = 100 / 8  # API speed 1..8 -> Continuously arg3 12.5..100
_INDEXED = re.compile(r"^presets\[(\d+)\]\.(.+)$")
_NAME_MAX = 40
# The camera cuts a preset name at the first "&" even when it is percent-encoded,
# so anything that could be read as query syntax is replaced before sending.
_NAME_UNSAFE = re.compile(r"[^\w .,()\-]+")


def _channel_of(rtsp_url: str) -> int | None:
    try:
        parts = urlsplit(rtsp_url or "")
    except ValueError:
        return None
    if "/cam/realmonitor" not in parts.path.lower():
        return None
    try:
        return int(parse_qs(parts.query).get("channel", [""])[0])
    except ValueError:
        return None


def _parse_kv(text: str) -> dict[str, str]:
    """`key=value` lines, as every Dahua CGI answers."""
    out: dict[str, str] = {}
    for line in text.splitlines():
        key, sep, value = line.strip().partition("=")
        if sep:
            out[key] = value
    return out


def _num(d: dict[str, str], key: str, default: float = 0.0) -> float:
    try:
        return float(d[key])
    except (KeyError, ValueError):
        return default


class _CameraRefused(PtzError):
    """The camera answered, but not with 200 (bad channel, unknown preset...)."""

    def __init__(self, http_status: int):
        super().__init__(f"Camera refused the request (HTTP {http_status})")
        self.http_status = http_status


@register_driver
class DahuaPtzDriver(PtzDriver):
    name = "dahua"

    def __init__(self, target: PtzTarget):
        super().__init__(target)
        self.channel = _channel_of(target.rtsp_url) or 1
        self._client: httpx.AsyncClient | None = None
        self._caps: PtzCapabilities | None = None
        self._rpc_session: str | None = None

    @classmethod
    def matches(cls, rtsp_url: str) -> bool:
        return _channel_of(rtsp_url) is not None

    # -- transport ---------------------------------------------------------

    def _http(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                base_url=f"http://{self.target.host}/cgi-bin/",
                auth=httpx.DigestAuth(self.target.username, self.target.password),
                timeout=_TIMEOUT,
            )
        return self._client

    async def _get(self, query: str) -> str:
        """GET `ptz.cgi?<query>&channel=N` and return the body, or raise PtzError."""
        url = f"ptz.cgi?{query}&channel={self.channel}"
        try:
            resp = await self._http().get(url)
        except httpx.TimeoutException:
            raise PtzTimeout("Camera did not respond") from None
        except httpx.HTTPError as e:
            raise PtzError(f"Camera unreachable ({type(e).__name__})") from None
        if resp.status_code == 401:
            raise PtzError("Camera rejected the credentials")
        if resp.status_code != 200:
            raise _CameraRefused(resp.status_code)
        return resp.text

    async def _command(self, code: str, arg1=0, arg2=0, arg3=0, arg4=None, action: str = "start") -> None:
        query = f"action={action}&code={code}&arg1={arg1}&arg2={arg2}&arg3={arg3}"
        if arg4 is not None:
            query += f"&arg4={arg4}"
        body = await self._get(query)
        if "OK" not in body:
            raise PtzError(f"Camera refused {code}")

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    # -- capability --------------------------------------------------------

    async def _read_caps(self) -> dict[str, str]:
        return _parse_kv(await self._get("action=getCurrentProtocolCaps"))

    async def probe(self) -> bool:
        try:
            caps = await self._read_caps()
        except _CameraRefused:
            return False  # no such channel / no PTZ service on this model
        return any(caps.get(f"caps.{k}") == "true" for k in ("Pan", "Tile", "Zoom"))

    async def capabilities(self) -> PtzCapabilities:
        if self._caps is None:
            c = await self._read_caps()
            rng = "caps.PtzMotionRange."
            self._caps = PtzCapabilities(
                pan_min=_num(c, rng + "HorizontalAngle[0]", 0),
                pan_max=_num(c, rng + "HorizontalAngle[1]", 360),
                tilt_min=_num(c, rng + "VerticalAngle[0]", -90),
                tilt_max=_num(c, rng + "VerticalAngle[1]", 90),
                zoom_min=_num(c, "caps.ZoomMin", 1),
                zoom_max=_num(c, "caps.ZoomMax", 1),
                max_presets=int(_num(c, "caps.PresetMax", 0)),
                presets=c.get("caps.Preset") == "true",
            )
        return self._caps

    # -- position ----------------------------------------------------------

    async def status(self) -> PtzStatus:
        s = _parse_kv(await self._get("action=getStatus"))
        busy = {s.get("status.MoveStatus"), s.get("status.ZoomStatus")} - {"Idle", None}
        return PtzStatus(
            pan=round(_num(s, "status.Postion[0]"), 2),  # sic: firmware spelling
            tilt=round(_num(s, "status.Postion[1]"), 2),
            zoom=round(_num(s, "status.AbsPosition[2]", 100) / 100, 2),
            moving=bool(busy),
        )

    async def move(self, pan: int, tilt: int, zoom: int, speed: int, timeout: float) -> None:
        camera_timeout = max(1, math.ceil(timeout) + 1)
        await self._command(
            "Continuously",
            arg1=pan * speed,
            arg2=tilt * speed,
            arg3=round(zoom * speed * _ZOOM_SPEED_PER_STEP),
            arg4=camera_timeout,
        )

    async def stop(self) -> None:
        await self._command("Continuously", arg4=0, action="stop")

    # -- JSON-RPC (only what ptz.cgi cannot do) -----------------------------

    async def _rpc_post(self, path: str, body: dict, session: str | None = None) -> dict:
        headers = {}
        if session:
            body = {**body, "session": session}
            headers["Cookie"] = f"WebClientSessionID={session}"  # required as well as the body field
        try:
            resp = await self._http().post(f"http://{self.target.host}{path}", json=body,
                                           headers=headers, auth=None)
            return resp.json()
        except httpx.TimeoutException:
            raise PtzTimeout("Camera did not respond") from None
        except (httpx.HTTPError, ValueError) as e:
            raise PtzError(f"Camera unreachable ({type(e).__name__})") from None

    async def _rpc_login(self) -> str:
        user, password = self.target.username, self.target.password

        def md5(text: str) -> str:
            return hashlib.md5(text.encode()).hexdigest().upper()

        login = {"userName": user, "password": "", "clientType": "Web3.0", "loginType": "Direct"}
        first = await self._rpc_post("/RPC2_Login", {"method": "global.login", "params": login, "id": 1})
        challenge, session = first.get("params") or {}, first.get("session")
        if not session or "random" not in challenge:
            raise PtzError("Camera refused the RPC login")
        login["password"] = md5(f"{user}:{challenge['random']}:" + md5(f"{user}:{challenge['realm']}:{password}"))
        login["authorityType"] = challenge.get("encryption", "Default")
        second = await self._rpc_post("/RPC2_Login", {"method": "global.login", "params": login, "id": 2}, session)
        if not second.get("result"):
            raise PtzError("Camera rejected the credentials")
        return second.get("session") or session

    async def _rpc(self, method: str, params: dict) -> dict:
        """One RPC call on a cached session, logging in again if it has expired."""
        for attempt in (1, 2):
            if self._rpc_session is None:
                self._rpc_session = await self._rpc_login()
            reply = await self._rpc_post("/RPC2", {"method": method, "params": params, "id": 3},
                                         self._rpc_session)
            if reply.get("result"):
                return reply
            self._rpc_session = None  # most likely an expired session: retry once
        raise PtzError(f"Camera refused {method}")

    async def autofocus(self) -> None:
        await self._rpc("LensFunc.autoBackFocus", {"channel": self.channel - 1})

    async def _zoom_to_steps(self, zoom: float) -> int:
        caps = await self.capabilities()
        span = max(caps.zoom_max - caps.zoom_min, 0.001)
        # The camera truncates, so round up to land on (not just under) the target.
        step = 1 + math.ceil((zoom - caps.zoom_min) * (_ZOOM_STEPS - 1) / span - 1e-6)
        return max(1, min(_ZOOM_STEPS, step))

    async def _steps_to_zoom(self, steps: float) -> float:
        caps = await self.capabilities()
        span = caps.zoom_max - caps.zoom_min
        zoom = caps.zoom_min + (max(steps, 1) - 1) * span / (_ZOOM_STEPS - 1)
        return round(zoom, 1)  # the camera reports in 0.1x steps

    async def goto(self, pan: float, tilt: float, zoom: float) -> None:
        await self._command(
            "PositionABS", arg1=round(pan, 2), arg2=round(tilt, 2),
            arg3=await self._zoom_to_steps(zoom),
        )

    # -- presets -----------------------------------------------------------

    async def list_presets(self) -> list[PtzPreset]:
        rows: dict[int, dict[str, str]] = {}
        for key, value in _parse_kv(await self._get("action=getPresets")).items():
            m = _INDEXED.match(key)
            if m:
                rows.setdefault(int(m.group(1)), {})[m.group(2)] = value
        presets = []
        for row in rows.values():
            if "Index" not in row:
                continue
            preset = PtzPreset(id=int(row["Index"]), name=row.get("Name", ""))
            if "Position[0]" in row:  # tenths of a degree, zoom in 1..128 steps
                preset.pan = _num(row, "Position[0]") / 10
                preset.tilt = _num(row, "Position[1]") / 10
                preset.zoom = await self._steps_to_zoom(_num(row, "Position[2]", 1))
            presets.append(preset)
        return sorted(presets, key=lambda p: p.id)

    async def goto_preset(self, preset_id: int) -> None:
        try:
            await self._command("GotoPreset", arg2=preset_id)
        except _CameraRefused:
            raise PtzNotFound("Preset not found") from None

    async def save_preset(self, preset_id: int, name: str | None) -> None:
        await self._command("SetPreset", arg2=preset_id)
        name = " ".join(_NAME_UNSAFE.sub(" ", name or "").split())[:_NAME_MAX]
        if name:
            # Separate call: `action=setPreset` names an existing preset.
            body = await self._get(
                f"action=setPreset&arg1={preset_id}&arg2={quote(name, safe='')}"
            )
            if "OK" not in body:
                raise PtzError("Preset saved but the camera refused the name")

    async def delete_preset(self, preset_id: int) -> None:
        await self._command("ClearPreset", arg2=preset_id)

