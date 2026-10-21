"""Turn ANSI-styled terminal lines into a standalone SVG.

Only SGR sequences are interpreted - that is all the renderer emits.
Colours are written as plain fill attributes so the result survives
GitHub's SVG sanitiser.
"""
from __future__ import annotations

import re
import unicodedata

SGR = re.compile(r"\x1b\[([0-9;:]*)m")
# Everything except SGR ("m"), which parse_line interprets itself.
OTHER = re.compile(r"\x1b\[[0-9;:]*[A-Za-ln-z]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)")

CELL_RATIO = 0.6  # advance width of a monospace glyph, in em
LINE_RATIO = 1.34  # line height, in em
PAD_X = 18.0
PAD_Y = 16.0
TOPBAR = 34.0
FONT = ('ui-monospace,SFMono-Regular,"SF Mono",Menlo,Consolas,'
        '"DejaVu Sans Mono","Liberation Mono",monospace')

BASE16 = ["#15161e", "#f7768e", "#9ece6a", "#e0af68", "#7aa2f7", "#bb9af7",
          "#7dcfff", "#a9b1d6", "#414868", "#f7768e", "#9ece6a", "#e0af68",
          "#7aa2f7", "#bb9af7", "#7dcfff", "#c0caf5"]


def _cw(ch: str) -> int:
    if unicodedata.combining(ch):
        return 0
    return 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1


class Style:
    __slots__ = ("fg", "bg", "bold", "dim", "underline")

    def __init__(self) -> None:
        self.fg = None
        self.bg = None
        self.bold = False
        self.dim = False
        self.underline = False

    def copy(self) -> "Style":
        s = Style()
        s.fg, s.bg = self.fg, self.bg
        s.bold, s.dim, s.underline = self.bold, self.dim, self.underline
        return s

    def key(self):
        return (self.fg, self.bg, self.bold, self.dim, self.underline)


def _apply(style: Style, params: str) -> Style:
    parts = [p for p in params.replace(":", ";").split(";")] if params else ["0"]
    nums = [int(p) if p.isdigit() else 0 for p in parts]
    i = 0
    while i < len(nums):
        n = nums[i]
        if n == 0:
            style = Style()
        elif n == 1:
            style.bold = True
        elif n == 2:
            style.dim = True
        elif n == 4:
            style.underline = True
        elif n == 22:
            style.bold = style.dim = False
        elif n == 24:
            style.underline = False
        elif n == 39:
            style.fg = None
        elif n == 49:
            style.bg = None
        elif 30 <= n <= 37:
            style.fg = BASE16[n - 30]
        elif 90 <= n <= 97:
            style.fg = BASE16[n - 90 + 8]
        elif 40 <= n <= 47:
            style.bg = BASE16[n - 40]
        elif 100 <= n <= 107:
            style.bg = BASE16[n - 100 + 8]
        elif n in (38, 48) and i + 1 < len(nums):
            mode = nums[i + 1]
            if mode == 2 and i + 4 < len(nums):
                col = "#%02x%02x%02x" % (nums[i + 2], nums[i + 3], nums[i + 4])
                i += 4
            elif mode == 5 and i + 2 < len(nums):
                col = _xterm(nums[i + 2])
                i += 2
            else:
                i += 1
                col = None
            if col:
                if n == 38:
                    style.fg = col
                else:
                    style.bg = col
        i += 1
    return style


def _xterm(n: int) -> str:
    if n < 16:
        return BASE16[n]
    if n < 232:
        n -= 16
        r, g, b = n // 36, (n % 36) // 6, n % 6
        lvl = [0, 95, 135, 175, 215, 255]
        return "#%02x%02x%02x" % (lvl[r], lvl[g], lvl[b])
    v = 8 + (n - 232) * 10
    return "#%02x%02x%02x" % (v, v, v)


