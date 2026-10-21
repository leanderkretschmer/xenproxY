"""Theme tokens and border styles.

Token roles follow the termcn vocabulary: primary, accent, foreground,
muted, border, success, warning, danger, info.
"""
from __future__ import annotations

import os
import sys

RESET = "\x1b[0m"
BOLD = "\x1b[1m"
DIM = "\x1b[2m"
ITALIC = "\x1b[3m"
UNDERLINE = "\x1b[4m"
REVERSE = "\x1b[7m"

THEMES = {
    "tokyo-night": {
        "primary": "#7aa2f7", "accent": "#bb9af7", "foreground": "#c0caf5",
        "muted": "#565f89", "border": "#3b4261", "success": "#9ece6a",
        "warning": "#e0af68", "danger": "#f7768e", "info": "#7dcfff",
        "surface": "#1f2335",
    },
    "dracula": {
        "primary": "#bd93f9", "accent": "#ff79c6", "foreground": "#f8f8f2",
        "muted": "#6272a4", "border": "#44475a", "success": "#50fa7b",
        "warning": "#f1fa8c", "danger": "#ff5555", "info": "#8be9fd",
        "surface": "#282a36",
    },
    "nord": {
        "primary": "#88c0d0", "accent": "#b48ead", "foreground": "#eceff4",
        "muted": "#4c566a", "border": "#434c5e", "success": "#a3be8c",
        "warning": "#ebcb8b", "danger": "#bf616a", "info": "#81a1c1",
        "surface": "#3b4252",
    },
    "gruvbox": {
        "primary": "#83a598", "accent": "#d3869b", "foreground": "#ebdbb2",
        "muted": "#928374", "border": "#504945", "success": "#b8bb26",
        "warning": "#fabd2f", "danger": "#fb4934", "info": "#8ec07c",
        "surface": "#3c3836",
    },
    "default": {
        "primary": "#5f87ff", "accent": "#af87ff", "foreground": "#d0d0d0",
        "muted": "#767676", "border": "#444444", "success": "#87d787",
        "warning": "#d7af5f", "danger": "#d75f5f", "info": "#5fd7d7",
        "surface": "#262626",
    },
    "high-contrast": {
        "primary": "#ffffff", "accent": "#ffff00", "foreground": "#ffffff",
        "muted": "#bcbcbc", "border": "#ffffff", "success": "#00ff00",
        "warning": "#ffff00", "danger": "#ff0000", "info": "#00ffff",
        "surface": "#000000",
    },
}

BORDERS = {
    "rounded": "╭─╮│╰╯│─",
    "single": "┌─┐│└┘│─",
    "double": "╔═╗║╚╝║═",
    "ascii": "+-+|+++-",
}

GLYPHS_UNICODE = {
    "arrow": "❯", "bullet": "•", "check": "✓", "cross": "✗",
    "dot": "●", "ring": "○", "bar_full": "█", "bar_empty": "░",
    "bar_half": "▒", "tri_right": "▸", "tri_down": "▾",
    "ellipsis": "…", "vline": "│", "sep": "│",
    "spinner": "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏",
    "up": "↑", "down": "↓", "enter": "⏎", "warn": "⚠",
}
GLYPHS_ASCII = {
    "arrow": ">", "bullet": "*", "check": "v", "cross": "x",
    "dot": "o", "ring": "o", "bar_full": "#", "bar_empty": ".",
    "bar_half": "=", "tri_right": ">", "tri_down": "v",
    "ellipsis": "...", "vline": "|", "sep": "|",
    "spinner": "-\\|/", "up": "^", "down": "v", "enter": "<-", "warn": "!",
}


def unicode_ok() -> bool:
    if os.environ.get("NO_UNICODE"):
        return False
    enc = (getattr(sys.stdout, "encoding", "") or "").lower()
    return "utf" in enc


