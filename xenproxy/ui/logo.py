"""Brand marks in the terminal's own colours.

The art files are produced from the real logos by `tools/img2art.py`: a
palette of hex colours, then one line per pixel row, one character per
pixel. U+2580 splits a cell into an upper and a lower half, so two stacked
pixels fit in one cell and come out square - a cell is about twice as tall
as it is wide.

Nothing here needs an image library at runtime, and nothing is read twice:
the finished rows are the same on every frame.
"""
from __future__ import annotations

import os

from ..theme import BOLD, RESET, Theme, _hex_rgb, _rgb_to_256
from .term import pad, vlen

ART_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                       "art")

UPPER = "▀"
LOWER = "▄"
ASCII_SHADES = " .:-=+*#%@"
DIGITS = "0123456789abcdefghijklmnopqrstuvwxyz"

# widest first - the caller takes the first one that fits
VARIANTS = (36, 24, 16, 12)
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
    art = _load(NAMES.get(name, name), width)
    if not art:
        return 0, 0
    _palette, grid = art
    return max(len(r) for r in grid), (len(grid) + 1) // 2


def best(name: str, max_width: int, max_height: int) -> int:
    """The widest shipped variant that fits, or 0 when none does."""
    for w in VARIANTS:
        gw, gh = size(name, w)
        if gw and gw <= max_width and gh <= max_height:
            return w
    return 0


def _lift(rgb: tuple, th: Theme) -> tuple:
    r, g, b = rgb
    lum = 0.2126 * r + 0.7152 * g + 0.0722 * b
    if lum >= FLOOR:
        return rgb
    if max(rgb) < HUELESS:
        return NEUTRAL
    f = FLOOR / max(lum, 1.0)
    return (min(255, int(r * f)), min(255, int(g * f)), min(255, int(b * f)))


def _fg(rgb: tuple, th: Theme) -> str:
    if th.depth >= 24:
        return "\x1b[38;2;%d;%d;%dm" % rgb
    if th.depth >= 8:
        return f"\x1b[38;5;{_rgb_to_256(*rgb)}m"
    return ""


def _bg(rgb: tuple, th: Theme) -> str:
    if th.depth >= 24:
        return "\x1b[48;2;%d;%d;%dm" % rgb
    if th.depth >= 8:
        return f"\x1b[48;5;{_rgb_to_256(*rgb)}m"
    return ""


def render(name: str, th: Theme, width: int, *, centre: int = 0,
           solid: bool = False) -> list[str]:
    key = (NAMES.get(name, name), width, th.name, th.depth, th.uni, centre,
           solid)
    hit = _rows.get(key)
    if hit is not None:
        return hit
    out = _render(*key[:5], centre=centre, solid=solid)
    if len(_rows) > 64:
        _rows.clear()
    _rows[key] = out
    return out


def _render(name: str, width: int, theme_name: str, depth: int, uni: bool,
            *, centre: int, solid: bool = False) -> list[str]:
    th = Theme(theme_name)
    art = _load(name, width)
    if not art:
        return []
    palette, grid = art
    cols = max(len(r) for r in grid)
    colours = [_lift(c, th) for c in palette]

    def at(y: int, x: int):
        if y >= len(grid):
            return None
        row = grid[y]
        ch = row[x] if x < len(row) else "."
        if ch == ".":
            return None
        i = DIGITS.find(ch)
        return colours[i] if 0 <= i < len(colours) else None

    return _cells(grid, cols, at, th, uni, depth, centre, solid)


