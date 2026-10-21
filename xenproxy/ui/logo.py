"""Brand marks, drawn at half height.

The source files are shaded block art, one cell per character. Printing
them as they are would cost twenty-two rows and look squashed, because a
terminal cell is about twice as tall as it is wide. Each pair of rows is
therefore folded into one cell of U+2580 (upper half block): the glyph
paints the top half in the foreground colour and the bottom half in the
background colour, so one row carries two, and the proportions come out
right.
"""
from __future__ import annotations

import os

from ..theme import RESET, Theme

ART_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                       "art")

# the shading characters, darkest first; index doubles as the intensity
SHADES = " ░▒▓█"
UPPER = "▀"
LOWER = "▄"
FULL = "█"
# what each intensity becomes without box-drawing support
ASCII_SHADES = " .:+#"

# How bright a cell of each intensity is painted, as a fraction of the
# token colour. Keeping 4 at 1.0 means the solid parts of the mark are the
# token itself and the shading reads as depth around it.
LEVELS = (0.0, 0.34, 0.55, 0.78, 1.0)

NAMES = {"xen": "xcp-ng", "incus": "incus", "proxmox": "proxmox"}

_cache: dict = {}
_rendered: dict = {}


def _rows(name: str) -> list[str]:
    if name not in _cache:
        path = os.path.join(ART_DIR, f"{name}.txt")
        try:
            with open(path, encoding="utf-8") as fh:
                rows = [l.rstrip("\n") for l in fh]
        except OSError:
            rows = []
        while rows and not rows[-1].strip():
            rows.pop()
        while rows and not rows[0].strip():
            rows.pop(0)
        width = max((len(r) for r in rows), default=0)
        _cache[name] = [r.ljust(width) for r in rows]
    return _cache[name]


def size(name: str) -> tuple[int, int]:
    """(width, height) of the half-height rendering."""
    rows = _rows(NAMES.get(name, name))
    if not rows:
        return 0, 0
    return len(rows[0]), (len(rows) + 1) // 2


def _intensity(ch: str) -> int:
    i = SHADES.find(ch)
    return i if i >= 0 else 0


def _shade(th: Theme, token: str, level: int) -> str:
    """The foreground escape for one intensity of the token colour."""
    if level <= 0 or th.depth == 0:
        return ""
    if th.depth < 24:
        # not enough colours to shade convincingly; the mark stays flat
        return th.fg(token)
    from ..theme import _hex_rgb
    r, g, b = _hex_rgb(th.tokens.get(token, token))
    f = LEVELS[min(level, len(LEVELS) - 1)]
    return f"\x1b[38;2;{int(r * f)};{int(g * f)};{int(b * f)}m"


def _shade_bg(th: Theme, token: str, level: int) -> str:
    if level <= 0 or th.depth == 0:
        return ""
    if th.depth < 24:
        return th.bg(token)
    from ..theme import _hex_rgb
    r, g, b = _hex_rgb(th.tokens.get(token, token))
    f = LEVELS[min(level, len(LEVELS) - 1)]
    return f"\x1b[48;2;{int(r * f)};{int(g * f)};{int(b * f)}m"


def render(name: str, th: Theme, token: str = "primary", *,
           width: int = 0) -> list[str]:
    """The mark as terminal rows, centred in `width` when one is given.

    Four hundred cells of escape sequences is not much, but it is the same
    four hundred on every frame, so the finished rows are kept.
    """
    key = (name, token, th.name, th.depth, th.uni, width)
    hit = _rendered.get(key)
    if hit is not None:
        return hit
    out = _render(name, th, token, width=width)
    if len(_rendered) > 64:         # a theme switch makes new entries
        _rendered.clear()
    _rendered[key] = out
    return out


def _render(name: str, th: Theme, token: str = "primary", *,
            width: int = 0) -> list[str]:
    rows = _rows(NAMES.get(name, name))
    if not rows:
        return []
    if not th.uni or th.depth == 0:
        # Without block glyphs there is nothing to fold rows into, and
        # without colour the shading would collapse into one silhouette.
        # Both cases keep the shading as characters instead.
        out = ["".join(ASCII_SHADES[_intensity(c)] for c in row)
               for row in rows[::2]]
        return [_centre(th.paint(r, token), len(rows[0]), width) for r in out]

    out: list[str] = []
    for i in range(0, len(rows), 2):
        top = rows[i]
        bottom = rows[i + 1] if i + 1 < len(rows) else " " * len(top)
        line = []
        for x in range(len(top)):
            t = _intensity(top[x])
            b = _intensity(bottom[x] if x < len(bottom) else " ")
            if not t and not b:
                line.append(" ")
            elif t and not b:
                line.append(_shade(th, token, t) + UPPER + RESET)
            elif b and not t:
                line.append(_shade(th, token, b) + LOWER + RESET)
            elif t == b:
                line.append(_shade(th, token, t) + FULL + RESET)
            else:
                line.append(_shade(th, token, t) + _shade_bg(th, token, b)
                            + UPPER + RESET)
        out.append("".join(line))
    return [_centre(r, len(rows[0]), width) for r in out]


def _centre(line: str, natural: int, width: int) -> str:
    if width <= natural:
        return line
    return " " * ((width - natural) // 2) + line
