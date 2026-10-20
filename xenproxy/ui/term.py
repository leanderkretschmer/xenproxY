"""Raw terminal control: alt screen, key decoding, frame diffing."""
from __future__ import annotations

import os
import re
import select
import shutil
import signal
import sys
import termios
import tty
import unicodedata

ANSI_RE = re.compile(r"\x1b\[[0-9;:]*[A-Za-z]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)")

ALT_ON = "\x1b[?1049h"
ALT_OFF = "\x1b[?1049l"
CUR_HIDE = "\x1b[?25l"
CUR_SHOW = "\x1b[?25h"
CLEAR = "\x1b[2J"
HOME = "\x1b[H"

KEYS = {
    "\x1b[A": "up", "\x1b[B": "down", "\x1b[C": "right", "\x1b[D": "left",
    "\x1bOA": "up", "\x1bOB": "down", "\x1bOC": "right", "\x1bOD": "left",
    "\x1b[H": "home", "\x1b[F": "end", "\x1b[1~": "home", "\x1b[4~": "end",
    "\x1b[5~": "pgup", "\x1b[6~": "pgdn", "\x1b[3~": "delete",
    "\x1b[Z": "shift-tab",
    "\r": "enter", "\n": "enter", "\t": "tab", "\x7f": "backspace",
    "\x08": "backspace", "\x1b": "esc", " ": "space",
}
for _i in range(1, 13):
    KEYS[f"\x1b[{10 + _i}~"] = f"f{_i}"
for _c in range(1, 27):
    KEYS.setdefault(chr(_c), f"ctrl-{chr(96 + _c)}")


def char_width(ch: str) -> int:
    if unicodedata.combining(ch):
        return 0
    return 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1


def strip_ansi(s: str) -> str:
    return ANSI_RE.sub("", s)


def vlen(s: str) -> int:
    """Visible width of a string, ignoring ANSI sequences."""
    return sum(char_width(c) for c in strip_ansi(s))


def truncate(s: str, width: int, ellipsis: str = "…") -> str:
    """Cut to `width` visible columns, preserving ANSI codes."""
    if vlen(s) <= width:
        return s
    if width <= 0:
        return ""
    out, used, i = [], 0, 0
    limit = width - len(ellipsis)
    while i < len(s):
        m = ANSI_RE.match(s, i)
        if m:
            out.append(m.group(0))
            i = m.end()
            continue
        w = char_width(s[i])
        if used + w > limit:
            break
        out.append(s[i])
        used += w
        i += 1
    return "".join(out) + ellipsis + "\x1b[0m"


def pad(s: str, width: int, align: str = "left") -> str:
    gap = width - vlen(s)
    if gap <= 0:
        return truncate(s, width)
    if align == "right":
        return " " * gap + s
    if align == "center":
        left = gap // 2
        return " " * left + s + " " * (gap - left)
    return s + " " * gap


class Term:
    def __init__(self) -> None:
        self.fd = sys.stdin.fileno()
        self._saved = None
        self._prev: list[str] = []
        self.resized = False
        self._buf = ""

    # -- lifecycle ---------------------------------------------------
    def size(self) -> tuple[int, int]:
        s = shutil.get_terminal_size(fallback=(100, 30))
        return max(40, s.columns), max(12, s.lines)

    def __enter__(self) -> "Term":
        try:
            self._saved = termios.tcgetattr(self.fd)
            tty.setraw(self.fd)
        except (termios.error, ValueError):
            self._saved = None
        sys.stdout.write(ALT_ON + CUR_HIDE + CLEAR)
        sys.stdout.flush()
        try:
            signal.signal(signal.SIGWINCH, self._on_resize)
        except (ValueError, AttributeError):
            pass
        return self

    def __exit__(self, *exc) -> None:
        sys.stdout.write(CUR_SHOW + ALT_OFF)
        sys.stdout.flush()
        if self._saved is not None:
            try:
                termios.tcsetattr(self.fd, termios.TCSADRAIN, self._saved)
            except (termios.error, ValueError):
                pass

    def _on_resize(self, *_a) -> None:
        self.resized = True
        self._prev = []

    # -- output ------------------------------------------------------
    def draw(self, lines: list[str]) -> None:
        """Repaint only the lines that changed since the last frame."""
        cols, rows = self.size()
        lines = lines[:rows]
        out = []
        for i in range(rows):
            new = lines[i] if i < len(lines) else ""
            old = self._prev[i] if i < len(self._prev) else None
            if new != old:
                out.append(f"\x1b[{i + 1};1H\x1b[2K{truncate(new, cols)}\x1b[0m")
        if out:
            sys.stdout.write("".join(out))
            sys.stdout.flush()
        self._prev = lines

    def invalidate(self) -> None:
        self._prev = []

    # -- input -------------------------------------------------------
    def read_key(self, timeout: float | None = None) -> str | None:
        """Return a key name, a single character, or None on timeout."""
        if not self._buf:
            r, _, _ = select.select([self.fd], [], [], timeout)
            if not r:
                return None
            try:
                data = os.read(self.fd, 1024)
            except OSError:
                return None
            if not data:
                return None
            self._buf += data.decode("utf-8", "replace")

        b = self._buf
        if b[0] == "\x1b" and len(b) > 1:
            for n in (6, 5, 4, 3, 2):
                if len(b) >= n and b[:n] in KEYS:
                    self._buf = b[n:]
                    return KEYS[b[:n]]
            m = re.match(r"\x1b\[[0-9;]*[A-Za-z~]", b)
            if m:
                self._buf = b[m.end():]
                return KEYS.get(m.group(0), "unknown")
            if b[1] == "\x1b":
                self._buf = b[1:]
                return "esc"
            self._buf = b[2:]
            return f"alt-{b[1]}"
        ch, self._buf = b[0], b[1:]
        return KEYS.get(ch, ch)
