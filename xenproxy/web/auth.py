"""Access control for the web front end.

The tool runs as root on a hypervisor and can start and delete virtual
machines, so the server is not something to leave open. Three things hold
it shut:

  * it binds to the loopback address unless told otherwise,
  * every request carries a token that is generated once and kept at
    0600 beside the rest of the configuration,
  * and the API accepts that token only in the Authorization header,
    never from the cookie that lets the page itself load. A cookie is
    sent by the browser on any request another site provokes; a header
    is not, so there is nothing for a hostile page to trigger.
"""
from __future__ import annotations

import hmac
import os
import secrets
import stat

from .. import config

TOKEN_FILE = os.path.join(config.CONF_DIR, "web-token")


def token(create: bool = True) -> str:
    """The shared secret, generated on first use."""
    try:
        st = os.lstat(TOKEN_FILE)
        if stat.S_ISREG(st.st_mode):
            with open(TOKEN_FILE, encoding="utf-8") as fh:
                value = fh.read().strip()
            if value:
                return value
    except OSError:
        pass
    if not create:
        return ""
    os.makedirs(config.CONF_DIR, mode=0o700, exist_ok=True)
    value = secrets.token_urlsafe(32)
    fd = os.open(TOKEN_FILE, os.O_WRONLY | os.O_CREAT | os.O_TRUNC
                 | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "w") as fh:
        fh.write(value + "\n")
    return value


def reset() -> str:
    try:
        os.unlink(TOKEN_FILE)
    except OSError:
        pass
    return token()


def matches(offered: str) -> bool:
    want = token(create=False)
    if not want or not offered:
        return False
    return hmac.compare_digest(offered, want)
