"""Brand marks in the terminal's own colours.

A mark is made of cells, not of glyphs: every cell is a background colour
under a space. No font can shift it, stripe it or leave a seam between its
rows, because no font is asked to draw anything. `tools/img2art.py`
samples the real logos onto that grid - one character per cell, a base-36
palette index or `.` for nothing - and the files it writes are all this
module reads.

Nothing here needs an image library at runtime, and nothing is rendered
twice: the finished rows are memoised, and a row costs one escape per run
of colour rather than one per cell.
"""
from __future__ import annotations

import os

from ..theme import RESET, Theme, _hex_rgb, _rgb_to_256
from .term import pad, vlen

ART_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                       "art")

ASCII_SHADES = " .:-=+*#%@"
DIGITS = "0123456789abcdefghijklmnopqrstuvwxyz"

# widest first - the caller takes the first one that fits
VARIANTS = (48, 36, 24, 16, 12)
NAMES = {"xen": "xcp-ng", "xcp-ng": "xcp-ng", "incus": "incus",
         "proxmox": "proxmox", "xenproxy": "xenproxy"}

# A logo's black sits on a dark terminal and disappears. Anything below
# this luminance is lifted until it can be seen; a colour with no hue left
# to keep becomes a neutral grey, because tinting it with a theme accent
# would put a colour in the mark that the mark does not have.
FLOOR = 42
HUELESS = 26
NEUTRAL = (168, 170, 178)

_art: dict = {}
_rows: dict = {}


def _load(name: str, width: int):
    """(palette, grid) for one variant, or None when it is not shipped."""
    key = (name, width)
    if key not in _art:
        path = os.path.join(ART_DIR, f"{name}-{width}.art")
        try:
            with open(path, encoding="utf-8") as fh:
                lines = [l.rstrip("\n") for l in fh if l.strip()]
        except OSError:
            _art[key] = None
            return None
        palette: list[tuple] = []
        grid: list[str] = []
        for line in lines:
            if line.startswith("#"):
                continue
            if line.startswith("palette "):
                palette = [_hex_rgb(h) for h in line.split()[1:]]
                continue
            grid.append(line)
        _art[key] = (palette, grid) if palette and grid else None
    return _art[key]


def size(name: str, width: int) -> tuple[int, int]:
    """(columns, rows) of one shipped variant, or (0, 0)."""
    art = _load(NAMES.get(name, name), width)
    if not art:
        return 0, 0
    _palette, grid = art
    return max(len(r) for r in grid), len(grid)


def best(name: str, max_width: int, max_height: int) -> int:
    """The widest shipped variant that fits, or 0 when none does."""
    for w in VARIANTS:
        gw, gh = size(name, w)
        if gw and gw <= max_width and gh <= max_height:
            return w
    return 0


def _lift(rgb: tuple) -> tuple:
    r, g, b = rgb
    lum = 0.2126 * r + 0.7152 * g + 0.0722 * b
    if lum >= FLOOR:
        return rgb
    if max(rgb) < HUELESS:
        return NEUTRAL
    f = FLOOR / max(lum, 1.0)
    return (min(255, int(r * f)), min(255, int(g * f)), min(255, int(b * f)))


def _bg(rgb: tuple, depth: int) -> str:
    if depth >= 24:
        return "\x1b[48;2;%d;%d;%dm" % rgb
    return f"\x1b[48;5;{_rgb_to_256(*rgb)}m"


def _shade(rgb: tuple) -> str:
    """Luminance as a character, for a terminal with no colour at all."""
    lum = (0.2126 * rgb[0] + 0.7152 * rgb[1] + 0.0722 * rgb[2]) / 255.0
    return ASCII_SHADES[min(len(ASCII_SHADES) - 1,
                            int(lum * len(ASCII_SHADES)))]


def _line(cells: list, depth: int) -> str:
    """One row of cells as one string.

    Neighbouring cells of the same colour share an escape: a 48-cell row
    of a two-ink mark is a handful of runs, not 48 of them, and the frame
    that carries it stays cheap to write.
    """
    if depth == 0:
        return "".join(" " if c is None else _shade(c) for c in cells)
    out = []
    run = None
    n = 0
    for c in cells + [()]:          # the sentinel flushes the last run
        if c == run:
            n += 1
            continue
        if n:
            out.append(" " * n if run is None
                       else _bg(run, depth) + " " * n + RESET)
        run, n = c, 1
    return "".join(out)


def _index(ch: str) -> int:
    return -1 if ch == "." else DIGITS.find(ch)


def render(name: str, th: Theme, width: int, *, centre: int = 0) -> list[str]:
    key = (NAMES.get(name, name), width, th.depth, centre)
    hit = _rows.get(key)
    if hit is not None:
        return hit
    out = _render(*key)
    if len(_rows) > 64:
        _rows.clear()
    _rows[key] = out
    return out