def _cells(grid, cols, at, th, uni, depth, centre, solid=False):
    """Fold pairs of pixel rows into half-block cells.

    `solid` gives up the half-block entirely and paints one colour per
    cell as a background. It costs half the vertical resolution and buys
    a mark that cannot be striped: some fonts draw U+2580 a few pixels
    below the top of the cell, and every row of them then shows a band of
    whatever is behind.
    """
    out = []
    for cy in range(0, len(grid), 2):
        line = []
        for x in range(cols):
            top, bottom = at(cy, x), at(cy + 1, x)
            if solid and uni and depth:
                line.append(_solid_cell(top, bottom, th))
                continue
            if not uni or depth == 0:
                line.append(_ascii_cell(top, bottom))
            elif top is None and bottom is None:
                line.append(" ")
            elif bottom is None:
                line.append(_fg(top, th) + UPPER + RESET)
            elif top is None:
                line.append(_fg(bottom, th) + LOWER + RESET)
            elif top == bottom:
                # A background fill covers the cell whatever the font does
                # with its glyphs. Painting U+2588 in the foreground relies
                # on the block reaching the cell edge, and where it falls
                # short the terminal shows through as a line between rows -
                # which is what striped the marks.
                line.append(_bg(top, th) + " " + RESET)
            else:
                line.append(_fg(top, th) + _bg(bottom, th) + UPPER + RESET)
        out.append("".join(line))
    pad = " " * max(0, (centre - cols) // 2)
    return [pad + l for l in out]


def _solid_cell(top, bottom, th) -> str:
    """One background-filled cell, the mean of the two halves."""
    halves = [c for c in (top, bottom) if c]
    if not halves:
        return " "
    mix = tuple(sum(c[i] for c in halves) // len(halves) for i in range(3))
    return _bg(mix, th) + " " + RESET


def _ascii_cell(top, bottom) -> str:
    """Luminance as a character, for terminals with no blocks or no colour."""
    vals = [0.2126 * c[0] + 0.7152 * c[1] + 0.0722 * c[2]
            for c in (top, bottom) if c]
    if not vals:
        return " "
    lum = sum(vals) / len(vals) / 255.0
    return ASCII_SHADES[min(len(ASCII_SHADES) - 1,
                            int(lum * len(ASCII_SHADES)))]


# -- the name --------------------------------------------------------
# The same drawing at every size, so narrowing the terminal shrinks the
# wordmark instead of swapping it for a different one. Shipped as coverage
# only - the colour is decided here, from the theme.
WORDMARK_WIDTHS = (140, 104, 80, 60, 44)


def wordmark_size(width: int, height: int) -> tuple[int, int]:
    """(columns, rows) of the largest wordmark that fits, or (0, 0)."""
    for w in WORDMARK_WIDTHS:
        gw, gh = size("wordmark", w)
        if gw and gw <= width and gh <= height:
            return gw, gh
    return 0, 0


def wordmark(th: Theme, width: int, height: int, *,
             centre: bool = True, solid: bool = False) -> list[str]:
    """The name, painted as a gradient from primary to accent.

    A flat colour over a wide face looks like a wall; the gradient gives it
    a direction without introducing a colour the theme does not have.
    """
    key = ("word", th.name, th.depth, th.uni, width, height, centre, solid)
    hit = _rows.get(key)
    if hit is not None:
        return hit
    out = _wordmark(th, width, height, centre, solid)
    if len(_rows) > 64:
        _rows.clear()
    _rows[key] = out
    return out


def _wordmark(th: Theme, width: int, height: int, centre: bool,
              solid: bool = False) -> list[str]:
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
    palette, grid = art
    cols = max(len(r) for r in grid)
    a = _hex_rgb(th.tokens.get("primary", "#7aa2f7"))
    b = _hex_rgb(th.tokens.get("accent", "#bb9af7"))
    # the shipped palette is a grey ramp: how much of the cell the letter
    # covers, which keeps the edges from looking chopped
    cover = [max(c) / 255.0 for c in palette]
    steps = [tuple(int(a[i] + (b[i] - a[i]) * (x / max(1, cols - 1)))
                   for i in range(3)) for x in range(cols)]

    def at(y: int, x: int):
        if y >= len(grid):
            return None
        row = grid[y]
        ch = row[x] if x < len(row) else "."
        if ch == ".":
            return None
        i = DIGITS.find(ch)
        if not 0 <= i < len(cover):
            return None
        f = cover[i]
        return tuple(int(c * f) for c in steps[min(x, cols - 1)])

    return _cells(grid, cols, at, th, th.uni, th.depth,
                  width if centre else 0, solid)


# -- the two together -------------------------------------------------
MARK_GAP = 3


def banner(th: Theme, width: int, height: int, *,
           solid: bool = False) -> list[str]:
    """The mark beside the name, as one centred block.

    The name decides the size - it is the part that has to be read - and
    the mark takes the largest variant that is no taller, so the pair
    keeps its proportions whatever the terminal gives them.
    """
    key = ("banner", th.name, th.depth, th.uni, width, height, solid)
    hit = _rows.get(key)
    if hit is not None:
        return hit
    out = _banner(th, width, height, solid)
    if len(_rows) > 64:
        _rows.clear()
    _rows[key] = out
    return out


def _banner(th: Theme, width: int, height: int,
            solid: bool = False) -> list[str]:
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
        word = wordmark(th, fallback, height, centre=False, solid=solid)
        return [pad("", max(0, (width - fallback) // 2)) + l for l in word]

    word_cols, mw, variant = pick
    # ask for exactly that size, or the largest one would come back again
    word = wordmark(th, word_cols, height, centre=False, solid=solid)
    if not word:
        return []
    ww, wh = vlen(word[0]), len(word)
    mark = render("xenproxy", th, variant, solid=solid)

    # sit the shorter of the two in the middle of the taller
    top = (wh - len(mark)) // 2
    rows = []
    left = pad("", max(0, (width - (mw + MARK_GAP + ww)) // 2))
    for i in range(wh):
        m = mark[i - top] if 0 <= i - top < len(mark) else ""
        rows.append(left + pad(m, mw) + " " * MARK_GAP + word[i])
    return rows