def color_depth() -> int:
    """24 for truecolor, 8 for 256-colour, 4 for basic ANSI."""
    if os.environ.get("NO_COLOR"):
        return 0
    ct = os.environ.get("COLORTERM", "").lower()
    if "truecolor" in ct or "24bit" in ct:
        return 24
    term = os.environ.get("TERM", "")
    if "256" in term:
        return 8
    if term in ("dumb", ""):
        return 0
    return 8


_ANSI16 = [
    (0, 0, 0), (205, 0, 0), (0, 205, 0), (205, 205, 0),
    (0, 0, 238), (205, 0, 205), (0, 205, 205), (229, 229, 229),
    (127, 127, 127), (255, 0, 0), (0, 255, 0), (255, 255, 0),
    (92, 92, 255), (255, 0, 255), (0, 255, 255), (255, 255, 255),
]


def _hex_rgb(h: str):
    h = h.lstrip("#")
    return int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)


def _rgb_to_256(r: int, g: int, b: int) -> int:
    if r == g == b:
        if r < 8:
            return 16
        if r > 248:
            return 231
        return 232 + (r - 8) * 24 // 247
    return 16 + 36 * (r * 5 // 255) + 6 * (g * 5 // 255) + (b * 5 // 255)


def _rgb_to_16(r: int, g: int, b: int) -> int:
    best, bd = 7, None
    for i, (cr, cg, cb) in enumerate(_ANSI16):
        d = (r - cr) ** 2 + (g - cg) ** 2 + (b - cb) ** 2
        if bd is None or d < bd:
            best, bd = i, d
    return best


class Theme:
    def __init__(self, name: str = "tokyo-night", border: str = "rounded"):
        self.name = name if name in THEMES else "tokyo-night"
        self.tokens = THEMES[self.name]
        self.depth = color_depth()
        self.uni = unicode_ok()
        self.g = GLYPHS_UNICODE if self.uni else GLYPHS_ASCII
        if border not in BORDERS or (border != "ascii" and not self.uni):
            border = border if self.uni else "ascii"
        self.border_chars = BORDERS.get(border, BORDERS["rounded"])
        self._fg_cache: dict = {}
        self._bg_cache: dict = {}

    def fg(self, token: str) -> str:
        # called a few thousand times per frame; the answer only depends on
        # the token and the colour depth, both fixed for this theme
        hit = self._fg_cache.get(token)
        if hit is None:
            hit = self._fg_cache[token] = self._fg(token)
        return hit

    def bg(self, token: str) -> str:
        hit = self._bg_cache.get(token)
        if hit is None:
            hit = self._bg_cache[token] = self._bg(token)
        return hit

    def _fg(self, token: str) -> str:
        if self.depth == 0:
            return ""
        h = self.tokens.get(token, token)
        r, g, b = _hex_rgb(h)
        if self.depth >= 24:
            return f"\x1b[38;2;{r};{g};{b}m"
        if self.depth >= 8:
            return f"\x1b[38;5;{_rgb_to_256(r, g, b)}m"
        i = _rgb_to_16(r, g, b)
        return f"\x1b[{30 + i}m" if i < 8 else f"\x1b[{90 + i - 8}m"

    def _bg(self, token: str) -> str:
        if self.depth == 0:
            return ""
        h = self.tokens.get(token, token)
        r, g, b = _hex_rgb(h)
        if self.depth >= 24:
            return f"\x1b[48;2;{r};{g};{b}m"
        if self.depth >= 8:
            return f"\x1b[48;5;{_rgb_to_256(r, g, b)}m"
        i = _rgb_to_16(r, g, b)
        return f"\x1b[{40 + i}m" if i < 8 else f"\x1b[{100 + i - 8}m"

    def paint(self, text: str, token: str, *, bold: bool = False,
              dim: bool = False, underline: bool = False) -> str:
        if self.depth == 0 and not bold:
            return text
        pre = ""
        if bold:
            pre += BOLD
        if dim:
            pre += DIM
        if underline:
            pre += UNDERLINE
        pre += self.fg(token)
        return f"{pre}{text}{RESET}" if pre else text
