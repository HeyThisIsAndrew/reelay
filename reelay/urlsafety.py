"""Which links Reelay will fetch, and which files it will accept.

Every download, listing, details, products and transcript fetch starts only from a link on a platform
Reelay imports from (TikTok, Instagram, YouTube, Snapchat), over http(s), checked BEFORE any network
activity (``fetch_url_problem``). yt-dlp's "generic" extractor would otherwise fetch any address,
including ones on the local network, and ``file://`` must never be reachable. Links one extractor hands
to another are checked the same way (``ytdl.guarded_ydl``). Anything an agent can make Reelay fetch is
narrower still: TikTok, Instagram and YouTube only, with only those platforms' extractors.

Files Reelay posts or serves (``media_file_problem``) must be real videos inside the folders the video
belongs to, whatever the database says.
"""

from __future__ import annotations

import ipaddress
import os
import re
import socket
from pathlib import Path
from urllib.parse import urlsplit

MEDIA_HOSTS = ("tiktok.com", "instagram.com", "youtube.com", "youtu.be")       # what agents may fetch
DOWNLOAD_HOSTS = (*MEDIA_HOSTS, "snapchat.com")                                  # what Reelay downloads from
HOST_NAMES = {"tiktok.com": "TikTok", "instagram.com": "Instagram", "youtube.com": "YouTube",
              "snapchat.com": "Snapchat"}
# The platforms' own "leave this site" redirectors: they bounce to any address, so they're never fetched.
_REDIRECTORS = re.compile(r"^(?:l|lm)\.instagram\.com$|^(?:[\w-]+\.)*youtube\.com/(?:redirect|attribution_link)\b"
                          r"|^(?:[\w-]+\.)*tiktok\.com/link/", re.IGNORECASE)
_BAD_CHARS = re.compile(r"[\s\\\x00-\x1f\x7f]")      # whitespace, backslashes, control characters
# yt-dlp extractor names (regular expressions) that may run for agent-triggered fetches
ALLOWED_EXTRACTORS = [r"tiktok(:.*)?", r"vm\.tiktok", r"instagram(:.*)?", r"youtube(:.*)?"]
IMAGE_TYPES = (".jpg", ".jpeg", ".png", ".webp")


class UnsafeURLError(ValueError):
    """A link Reelay won't fetch (raised before any network activity)."""


def _platform_names(hosts) -> str:
    names = list(dict.fromkeys(HOST_NAMES.get(h, "YouTube" if h == "youtu.be" else h) for h in hosts))
    return ", ".join(names[:-1]) + " and " + names[-1] if len(names) > 1 else names[0]


def fetch_url_problem(url, hosts=DOWNLOAD_HOSTS) -> str | None:
    """Why `url` may not be fetched, or None if it's an http(s) link on one of `hosts` (or a subdomain).

    Anything that can't be parsed is refused, never waved through."""
    if not isinstance(url, str):
        return "That link can't be read."
    url = url.strip()
    if not url:
        return "There's no link to fetch."
    if _BAD_CHARS.search(url):
        return "That link contains spaces, backslashes or control characters."
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError:
        return "That link can't be read."
    if parts.scheme.lower() not in ("http", "https"):
        return "Only http(s) links can be fetched."
    if parts.username is not None or parts.password is not None or "@" in parts.netloc:
        return "Links with a user name or password can't be fetched."
    host = (parts.hostname or "").lower().rstrip(".")
    if not host or not any(host == h or host.endswith("." + h) for h in hosts):
        return f"Only {_platform_names(hosts)} links can be fetched."
    if port not in (None, 80, 443):
        return "Links to other ports can't be fetched."
    if _REDIRECTORS.match(host) or _REDIRECTORS.match(host + (parts.path or "")):
        return "That's a redirect link; paste the link it leads to instead."
    return None


def media_url_problem(url: str) -> str | None:
    """Why `url` may not be fetched for an agent, or None if it's a TikTok, Instagram or YouTube link."""
    return fetch_url_problem(url, MEDIA_HOSTS)


def check_fetch_url(url, hosts=DOWNLOAD_HOSTS) -> str:
    """`url`, stripped, or UnsafeURLError if Reelay won't fetch it."""
    problem = fetch_url_problem(url, hosts)
    if problem:
        raise UnsafeURLError(problem)
    return url.strip()


_LOCAL = "Links to this computer or the local network can't be fetched."


def _public(address: str) -> bool:
    ip = ipaddress.ip_address(address.split("%", 1)[0])           # drop an IPv6 zone ("fe80::1%en0")
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped:
        ip = ip.ipv4_mapped
    return ip.is_global


