"""Drive enumeration: media type (SSD/HDD) + SMART health per volume.

Used by the Storage settings UI so the user can see what they're choosing before
putting an archive (or hot) tier on a drive — and so a drive in SMART Warning /
Predictive Failure is flagged loudly (exactly the failing-G: situation that
motivated two-tier storage in the first place).

Windows: shells out to PowerShell Storage cmdlets and joins volume → partition →
physical disk. Linux: enumerates /proc/mounts + lsblk, with optional smartctl
health. Both degrade gracefully: if the tooling is unavailable, drives are
still returned with media_type/health = "Unknown".

The "letter" field keeps its name for API/UI compatibility; on Linux it holds
the mount point (e.g. "/", "/mnt/backup") instead of a drive letter.
"""

import json
import logging
import os
import shutil
import string
import subprocess
import sys
import threading
import time
from pathlib import Path

logger = logging.getLogger(__name__)

# Drive enumeration shells out (~hundreds of ms, occasionally slow under load).
# The Storage panel polls status every few seconds, so cache the result briefly
# and serve the last good list if a refresh fails — never blank the UI's
# media-type/health just because one call timed out.
_CACHE_TTL = 30.0
_cache_lock = threading.Lock()
_cache: dict | None = None  # {"drives": list[dict], "ts": float}

# Single PS pass: map each lettered volume to its physical disk's media type +
# health, emit a JSON array of {letter,label,total,free,media_type,health,operational}.
_PS_SCRIPT = r"""
$ErrorActionPreference = 'SilentlyContinue'
$parts = @{}
foreach ($p in (Get-Partition | Where-Object { $_.DriveLetter })) {
  $parts["$($p.DriveLetter)"] = "$($p.DiskNumber)"
}
$disks = @{}
foreach ($d in (Get-PhysicalDisk)) { $disks["$($d.DeviceId)"] = $d }
$out = foreach ($v in (Get-Volume | Where-Object { $_.DriveLetter })) {
  $letter = "$($v.DriveLetter)"
  $pd = $null
  $dn = $parts[$letter]
  if ($dn -ne $null) { $pd = $disks[$dn] }
  [PSCustomObject]@{
    letter      = $letter
    label       = "$($v.FileSystemLabel)"
    total       = [int64]$v.Size
    free        = [int64]$v.SizeRemaining
    media_type  = if ($pd) { "$($pd.MediaType)" } else { "Unknown" }
    health      = if ($pd) { "$($pd.HealthStatus)" } else { "Unknown" }
    operational = if ($pd) { ($pd.OperationalStatus -join ', ') } else { "" }
  }
}
@($out) | ConvertTo-Json -Compress -Depth 3
"""

# Filesystems worth showing as tier candidates on Linux. fuseblk = ntfs-3g;
# cifs = mounted network shares (valid archive targets, like F: was on Windows).
_LINUX_FSTYPES = {"ext4", "ext3", "xfs", "btrfs", "ntfs3", "fuseblk", "vfat", "exfat", "cifs"}
_LINUX_SKIP_MOUNTS = ("/boot", "/var/lib/docker", "/run", "/dev", "/sys", "/proc", "/snap")


def _normalize_media(raw: str) -> str:
    v = (raw or "").strip()
    if v in ("4", "SSD"):
        return "SSD"
    if v in ("3", "HDD"):
        return "HDD"
    if v in ("5", "SCM"):
        return "SCM"
    return v or "Unknown"


def _normalize_health(health: str, operational: str) -> str:
    """Collapse HealthStatus + OperationalStatus into Healthy/Warning/Unhealthy/Unknown."""
    h = (health or "").strip().lower()
    op = (operational or "").strip().lower()
    if "predictive failure" in op or "predictive failure" in h:
        return "Warning"
    if h == "healthy":
        return "Healthy"
    if h == "warning":
        return "Warning"
    if h in ("unhealthy", "failed"):
        return "Unhealthy"
    return "Unknown"


def _list_drives_powershell() -> list[dict] | None:
    if sys.platform != "win32":
        return None
    try:
        proc = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", _PS_SCRIPT],
            capture_output=True, text=True, timeout=20,
        )
        if proc.returncode != 0 or not proc.stdout.strip():
            return None
        data = json.loads(proc.stdout)
        if isinstance(data, dict):
            data = [data]
        drives = []
        for d in data:
            letter = str(d.get("letter", "")).strip(": ")
            if not letter:
                continue
            drives.append({
                "letter": f"{letter}:",
                "label": d.get("label") or "",
                "total_bytes": int(d.get("total") or 0),
                "free_bytes": int(d.get("free") or 0),
                "media_type": _normalize_media(str(d.get("media_type", ""))),
                "health": _normalize_health(str(d.get("health", "")), str(d.get("operational", ""))),
            })
        return drives
    except Exception:
        logger.exception("PowerShell drive enumeration failed; falling back")
        return None


def _read_proc_mounts() -> list[tuple[str, str, str]]:
    """Return (device, mountpoint, fstype) tuples from /proc/mounts."""
    mounts = []
    try:
        with open("/proc/mounts") as f:
            for line in f:
                parts = line.split()
                if len(parts) < 3:
                    continue
                device, mountpoint, fstype = parts[0], parts[1], parts[2]
                # /proc/mounts octal-escapes spaces etc. (\040)
                mountpoint = mountpoint.encode().decode("unicode_escape")
                mounts.append((device, mountpoint, fstype))
    except OSError:
        logger.exception("Failed to read /proc/mounts")
    return mounts