def _render(name: str, width: int, depth: int, centre: int) -> list[str]:
    art = _load(name, width)
    if not art:
        return []
    palette, grid = art
    cols = max(len(r) for r in grid)
    colours = [_lift(c) for c in palette]
    lead = " " * max(0, (centre - cols) // 2)
    out = []
    for row in grid:
        cells = []
        for ch in row.ljust(cols, "."):
            i = _index(ch)
            cells.append(colours[i] if 0 <= i < len(colours) else None)
        out.append(lead + _line(cells, depth))
    return out


# -- the name --------------------------------------------------------
# The same drawing at every size, so narrowing the terminal shrinks the
# wordmark instead of swapping it for a different one. Shipped as coverage
# only - the colour is decided here, from the theme. Nothing below seven
# rows: the letters stop being letters, and the header falls back to text.
WORDMARK_WIDTHS = (140, 104, 80)


def wordmark_size(width: int, height: int) -> tuple[int, int]:
    """(columns, rows) of the largest wordmark that fits, or (0, 0)."""
    for w in WORDMARK_WIDTHS:
        gw, gh = size("wordmark", w)
        if gw and gw <= width and gh <= height:
            return gw, gh
    return 0, 0


def wordmark(th: Theme, width: int, height: int, *,
             centre: bool = True) -> list[str]:
    """The name, painted as a gradient from primary to accent.

    A flat colour over a wide face looks like a wall; the gradient gives it
    a direction without introducing a colour the theme does not have.
    """
    key = ("word", th.name, th.depth, width, height, centre)
    hit = _rows.get(key)
    if hit is not None:
        return hit
    out = _wordmark(th, width, height, centre)
    if len(_rows) > 64:
        _rows.clear()
    _rows[key] = out
    return out


def _wordmark(th: Theme, width: int, height: int, centre: bool) -> list[str]:
    chosen = 0
    for w in WORDMARK_WIDTHS:
        gw, gh = size("wordmark", w)
        if gw and gw <= width and gh <= height:
            chosen = w
            break
    if not chosen:
        return []
    art = _load("wordmark", chosen)
    if not art:
        return []
    _palette, grid = art
    cols = max(len(r) for r in grid)
    a = _hex_rgb(th.tokens.get("primary", "#7aa2f7"))
    b = _hex_rgb(th.tokens.get("accent", "#bb9af7"))
    steps = [tuple(int(a[i] + (b[i] - a[i]) * (x / max(1, cols - 1)))
                   for i in range(3)) for x in range(cols)]
    lead = " " * max(0, (width - cols) // 2) if centre else ""
    out = []
    for row in grid:
        cells = [None if ch == "." else steps[x]
                 for x, ch in enumerate(row.ljust(cols, "."))]
        out.append(lead + _line(cells, th.depth))
    return out


# -- the two together -------------------------------------------------
MARK_GAP = 3


def banner(th: Theme, width: int, height: int) -> list[str]:
    """The mark beside the name, as one centred block.

    The name decides the size - it is the part that has to be read - and
    the mark takes the largest variant that is no taller, so the pair
    keeps its proportions whatever the terminal gives them.
    """
    key = ("banner", th.name, th.depth, width, height)
    hit = _rows.get(key)
    if hit is not None:
        return hit
    out = _banner(th, width, height)
    if len(_rows) > 64:
        _rows.clear()
    _rows[key] = out
    return out


def _banner(th: Theme, width: int, height: int) -> list[str]:
    # Try the pair at each size before giving up on the mark: a step
    # smaller with both of them beats the name on its own.
    pick = None
    fallback = 0
    for w in WORDMARK_WIDTHS:
        ww, wh = size("wordmark", w)
        if not ww or ww > width or wh > height:
            continue
        fallback = fallback or ww
        for v in VARIANTS:
            mw, mh = size("xenproxy", v)
            if mw and mh <= wh and mw + MARK_GAP + ww <= width:
                pick = (ww, mw, v)
                break
        if pick:
            break
    if not pick and not fallback:
        return []
    if not pick:
        word = wordmark(th, fallback, height, centre=False)
        return [pad("", max(0, (width - fallback) // 2)) + l for l in word]

    word_cols, mw, variant = pick
    # ask for exactly that size, or the largest one would come back again
    word = wordmark(th, word_cols, height, centre=False)
    if not word:
        return []
    ww, wh = vlen(word[0]), len(word)
    mark = render("xenproxy", th, variant)

    # sit the shorter of the two in the middle of the taller
    top = (wh - len(mark)) // 2
    rows = []
    left = pad("", max(0, (width - (mw + MARK_GAP + ww)) // 2))
    for i in range(wh):
        m = mark[i - top] if 0 <= i - top < len(mark) else ""
        rows.append(left + pad(m, mw) + " " * MARK_GAP + word[i])
    return rows