def parse_line(line: str, style: Style) -> tuple[list[tuple[int, str, Style]], Style]:
    """Split one line into (column, text, style) runs."""
    line = OTHER.sub("", line)
    runs: list[tuple[int, str, Style]] = []
    col = 0
    buf: list[str] = []
    buf_col = 0
    pos = 0
    cur = style.copy()
    for m in SGR.finditer(line):
        chunk = line[pos:m.start()]
        if chunk:
            if not buf:
                buf_col = col
            buf.append(chunk)
            col += sum(_cw(c) for c in chunk)
        if buf:
            runs.append((buf_col, "".join(buf), cur.copy()))
            buf = []
        cur = _apply(cur.copy(), m.group(1))
        pos = m.end()
    tail = line[pos:]
    if tail:
        runs.append((col, tail, cur.copy()))
    return runs, cur


# Box-drawing glyphs are drawn as vectors instead of text: a font glyph does
# not fill the whole cell, so stacked "|" would show gaps between rows.
BOX = {
    "\u2500": "lr", "\u2501": "lr", "\u2550": "lr",
    "\u2502": "tb", "\u2503": "tb", "\u2551": "tb",
    "\u250c": "rb", "\u256d": "rb", "\u2554": "rb",
    "\u2510": "lb", "\u256e": "lb", "\u2557": "lb",
    "\u2514": "rt", "\u2570": "rt", "\u255a": "rt",
    "\u2518": "lt", "\u256f": "lt", "\u255d": "lt",
    "\u251c": "tbr", "\u2524": "tbl", "\u252c": "lrb",
    "\u2534": "lrt", "\u253c": "lrtb",
}


# Shading blocks become solid rectangles so progress bars read as one bar.
BLOCKS = {"\u2588": 1.0, "\u2593": 0.75, "\u2592": 0.5, "\u2591": 0.25}
# Half blocks carry two colours in one cell - the glyph's top half is the
# foreground, the bottom half whatever the background rect already painted.
# Drawn as geometry rather than as a glyph, because a font's half block
# rarely fills the cell exactly and the gaps show up as a grid.
HALVES = {"\u2580": 0.0, "\u2584": 0.5}


def box_path(ch: str, x: float, y: float, cw: float, lh: float) -> str:
    """Line segments for one box-drawing cell, as SVG path data."""
    sides = BOX[ch]
    cx, cy = x + cw / 2.0, y + lh / 2.0
    seg = []
    if "l" in sides:
        seg.append(f"M{x:.2f},{cy:.2f}L{cx:.2f},{cy:.2f}")
    if "r" in sides:
        seg.append(f"M{cx:.2f},{cy:.2f}L{x + cw:.2f},{cy:.2f}")
    if "t" in sides:
        seg.append(f"M{cx:.2f},{y:.2f}L{cx:.2f},{cy:.2f}")
    if "b" in sides:
        seg.append(f"M{cx:.2f},{cy:.2f}L{cx:.2f},{y + lh:.2f}")
    return "".join(seg)


def esc(text: str) -> str:
    return (text.replace("&", "&amp;").replace("<", "&lt;")
                .replace(">", "&gt;").replace('"', "&quot;"))


def _emit_text(out: list, chars: list, col: int, row: int, cw: float,
               lh: float, top: float, y: float, colour: str, st, fg: str) -> None:
    text = "".join(chars)
    if not text.strip():
        return
    x = PAD_X + col * cw
    span = sum(_cw(c) for c in text) * cw
    attrs = [f'x="{x:.1f}"', f'y="{y:.1f}"', f'fill="{colour}"',
             'xml:space="preserve"', f'textLength="{span:.2f}"',
             'lengthAdjust="spacing"']
    if st.bold:
        attrs.append('font-weight="600"')
    if st.dim:
        attrs.append('opacity="0.65"')
    if st.underline:
        attrs.append('text-decoration="underline"')
    out.append(f'<text {" ".join(attrs)}>{esc(text)}</text>')