def _lsblk_devices() -> dict[str, dict]:
    """Map device path -> {label, rota, type, pkname} via one lsblk call."""
    try:
        proc = subprocess.run(
            ["lsblk", "-J", "-b", "-o", "PATH,LABEL,ROTA,TYPE,PKNAME"],
            capture_output=True, text=True, timeout=10,
        )
        if proc.returncode != 0 or not proc.stdout.strip():
            return {}
        devices: dict[str, dict] = {}

        def _walk(nodes: list[dict]) -> None:
            for node in nodes:
                path = node.get("path")
                if path:
                    devices[path] = node
                _walk(node.get("children") or [])

        _walk(json.loads(proc.stdout).get("blockdevices") or [])
        return devices
    except Exception:
        logger.exception("lsblk enumeration failed")
        return {}


def _smartctl_health(disk_path: str) -> str:
    """SMART health via smartctl (direct, then via a NOPASSWD sudoers rule;
    degrades to Unknown)."""
    for cmd in (["smartctl", "-H", "-j", disk_path],
                ["sudo", "-n", "smartctl", "-H", "-j", disk_path]):
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
            if not proc.stdout.strip():
                continue
            data = json.loads(proc.stdout)
            status = data.get("smart_status")
            if isinstance(status, dict) and "passed" in status:
                return "Healthy" if status["passed"] else "Unhealthy"
        except Exception:
            continue
    return "Unknown"


def _list_drives_linux() -> list[dict] | None:
    if sys.platform == "win32":
        return None
    try:
        devices = _lsblk_devices()
        drives = []
        seen_mounts: set[str] = set()
        for device, mountpoint, fstype in _read_proc_mounts():
            if fstype not in _LINUX_FSTYPES:
                continue
            if mountpoint in seen_mounts:
                continue
            if any(mountpoint == s or mountpoint.startswith(s + "/") for s in _LINUX_SKIP_MOUNTS):
                continue
            if mountpoint == "/boot/efi":
                continue
            try:
                usage = shutil.disk_usage(mountpoint)
            except OSError:
                continue
            seen_mounts.add(mountpoint)

            info = devices.get(device, {})
            label = info.get("label") or ""
            if fstype == "cifs":
                media = "Network"
                health = "Unknown"
            elif device.startswith("/dev/loop") or info.get("type") == "loop":
                media = "Unknown"
                health = "Unknown"
            else:
                rota = info.get("rota")
                media = "HDD" if rota in (True, 1, "1") else "SSD" if rota in (False, 0, "0") else "Unknown"
                parent = info.get("pkname")
                disk_path = f"/dev/{parent}" if parent else device
                health = _smartctl_health(disk_path)
            drives.append({
                "letter": mountpoint,
                "label": label,
                "total_bytes": usage.total,
                "free_bytes": usage.free,
                "media_type": media,
                "health": health,
            })
        return drives or None
    except Exception:
        logger.exception("Linux drive enumeration failed; falling back")
        return None


def _list_drives_fallback() -> list[dict]:
    """No SMART/media type — just capacity via shutil."""
    drives = []
    if sys.platform == "win32":
        for letter in string.ascii_uppercase:
            root = f"{letter}:\\"
            if not Path(root).exists():
                continue
            try:
                usage = shutil.disk_usage(root)
            except OSError:
                continue
            drives.append({
                "letter": f"{letter}:",
                "label": "",
                "total_bytes": usage.total,
                "free_bytes": usage.free,
                "media_type": "Unknown",
                "health": "Unknown",
            })
    else:
        try:
            usage = shutil.disk_usage("/")
            drives.append({
                "letter": "/",
                "label": "",
                "total_bytes": usage.total,
                "free_bytes": usage.free,
                "media_type": "Unknown",
                "health": "Unknown",
            })
        except OSError:
            pass
    return drives


def list_drives() -> list[dict]:
    """Return all fixed drives with media type, SMART health, and capacity.

    Cached for _CACHE_TTL seconds. On a failed/empty enumeration, the last good
    result is reused (so the UI keeps real media/health values) and only falls
    back to the capacity-only list if nothing was ever cached.
    """
    global _cache
    with _cache_lock:
        if _cache and (time.monotonic() - _cache["ts"]) < _CACHE_TTL:
            return _cache["drives"]

    drives = _list_drives_powershell() if sys.platform == "win32" else _list_drives_linux()
    if drives:
        with _cache_lock:
            _cache = {"drives": drives, "ts": time.monotonic()}
        return drives

    # Refresh failed — prefer the last good (SMART-aware) result over the
    # capacity-only fallback so health/media don't flicker to "Unknown".
    with _cache_lock:
        if _cache:
            _cache["ts"] = time.monotonic()  # don't hammer a failing call
            return _cache["drives"]
    return _list_drives_fallback()


def mount_root_for_path(path: str) -> str:
    """Return the volume identity containing a path.

    Windows: the drive letter ("E:"). Linux: the mount point ("/mnt/backup"),
    found by walking up until the device (st_dev) changes — handles loop
    images and bind mounts correctly.
    """
    if not path:
        return ""
    try:
        p = Path(path).resolve()
        if sys.platform == "win32":
            return p.drive.rstrip("\\").upper()
        # Walk up to the deepest existing ancestor first (path may not exist yet)
        while not p.exists() and p != p.parent:
            p = p.parent
        dev = p.stat().st_dev
        while p != p.parent and p.parent.stat().st_dev == dev:
            p = p.parent
        return str(p)
    except Exception:
        return ""


def drive_for_path(path: str) -> dict | None:
    """Return the drive descriptor for the volume containing the given path."""
    root = mount_root_for_path(path)
    if not root:
        return None
    for d in list_drives():
        if sys.platform == "win32":
            if d["letter"].upper() == root:
                return d
        elif d["letter"] == root:
            return d
    return None
