"""Neutralising text that came from somewhere else."""
from __future__ import annotations

import re

_CTRL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f]")


def scrub(value) -> str:
    """Strip control characters from a string bound for the terminal.

    Names, error texts and task results all originate on the Xen host. A
    guest called "web\\x1b[2J" would otherwise clear the screen when it is
    listed, and worse sequences can drive the emulator. Tabs and newlines
    survive because log lines legitimately contain them.
    """
    return _CTRL.sub("?", str(value))
