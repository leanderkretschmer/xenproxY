"""Raw terminal control: alt screen, key and mouse decoding, frame diffing."""
from __future__ import annotations

import collections
import functools
import os
import re
import select
import shutil
import signal
import sys
import termios
import tty
import unicodedata

ANSI_RE = re.compile(r"\x1b\[[0-9;:<>?]*[A-Za-z]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)")

ALT_ON = "\x1b[?1049h"
ALT_OFF = "\x1b[?1049l"
CUR_HIDE = "\x1b[?25l"
CUR_SHOW = "\x1b[?25h"
CLEAR = "\x1b[2J"
HOME = "\x1b[H"

# 1000 = click reports, 1003 = motion reports (needed for hover highlights),
# 1006 = SGR coordinates, which lift the 223-column limit of the original
# encoding. 1015 is deliberately not requested: terminals that lack 1006 fall
# back to the legacy form, which is decoded below as well.
MOUSE_ON = "\x1b[?1000h\x1b[?1003h\x1b[?1006h"
MOUSE_OFF = "\x1b[?1006l\x1b[?1003l\x1b[?1000l"

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

SGR_MOUSE = re.compile(r"\x1b\[<(\d+);(\d+);(\d+)([Mm])")
X10_MOUSE = re.compile(r"\x1b\[M(...)", re.S)
# Anything that could still grow into a complete sequence is left in the
# buffer instead of being decoded as a stray escape.
PARTIAL = re.compile(r"\x1b(\[[0-9;:<>?]*|O|\[M.{0,2})?$", re.S)


class Mouse:
    """One pointer report, in 0-based screen coordinates."""

    __slots__ = ("kind", "button", "x", "y")

    def __init__(self, kind: str, button: int, x: int, y: int) -> None:
        self.kind = kind          # press | release | move | wheel-up | wheel-down
        self.button = button      # 1 left, 2 middle, 3 right, 0 none
        self.x = x
        self.y = y

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"Mouse({self.kind}, b{self.button}, {self.x},{self.y})"


def _decode_mouse(cb: int, x: int, y: int, release: bool) -> Mouse:
    button = (cb & 3) + 1
    if cb & 64:
        return Mouse("wheel-up" if (cb & 1) == 0 else "wheel-down", 0, x, y)
    if cb & 32:
        # motion; the button bits say which button is held, 4 means none
        return Mouse("move", 0 if (cb & 3) == 3 else button, x, y)
    if release or (cb & 3) == 3:
        return Mouse("release", 0 if (cb & 3) == 3 else button, x, y)
    return Mouse("press", button, x, y)


@functools.lru_cache(maxsize=4096)
def char_width(ch: str) -> int:
    if unicodedata.combining(ch):
        return 0
    return 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1


def strip_ansi(s: str) -> str:
    # the regex is the expensive part and most strings never need it
    return ANSI_RE.sub("", s) if "\x1b" in s else s


def vlen(s: str) -> int:
    """Visible width of a string, ignoring ANSI sequences."""
    plain = strip_ansi(s)
    if plain.isascii():
        return len(plain)       # the common case, without a per-character walk
    return sum(char_width(c) for c in plain)


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


