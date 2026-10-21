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

from ..theme import RESET, Theme, _hex_rgb, _rgb_to_256

ART_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                       "art")

UPPER = "▀"
LOWER = "▄"
FULL = "█"
ASCII_SHADES = " .:-=+*#%@"
DIGITS = "0123456789abcdefghijklmnopqrstuvwxyz"

# widest first - the caller takes the first one that fits
VARIANTS = (36, 24, 16)
NAMES = {"xen": "xcp-ng", "xcp-ng": "xcp-ng", "incus": "incus",
         "proxmox": "proxmox"}

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


def render(name: str, th: Theme, width: int, *, centre: int = 0) -> list[str]:
    key = (NAMES.get(name, name), width, th.name, th.depth, th.uni, centre)
    hit = _rows.get(key)
    if hit is not None:
        return hit
    out = _render(*key[:5], centre=centre)
    if len(_rows) > 64:
        _rows.clear()
    _rows[key] = out
    return out


def _render(name: str, width: int, theme_name: str, depth: int, uni: bool,
            *, centre: int) -> list[str]:
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

    out = []
    for cy in range(0, len(grid), 2):
        line = []
        for x in range(cols):
            top, bottom = at(cy, x), at(cy + 1, x)
            if not uni or depth == 0:
                line.append(_ascii_cell(top, bottom))
            elif top is None and bottom is None:
                line.append(" ")
            elif bottom is None:
                line.append(_fg(top, th) + UPPER + RESET)
            elif top is None:
                line.append(_fg(bottom, th) + LOWER + RESET)
            elif top == bottom:
                line.append(_fg(top, th) + FULL + RESET)
            else:
                line.append(_fg(top, th) + _bg(bottom, th) + UPPER + RESET)
        out.append("".join(line))
    pad = " " * max(0, (centre - cols) // 2)
    return [pad + l for l in out]


def _ascii_cell(top, bottom) -> str:
    """Luminance as a character, for terminals with no blocks or no colour."""
    vals = [0.2126 * c[0] + 0.7152 * c[1] + 0.0722 * c[2]
            for c in (top, bottom) if c]
    if not vals:
        return " "
    lum = sum(vals) / len(vals) / 255.0
    return ASCII_SHADES[min(len(ASCII_SHADES) - 1,
                            int(lum * len(ASCII_SHADES)))]


# -- the name ---------------------------------------------------------
WORDMARKS = ("wordmark-full", "wordmark")
_text: dict = {}


def _text_art(name: str) -> list[str]:
    if name not in _text:
        try:
            with open(os.path.join(ART_DIR, f"{name}.txt"), encoding="utf-8") as fh:
                rows = [l.rstrip("\n") for l in fh]
        except OSError:
            rows = []
        while rows and not rows[-1].strip():
            rows.pop()
        _text[name] = rows
    return _text[name]


def wordmark_size(width: int, height: int) -> tuple[int, int]:
    """(columns, rows) of the largest wordmark that fits, or (0, 0)."""
    for name in WORDMARKS:
        rows = _text_art(name)
        if not rows:
            continue
        w = max(len(r) for r in rows)
        if w <= width and len(rows) <= height:
            return w, len(rows)
    return 0, 0


def wordmark(th: Theme, width: int, height: int, *,
             centre: bool = True) -> list[str]:
    """The name, painted as a gradient from primary to accent.

    A flat colour on six rows of block letters looks like a wall; the
    gradient gives it a direction without introducing a colour that is not
    already in the theme.
    """
    key = ("wordmark", th.name, th.depth, th.uni, width, height, centre)
    hit = _rows.get(key)
    if hit is not None:
        return hit
    out = _wordmark(th, width, height, centre)
    if len(_rows) > 64:
        _rows.clear()
    _rows[key] = out
    return out


def _wordmark(th: Theme, width: int, height: int, centre: bool) -> list[str]:
    chosen = ""
    for name in WORDMARKS:
        rows = _text_art(name)
        if rows and max(len(r) for r in rows) <= width and len(rows) <= height:
            chosen = name
            break
    if not chosen:
        return []
    rows = _text_art(chosen)
    cols = max(len(r) for r in rows)
    pad = " " * max(0, (width - cols) // 2) if centre else ""
    if th.depth < 24:
        return [pad + th.paint(r, "primary", bold=True) for r in rows]
    a = _hex_rgb(th.tokens.get("primary", "#7aa2f7"))
    b = _hex_rgb(th.tokens.get("accent", "#bb9af7"))
    steps = [tuple(int(a[i] + (b[i] - a[i]) * (x / max(1, cols - 1)))
                   for i in range(3)) for x in range(cols)]
    out = []
    for row in rows:
        line, last = [], None
        for x, ch in enumerate(row):
            if ch == " ":
                line.append(" ")
                continue
            col = steps[min(x, cols - 1)]
            if col != last:
                line.append(_fg(col, th))
                last = col
            line.append(ch)
        out.append(pad + "".join(line) + RESET)
    return out