def asset_url_problem(url, resolve: bool = True) -> str | None:
    """For files a platform points at (cover images, caption tracks), which live on CDNs with many
    names: http(s) only, no user name, and never an address on this computer or the local network.

    The host is read the way the system will read it: numeric forms like ``127.1``, ``2130706433`` or
    ``0x7f000001`` count as the addresses they stand for, and (with ``resolve``) a name is looked up and
    every address it points to must be public. A name that can't be looked up is refused.
    (Not covered: a name that changes what it points to between this check and the download.)"""
    if not isinstance(url, str) or not url.strip() or _BAD_CHARS.search(url.strip()):
        return "That link can't be read."
    try:
        parts = urlsplit(url.strip())
        port = parts.port
    except ValueError:
        return "That link can't be read."
    if parts.scheme.lower() not in ("http", "https"):
        return "Only http(s) links can be fetched."
    if parts.username is not None or "@" in parts.netloc:
        return "Links with a user name or password can't be fetched."
    host = (parts.hostname or "").lower().rstrip(".")
    if not host or host == "localhost" or host.endswith((".localhost", ".local", ".internal", ".home.arpa")):
        return _LOCAL
    port = port or (443 if parts.scheme.lower() == "https" else 80)
    try:      # a number in any form the system accepts (no lookup)
        numeric = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM, flags=socket.AI_NUMERICHOST)
    except (socket.gaierror, UnicodeError, ValueError):
        numeric = None
    if numeric is not None:
        return None if all(_public(info[4][0]) for info in numeric) else _LOCAL
    if not resolve:
        return None
    try:
        found = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except (socket.gaierror, UnicodeError, ValueError, OSError):
        return "That link's address can't be found."
    if not found or not all(_public(info[4][0]) for info in found):
        return _LOCAL
    return None


# The extractors Reelay's own downloads use. yt-dlp's "generic" extractor fetches whatever a link points
# at (and follows redirects to anywhere), so it's only added for link formats that need it and come from
# the platform itself: Instagram share links, TikTok photo posts, and Snapchat links other than Spotlight.
# Redirector links on the platforms (l.instagram.com/?u=…, youtube.com/redirect?q=…) then match no
# extractor and are refused before any network activity.
APP_EXTRACTORS = [r"tiktok(:.*)?", r"vm\.tiktok", r"instagram(:.*)?", r"youtube.*", r"snapchatspotlight"]
_NEEDS_GENERIC = re.compile(r"^https?://(?:www\.|m\.)?(?:instagram\.com/share/|tiktok\.com/@[^/?#]+/photo/"
                            r"|snapchat\.com/(?!spotlight/))", re.IGNORECASE)


def app_ydl(opts: dict, url: str) -> dict:
    """yt-dlp options for one of Reelay's own fetches of `url`: only the platform extractors (plus the
    generic one for the few link formats that need it), never file://. Options that already name their
    extractors (an agent's fetch, see ``restrict_ydl``) are never widened."""
    if "allowed_extractors" in opts:
        return {**opts, "enable_file_urls": False}
    generic = ["generic"] if isinstance(url, str) and _NEEDS_GENERIC.match(url.strip()) else []
    return {**opts, "allowed_extractors": APP_EXTRACTORS + generic, "enable_file_urls": False}


def restrict_ydl(opts: dict) -> dict:
    """yt-dlp options for an agent-triggered fetch: only the platform extractors, never file://."""
    return {**opts, "allowed_extractors": list(ALLOWED_EXTRACTORS), "enable_file_urls": False}


# ---------------------------------------------------------------------- files Reelay posts or serves
VIDEO_TYPES = (".mp4", ".m4v", ".mov", ".webm", ".mkv")
_MP4_BOXES = (b"ftyp", b"moov", b"mdat", b"free", b"wide", b"skip")
_EBML = b"\x1a\x45\xdf\xa3"           # WebM / Matroska


def looks_like_video(path: Path | str) -> bool:
    """The file starts like an MP4 / QuickTime or WebM / Matroska video (not just named like one)."""
    try:
        with open(path, "rb") as f:
            head = f.read(12)
    except OSError:
        return False
    return head[4:8] in _MP4_BOXES or head[:4] == _EBML


def media_file_problem(path, roots) -> str | None:
    """Why `path` may not be posted or served, or None. It must exist, be a regular file once links and
    ``..`` are resolved, sit inside one of `roots` (the folders this video belongs to), be named like a
    video and start like one."""
    if not path:
        return "There's no video file."
    try:
        real = Path(path).expanduser().resolve(strict=True)
    except (OSError, RuntimeError):
        return "The video file is missing."
    if not real.is_file():
        return "That isn't a video file."
    allowed = []
    for root in roots:
        if not root:
            continue
        try:
            allowed.append(Path(root).expanduser().resolve(strict=False))
        except (OSError, RuntimeError):
            continue
    if not any(r in real.parents for r in allowed):
        return "The video file isn't inside this profile's video folder."
    if real.suffix.lower() not in VIDEO_TYPES:
        return "That isn't a video file."
    if not looks_like_video(real):
        return "That file isn't a video."
    return None


def file_identity(path: Path | str) -> tuple[int, int] | None:
    """(device, inode) of the file a path leads to, to notice it being swapped for another."""
    try:
        st = os.stat(path)
    except OSError:
        return None
    return st.st_dev, st.st_ino


def image_path_problem(path: str, library: Path | str) -> str | None:
    """A cover image path set by an agent must be an image inside the profile's library folder."""
    if not path:
        return None
    p = Path(path).expanduser()
    if p.suffix.lower() not in IMAGE_TYPES:
        return "The cover must be a .jpg, .png or .webp image."
    try:
        resolved, root = p.resolve(strict=False), Path(library).expanduser().resolve(strict=False)
    except (OSError, RuntimeError):
        return "That cover path can't be used."
    if root != resolved and root not in resolved.parents:
        return "The cover image must be inside this profile's video folder."
    return None


def inside(path: Path | str, folder: Path | str) -> bool:
    """True if `path` (after resolving links and ..) is inside `folder`."""
    try:
        p, root = Path(path).resolve(strict=False), Path(folder).resolve(strict=False)
    except (OSError, RuntimeError):
        return False
    return root in p.parents
