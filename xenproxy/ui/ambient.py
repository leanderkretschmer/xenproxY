"""The quiet field in the margins.

Past a certain width the interface stops growing and is centred, which
leaves a column of nothing down each side. This fills it with a slow noise
pattern - sparse marks that drift and swell, dim enough to read as texture
rather than content.

It has to cost nothing. The pattern loops over a fixed number of phases,
and each phase's margins are built once and then handed out, so animating
is a dictionary lookup rather than arithmetic over ten thousand cells.
"""
from __future__ import annotations

import math
import threading

from ..theme import RESET, Theme

PHASES = 48            # one full breath, then it repeats
RAMP_UNI = " ·•●"
RAMP_ASCII = " .oO"
# how much of the field shows at all; most cells stay empty on purpose
FLOOR = 0.46

_cache: dict = {}


def _value(x: int, y: int, phase: int) -> float:
    """Smooth blobs that drift and breathe, in 0..1.

    Three gratings at different angles and speeds beat against each other,
    which gives something that never quite repeats inside one loop, and a
    slow swell on top opens and closes the whole field.
    """
    t = phase * (2 * math.pi / PHASES)
    v = (math.sin(x * 0.21 + t * 1.3)
         * math.sin(y * 0.37 - t * 0.9)
         + 0.7 * math.sin((x + y * 0.6) * 0.13 + t * 2.1)
         + 0.4 * math.sin((x * 0.6 - y) * 0.09 - t * 1.7))
    v = (v / 2.1 + 1) / 2
    swell = 0.72 + 0.28 * math.sin(t)
    return max(0.0, min(1.0, v * swell))


def _shade(th: Theme, level: int, levels: int) -> str:
    """Dim marks sit between surface and border; the brightest reaches
    the muted text colour, so a swell is seen as light and not only as
    more dots."""
    if th.depth == 0:
        return ""
    from ..theme import _hex_rgb, _rgb_to_256
    a = _hex_rgb(th.tokens.get("surface", "#1f2335"))
    b = _hex_rgb(th.tokens.get("border", "#3b4261"))
    c = _hex_rgb(th.tokens.get("muted", "#565f89"))
    f = (level + 1) / levels
    if f < 0.999:
        col = tuple(int(a[i] + (b[i] - a[i]) * f) for i in range(3))
    else:
        col = tuple((b[i] + c[i]) // 2 for i in range(3))
    if th.depth >= 24:
        return "\x1b[38;2;%d;%d;%dm" % col
    return f"\x1b[38;5;{_rgb_to_256(*col)}m"


def _strip(th: Theme, x0: int, width: int, y: int, phase: int) -> str:
    """One horizontal run of the field, as a coloured string."""
    if width <= 0:
        return ""
    ramp = RAMP_UNI if th.uni else RAMP_ASCII
    out = []
    last = None
    for i in range(width):
        v = _value(x0 + i, y, phase)
        if v < FLOOR:
            out.append(" ")
            last = None
            continue
        step = min(len(ramp) - 1,
                   1 + int((v - FLOOR) / (1 - FLOOR) * (len(ramp) - 1)))
        shade = _shade(th, step - 1, len(ramp) - 1)
        if shade != last:
            out.append(shade)
            last = shade
        out.append(ramp[step])
    return "".join(out) + (RESET if last else "")


_warming: set = set()


def warm(th: Theme, cols: int, rows: int, left_w: int, view: int) -> None:
    """Build the whole loop in the background.

    One phase is twenty-odd milliseconds of trigonometry, which is a
    visible hitch the first time each of them comes round. Doing them all
    on a spare thread means the first breath is as smooth as the rest.
    """
    sig = (th.name, cols, rows, left_w, view)
    if sig in _warming:
        return
    _warming.add(sig)

    def build():
        for p in range(PHASES):
            margins(th, cols, rows, left_w, view, p)
    threading.Thread(target=build, daemon=True).start()


def margins(th: Theme, cols: int, rows: int, left_w: int, view: int,
            phase: int) -> tuple[list[str], list[str]]:
    """(left, right) columns of field, one string per row."""
    right_x = left_w + view
    right_w = max(0, cols - right_x)
    key = (th.name, th.depth, th.uni, cols, rows, left_w, view,
           phase % PHASES)
    hit = _cache.get(key)
    if hit is not None:
        return hit
    if len(_cache) > 2 * PHASES:
        _cache.clear()
        _warming.clear()
    p = phase % PHASES
    left = [_strip(th, 0, left_w, y, p) for y in range(rows)]
    right = [_strip(th, right_x, right_w, y, p) for y in range(rows)]
    _cache[key] = (left, right)
    return left, right
