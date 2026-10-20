"""Update check against GitHub releases."""
from __future__ import annotations

import json
import re
import ssl
import urllib.error
import urllib.request

from . import __repo__, __version__

API = "https://api.github.com/repos/{repo}/releases/latest"
TAGS = "https://api.github.com/repos/{repo}/tags"
INSTALL_URL = ("https://raw.githubusercontent.com/{repo}/main/install.sh")


def parse_version(text: str) -> tuple:
    nums = re.findall(r"\d+", text or "")
    return tuple(int(n) for n in nums[:4]) or (0,)


def is_newer(remote: str, local: str = __version__) -> bool:
    return parse_version(remote) > parse_version(local)


def _get_json(url: str, timeout: int = 10):
    req = urllib.request.Request(url, headers={
        "Accept": "application/vnd.github+json",
        "User-Agent": f"xenproxY/{__version__}",
    })
    ctx = ssl.create_default_context()
    with urllib.request.urlopen(req, timeout=timeout, context=ctx) as resp:
        return json.loads(resp.read().decode("utf-8"))


def latest(repo: str = __repo__, timeout: int = 10) -> dict | None:
    """Newest release, falling back to the newest tag."""
    try:
        data = _get_json(API.format(repo=repo), timeout)
        tag = data.get("tag_name") or ""
        if tag:
            return {"version": tag.lstrip("v"), "url": data.get("html_url", ""),
                    "notes": (data.get("body") or "").strip()[:800],
                    "published": data.get("published_at", "")}
    except (urllib.error.HTTPError, urllib.error.URLError, OSError, ValueError):
        pass
    try:
        tags = _get_json(TAGS.format(repo=repo), timeout)
        if tags:
            best = max((t.get("name", "") for t in tags), key=parse_version)
            return {"version": best.lstrip("v"),
                    "url": f"https://github.com/{repo}/releases",
                    "notes": "", "published": ""}
    except (urllib.error.HTTPError, urllib.error.URLError, OSError, ValueError):
        pass
    return None


def check(repo: str = __repo__) -> dict | None:
    """Return release info when the remote version is newer."""
    info = latest(repo)
    if info and is_newer(info["version"]):
        return info
    return None


def install_command(repo: str = __repo__) -> str:
    return f"curl -fsSL {INSTALL_URL.format(repo=repo)} | bash"