def render(lines: list[str], *, title: str = "", cols: int | None = None,
           bg: str = "#16161e", fg: str = "#c0caf5",
           font_size: float = 13.0, chrome: bool = True) -> str:
    cw = font_size * CELL_RATIO
    lh = font_size * LINE_RATIO
    width_cols = cols or max((sum(_cw(c) for c in OTHER.sub("", SGR.sub("", l)))
                              for l in lines), default=80)
    w = PAD_X * 2 + width_cols * cw
    top = TOPBAR if chrome else PAD_Y
    h = top + PAD_Y + len(lines) * lh

    out = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{w:.0f}" height="{h:.0f}" '
        f'viewBox="0 0 {w:.0f} {h:.0f}" font-family=\'{FONT}\' '
        f'font-size="{font_size}">',
        f'<rect x="0" y="0" width="{w:.0f}" height="{h:.0f}" rx="10" fill="{bg}"/>',
    ]
    if chrome:
        out.append(f'<rect x="0" y="0" width="{w:.0f}" height="{TOPBAR:.0f}" '
                   f'rx="10" fill="#1a1b26"/>')
        out.append(f'<rect x="0" y="{TOPBAR - 10:.0f}" width="{w:.0f}" '
                   f'height="10" fill="#1a1b26"/>')
        for i, c in enumerate(("#f7768e", "#e0af68", "#9ece6a")):
            out.append(f'<circle cx="{18 + i * 17}" cy="{TOPBAR / 2:.0f}" r="5.5" '
                       f'fill="{c}"/>')
        if title:
            out.append(f'<text x="{w / 2:.0f}" y="{TOPBAR / 2 + 4:.0f}" '
                       f'text-anchor="middle" fill="#565f89" '
                       f'font-size="{font_size - 1}">{esc(title)}</text>')

    style = Style()
    strokes: dict = {}
    for row, line in enumerate(lines):
        runs, style = parse_line(line, style)
        y = top + row * lh + lh * 0.74
        for col, text, st in runs:
            if not text.strip() and not st.bg:
                continue
            x = PAD_X + col * cw
            if st.bg:
                rw = sum(_cw(c) for c in text) * cw
                out.append(f'<rect x="{x:.1f}" y="{top + row * lh:.1f}" '
                           f'width="{rw:.2f}" height="{lh:.1f}" '
                           f'fill="{st.bg}" shape-rendering="crispEdges"/>')
            if not text.strip():
                continue
            colour = st.fg or fg
            chars = list(text)
            plain: list[str] = []
            plain_col = col
            c = col
            i = 0

            def flush(_c=None):
                nonlocal plain, plain_col
                if plain:
                    _emit_text(out, plain, plain_col, row, cw, lh, top, y,
                               colour, st, fg)
                    plain = []

            while i < len(chars):
                ch = chars[i]
                if ch in BLOCKS:
                    flush()
                    op = BLOCKS[ch]
                    j = i
                    while j < len(chars) and BLOCKS.get(chars[j]) == op:
                        j += 1
                    run_len = j - i
                    # one rectangle per block run - adjacent rects would show
                    # antialiasing seams at fractional coordinates
                    out.append(
                        f'<rect x="{PAD_X + c * cw:.2f}" '
                        f'y="{top + row * lh:.2f}" '
                        f'width="{run_len * cw:.2f}" height="{lh:.2f}" '
                        f'fill="{colour}" opacity="{op}" '
                        f'shape-rendering="crispEdges"/>')
                    c += run_len
                    i = j
                elif ch in HALVES:
                    flush()
                    off = HALVES[ch]
                    j = i
                    while j < len(chars) and chars[j] == ch:
                        j += 1
                    run_len = j - i
                    out.append(
                        f'<rect x="{PAD_X + c * cw:.2f}" '
                        f'y="{top + row * lh + lh * off:.2f}" '
                        f'width="{run_len * cw:.2f}" height="{lh / 2:.2f}" '
                        f'fill="{colour}" shape-rendering="crispEdges"/>')
                    c += run_len
                    i = j
                elif ch in BOX:
                    flush()
                    strokes.setdefault((colour, st.dim), []).append(
                        box_path(ch, PAD_X + c * cw, top + row * lh, cw, lh))
                    c += 1
                    i += 1
                else:
                    if not plain:
                        plain_col = c
                    plain.append(ch)
                    c += _cw(ch)
                    i += 1
            flush()

    for (colour, dim), segs in strokes.items():
        op = ' opacity="0.65"' if dim else ""
        out.append(f'<path d="{"".join(segs)}" stroke="{colour}" '
                   f'stroke-width="{max(1.0, font_size / 12.0):.2f}" fill="none" '
                   f'stroke-linecap="butt"{op}/>')
    out.append("</svg>")
    return "\n".join(out)
