"""RTSP URL credential normalisation.

go2rtc is written in Go and parses stream URLs with `net/url`, which rejects
any character outside RFC 3986's userinfo set — most importantly `@`, but also
`^`, `[`, `]`, `<`, `>`, `"`, backslash and space. Camera passwords routinely
contain those. ffmpeg is lenient (it splits on the *last* `@` and accepts the
rest verbatim), so a raw password produces the confusing half-broken state
where recording works but live view, the frame broker, motion detection and
thumbnails all fail with:

    streams: parse "rtsp://user:p@ss@host/path": net/url: invalid userinfo

Percent-encoding the userinfo satisfies Go and is transparent to ffmpeg, which
decodes the escapes before authenticating. Normalising on the way into the DB
means every consumer reads a URL both stacks accept.
"""

import re
from urllib.parse import quote

# RFC 3986 userinfo = unreserved / pct-encoded / sub-delims / ":"
# `%` is kept so existing escapes survive (see `_encode_userinfo`).
_USERINFO_SAFE = frozenset(
    "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
    "abcdefghijklmnopqrstuvwxyz"
    "0123456789"
    "-._~"       # unreserved
    "!$&'()*+,;="  # sub-delims
    ":"          # user/password separator
)

_HEX = frozenset("0123456789abcdefABCDEF")

# scheme://authority[rest]. The authority ends at the first "/", "?" or "#".
_URL_RE = re.compile(r"^(?P<scheme>[A-Za-z][A-Za-z0-9+.\-]*://)(?P<authority>[^/?#]*)(?P<rest>.*)$")


def _encode_userinfo(userinfo: str) -> str:
    """Percent-encode everything Go rejects, leaving existing `%XX` intact.

    Skipping well-formed escapes is what makes this idempotent: re-saving a
    camera must not turn `%40` into `%2540`.
    """
    out: list[str] = []
    i = 0
    n = len(userinfo)
    while i < n:
        ch = userinfo[i]
        if (
            ch == "%"
            and i + 2 < n
            and userinfo[i + 1] in _HEX
            and userinfo[i + 2] in _HEX
        ):
            out.append(userinfo[i : i + 3])
            i += 3
            continue
        if ch in _USERINFO_SAFE:
            out.append(ch)
        else:
            out.extend(f"%{b:02X}" for b in ch.encode("utf-8"))
        i += 1
    return "".join(out)


def normalize_rtsp_url(url: str | None) -> str | None:
    """Return `url` with its credentials percent-encoded for go2rtc.

    Returns the input unchanged when there is nothing to fix (no credentials,
    or not a `scheme://` URL). Safe to call repeatedly on the same value.
    """
    if not url:
        return url
    m = _URL_RE.match(url.strip())
    if not m:
        return url
    authority = m.group("authority")
    if "@" not in authority:
        return url
    # Split on the LAST "@" — an unencoded "@" inside the password would
    # otherwise steal the host, which is exactly the case being repaired.
    userinfo, _, hostport = authority.rpartition("@")
    return f"{m.group('scheme')}{_encode_userinfo(userinfo)}@{hostport}{m.group('rest')}"


def build_rtsp_credentials(username: str, password: str) -> str:
    """Build the `user:pass@` prefix for a discovered stream URL, encoded.

    These come from plaintext form fields rather than a URL, so `%` is escaped
    too — a password containing a literal `%` must not be mistaken for the
    start of an escape. `normalize_rtsp_url` then leaves the result alone.
    """
    if not username:
        return ""
    user = quote(username, safe="")
    if not password:
        return f"{user}@"
    return f"{user}:{quote(password, safe='')}@"
