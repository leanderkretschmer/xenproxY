"""Update check against GitHub releases.

The check reports *why* it found nothing, because the three reasons want
three different answers: already current, nothing published yet, or GitHub
out of reach. A check that quietly returns nothing looks exactly like a
check that never ran.
"""
from __future__ import annotations

import json
import re
import ssl
import urllib.error
import urllib.request

from . import __repo__, __version__

API = "https://api.github.com/repos/{repo}/releases/latest"
TAGS = "https://api.github.com/repos/{repo}/tags"
INSTALL_URL = "https://raw.githubusercontent.com/{repo}/main/install.sh"

NEWER = "newer"            # a newer version is published
CURRENT = "current"        # the published version is the one we run
NONE = "none"              # GitHub answered, but nothing is published
UNREACHABLE = "unreachable"


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


def _rate_limited(exc: urllib.error.HTTPError) -> bool:
    try:
        return (exc.code in (403, 429)
                and exc.headers.get("X-RateLimit-Remaining") == "0")
    except Exception:  # noqa: BLE001 - diagnosis must never raise
        return False


def _from_release(data, repo: str) -> dict | None:
    tag = (data or {}).get("tag_name") or ""
    if not tag:
        return None
    return {"version": tag.lstrip("v"), "url": data.get("html_url", ""),
            "notes": (data.get("body") or "").strip()[:800],
            "published": data.get("published_at", "")}


def _from_tags(data, repo: str) -> dict | None:
    names = [t.get("name", "") for t in (data or []) if isinstance(t, dict)]
    names = [n for n in names if n]
    if not names:
        return None
    best = max(names, key=parse_version)
    return {"version": best.lstrip("v"),
            "url": f"https://github.com/{repo}/releases/tag/{best}",
            "notes": "", "published": ""}


def status(repo: str = __repo__, timeout: int = 10) -> dict:
    """What the check found, with the reason attached.

    A 404 from `releases/latest` means GitHub answered and the repository
    simply has no release - that is a different thing from not getting an
    answer at all, and only the second one is a failure. `tags` tells the
    two apart: an existing repository without tags answers 200 with an
    empty list, one that is not there answers 404 as well.
    """
    served = False          # at least one endpoint returned a document
    info = None
    error = ""
    for url, parse in ((API.format(repo=repo), _from_release),
                       (TAGS.format(repo=repo), _from_tags)):
        try:
            data = _get_json(url, timeout)
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                pass
            elif _rate_limited(exc):
                # the unauthenticated API allows 60 requests an hour per
                # address, and a node behind a busy NAT runs into that
                error = ("GitHub rate limit reached - try again later, or "
                         "reinstall from main")
            else:
                error = f"GitHub answered {exc.code}"
            continue
        except (urllib.error.URLError, OSError, ValueError) as exc:
            error = str(getattr(exc, "reason", None) or exc)
            continue
        served = True
        info = parse(data, repo)
        if info:
            break

    if info is None:
        if served:
            return {"state": NONE, "error": "", "info": None, "repo": repo}
        return {"state": UNREACHABLE,
                "error": error or f"GitHub has no answer for {repo}",
                "info": None, "repo": repo}
    return {"state": NEWER if is_newer(info["version"]) else CURRENT,
            "error": "", "info": info}


def describe(st: dict) -> tuple[str, str]:
    """A sentence for the operator, plus the token to paint it with."""
    state = (st or {}).get("state", UNREACHABLE)
    info = (st or {}).get("info") or {}
    if state == NEWER:
        return f"version {info.get('version')} is available", "warning"
    if state == CURRENT:
        return (f"xenproxY {__version__} is the newest published version",
                "success")
    if state == NONE:
        return (f"{(st or {}).get('repo', 'the repository')} publishes no "
                "release yet - 'Reinstall' pulls the current main branch",
                "info")
    err = (st or {}).get("error") or "no answer"
    return f"could not reach GitHub: {err}", "danger"


def check(repo: str = __repo__) -> dict | None:
    """Release info when the remote version is newer, else None."""
    st = status(repo)
    return st["info"] if st["state"] == NEWER else None


def latest(repo: str = __repo__, timeout: int = 10) -> dict | None:
    return status(repo, timeout)["info"]


def install_command(repo: str = __repo__) -> str:
    return f"curl -fsSL {INSTALL_URL.format(repo=repo)} | bash"