def slice_cols(s: str, start: int, end: int | None = None) -> str:
    """The visible columns [start, end) of a styled string.

    Colour codes seen before `start` are re-emitted, so a slice taken out of
    the middle of a line keeps the styling it was drawn with. Used to put a
    modal on top of a frame without shearing off what sits to its right.
    """
    out, col, i = [], 0, 0
    state: list[str] = []
    while i < len(s):
        m = ANSI_RE.match(s, i)
        if m:
            code = m.group(0)
            if col >= start:
                out.append(code)
            else:
                if code in ("\x1b[0m", "\x1b[m"):
                    state.clear()
                else:
                    state.append(code)
            i = m.end()
            continue
        w = char_width(s[i])
        if end is not None and col + w > end:
            break
        if col >= start:
            out.append(s[i])
        col += w
        i += 1
    body = "".join(out)
    if not body.strip():
        return body
    return "".join(state) + body


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
    def __init__(self, mouse: bool = True) -> None:
        self.fd = sys.stdin.fileno()
        self._saved = None
        self._prev: list[str] = []
        self.resized = False
        self._buf = ""
        self._events: collections.deque = collections.deque()
        self.mouse = bool(mouse)
        self._mouse_on = False

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
        if self.mouse:
            sys.stdout.write(MOUSE_ON)
            self._mouse_on = True
        sys.stdout.flush()
        try:
            signal.signal(signal.SIGWINCH, self._on_resize)
        except (ValueError, AttributeError):
            pass
        return self

    def __exit__(self, *exc) -> None:
        if self._mouse_on:
            sys.stdout.write(MOUSE_OFF)
            self._mouse_on = False
        sys.stdout.write(CUR_SHOW + ALT_OFF)
        sys.stdout.flush()
        if self._saved is not None:
            try:
                termios.tcsetattr(self.fd, termios.TCSADRAIN, self._saved)
            except (termios.error, ValueError):
                pass

    def set_mouse(self, on: bool) -> None:
        """Turn pointer reporting on or off at runtime.

        Worth having as a switch: while the terminal reports mouse events the
        usual click-and-drag text selection no longer reaches the terminal
        emulator, so anyone who wants to copy something out needs it off.
        """
        on = bool(on)
        self.mouse = on
        if on and not self._mouse_on:
            sys.stdout.write(MOUSE_ON)
            self._mouse_on = True
        elif not on and self._mouse_on:
            sys.stdout.write(MOUSE_OFF)
            self._mouse_on = False
        sys.stdout.flush()
        self._events.clear()

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
    def _fill(self, timeout: float | None) -> bool:
        r, _, _ = select.select([self.fd], [], [], timeout)
        if not r:
            return False
        try:
            data = os.read(self.fd, 4096)
        except OSError:
            return False
        if not data:
            return False
        self._buf += data.decode("utf-8", "replace")
        return True

    def _push(self, ev) -> None:
        # A pointer dragged across the screen produces far more reports than
        # the render loop needs; only the newest position matters.
        if isinstance(ev, Mouse) and ev.kind == "move" and self._events:
            last = self._events[-1]
            if isinstance(last, Mouse) and last.kind == "move":
                self._events[-1] = ev
                return
        self._events.append(ev)

    def _parse(self) -> None:
        while self._buf:
            b = self._buf
            if b[0] != "\x1b":
                ch, self._buf = b[0], b[1:]
                self._push(KEYS.get(ch, ch))
                continue
            if b == "\x1b":
                self._buf = ""
                self._push("esc")
                continue
            m = SGR_MOUSE.match(b)
            if m:
                self._buf = b[m.end():]
                if self.mouse:
                    self._push(_decode_mouse(
                        int(m.group(1)), int(m.group(2)) - 1,
                        int(m.group(3)) - 1, m.group(4) == "m"))
                continue
            m = X10_MOUSE.match(b)
            if m:
                self._buf = b[m.end():]
                if self.mouse:
                    cb, cx, cy = (ord(c) for c in m.group(1))
                    self._push(_decode_mouse(cb - 32, cx - 33, cy - 33, False))
                continue
            hit = None
            for n in (6, 5, 4, 3, 2):
                if len(b) >= n and b[:n] in KEYS:
                    hit = b[:n]
                    break
            if hit:
                self._buf = b[len(hit):]
                self._push(KEYS[hit])
                continue
            m = re.match(r"\x1b\[[0-9;]*[A-Za-z~]", b)
            if m:
                self._buf = b[m.end():]
                self._push(KEYS.get(m.group(0), "unknown"))
                continue
            if PARTIAL.match(b):
                return          # wait for the rest of the sequence
            if b[1] == "\x1b":
                self._buf = b[1:]
                self._push("esc")
                continue
            self._buf = b[2:]
            self._push(f"alt-{b[1]}")

    def read_key(self, timeout: float | None = None):
        """A key name, a single character, a Mouse event, or None."""
        if not self._events:
            if not self._fill(timeout):
                return None
            self._parse()
            if not self._events and self._buf:
                # an escape sequence arrived in pieces; give it one more
                # chance before handing back control
                if self._fill(0.02):
                    self._parse()
        return self._events.popleft() if self._events else None
