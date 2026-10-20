"""Stateful, interactive components."""
from __future__ import annotations

from ..theme import RESET, Theme
from . import render as R
from .term import pad, truncate, vlen


class SelectList:
    """Arrow-key list. Items are (value, label, hint) triples."""

    def __init__(self, items: list[tuple] | None = None, *, cursor: int = 0) -> None:
        self.items: list[tuple] = items or []
        self.cursor = cursor
        self.offset = 0

    def set_items(self, items: list[tuple], keep: bool = True) -> None:
        old = self.value if keep and self.items else None
        self.items = items
        if old is not None:
            for i, it in enumerate(items):
                if it[0] == old:
                    self.cursor = i
                    break
            else:
                self.cursor = min(self.cursor, max(0, len(items) - 1))
        else:
            self.cursor = min(self.cursor, max(0, len(items) - 1))

    @property
    def value(self):
        if 0 <= self.cursor < len(self.items):
            return self.items[self.cursor][0]
        return None

    @property
    def item(self):
        if 0 <= self.cursor < len(self.items):
            return self.items[self.cursor]
        return None

    def key(self, k: str, page: int = 10) -> bool:
        n = len(self.items)
        if not n:
            return False
        if k in ("up", "k"):
            self.cursor = (self.cursor - 1) % n
        elif k in ("down", "j"):
            self.cursor = (self.cursor + 1) % n
        elif k == "pgup":
            self.cursor = max(0, self.cursor - page)
        elif k == "pgdn":
            self.cursor = min(n - 1, self.cursor + page)
        elif k == "home":
            self.cursor = 0
        elif k == "end":
            self.cursor = n - 1
        else:
            return False
        return True

    def render(self, th: Theme, width: int, height: int) -> list[str]:
        if not self.items:
            return [th.paint("  nothing here yet", "muted")]
        if self.cursor < self.offset:
            self.offset = self.cursor
        if self.cursor >= self.offset + height:
            self.offset = self.cursor - height + 1
        self.offset = max(0, min(self.offset, max(0, len(self.items) - height)))

        out = []
        for i in range(self.offset, min(len(self.items), self.offset + height)):
            value, label = self.items[i][0], self.items[i][1]
            hint = self.items[i][2] if len(self.items[i]) > 2 else ""
            sel = i == self.cursor
            mark = th.g["arrow"] if sel else " "
            line = f"{mark} {label}"
            if hint:
                gap = width - vlen(line) - vlen(hint)
                if gap < 2:
                    hint = truncate(hint, max(0, vlen(hint) + gap - 2))
                    gap = width - vlen(line) - vlen(hint)
                if hint and gap >= 2:
                    line = line + " " * gap + hint
                else:
                    line = truncate(line, width)
            line = pad(truncate(line, width), width)
            if sel:
                out.append(f"{th.bg('surface')}{th.fg('primary')}{line}{RESET}")
            else:
                out.append(th.paint(line, "foreground"))
        if len(self.items) > height:
            out[-1] = truncate(out[-1], width - 4) + th.paint(
                f" {self.offset + height}/{len(self.items)}", "muted")
        return out


class TextField:
    def __init__(self, value: str = "", *, masked: bool = False,
                 placeholder: str = "", numeric: bool = False) -> None:
        self.value = str(value)
        self.masked = masked
        self.placeholder = placeholder
        self.numeric = numeric
        self.cursor = len(self.value)

    def key(self, k: str) -> bool:
        if k == "backspace":
            if self.cursor > 0:
                self.value = self.value[:self.cursor - 1] + self.value[self.cursor:]
                self.cursor -= 1
            return True
        if k == "delete":
            self.value = self.value[:self.cursor] + self.value[self.cursor + 1:]
            return True
        if k == "left":
            self.cursor = max(0, self.cursor - 1)
            return True
        if k == "right":
            self.cursor = min(len(self.value), self.cursor + 1)
            return True
        if k == "home":
            self.cursor = 0
            return True
        if k == "end":
            self.cursor = len(self.value)
            return True
        if k == "ctrl-u":
            self.value, self.cursor = "", 0
            return True
        if k == "space":
            k = " "
        if len(k) == 1 and k.isprintable():
            if self.numeric and not k.isdigit():
                return True
            self.value = self.value[:self.cursor] + k + self.value[self.cursor:]
            self.cursor += 1
            return True
        return False

    def render(self, th: Theme, width: int, focused: bool) -> str:
        shown = "*" * len(self.value) if self.masked else self.value
        if not shown and not focused:
            body = th.paint(self.placeholder or " ", "muted")
        elif focused:
            c = min(self.cursor, len(shown))
            body = (th.paint(shown[:c], "foreground")
                    + f"{th.bg('primary')}{th.fg('surface')}"
                    + (shown[c] if c < len(shown) else " ") + RESET
                    + th.paint(shown[c + 1:], "foreground"))
        else:
            body = th.paint(shown, "foreground")
        token = "primary" if focused else "border"
        return (th.paint(th.g["vline"] + " ", token)
                + truncate(body, max(1, width - 2)))


class Toggle:
    def __init__(self, value: bool = False) -> None:
        self.value = bool(value)

    def key(self, k: str) -> bool:
        if k in ("space", "enter", "left", "right", "h", "l"):
            self.value = not self.value
            return True
        return False

    def render(self, th: Theme, width: int = 0, focused: bool = False) -> str:
        on = th.g["check"] if self.value else " "
        box = f"[{on}]"
        return th.paint(box, "success" if self.value else
                        ("primary" if focused else "muted"), bold=focused)


class Choice:
    """A horizontal radio group."""

    def __init__(self, options: list[tuple[str, str]], value: str = "") -> None:
        self.options = options
        self.index = 0
        for i, (v, _l) in enumerate(options):
            if v == value:
                self.index = i

    @property
    def value(self) -> str:
        return self.options[self.index][0] if self.options else ""

    def key(self, k: str) -> bool:
        if k in ("left", "h"):
            self.index = (self.index - 1) % len(self.options)
            return True
        if k in ("right", "l", "space"):
            self.index = (self.index + 1) % len(self.options)
            return True
        return False

    def render(self, th: Theme, width: int = 0, focused: bool = False) -> str:
        parts = []
        for i, (_v, label) in enumerate(self.options):
            if i == self.index:
                parts.append(f"{th.bg('primary')}{th.fg('surface')} {label} {RESET}")
            else:
                parts.append(th.paint(f" {label} ", "muted"))
        caret = th.paint(th.g["arrow"] + " ", "primary") if focused else "  "
        return caret + "".join(parts)


class Field:
    def __init__(self, key: str, label: str, widget, *, help: str = "",
                 visible=None) -> None:
        self.key = key
        self.label = label
        self.widget = widget
        self.help = help
        self.visible = visible or (lambda _v: True)


class Form:
    def __init__(self, fields: list[Field]) -> None:
        self.fields = fields
        self.focus = 0

    def values(self) -> dict:
        out = {}
        for f in self.fields:
            w = f.widget
            out[f.key] = w.value
        return out

    def active(self) -> list[Field]:
        vals = self.values()
        return [f for f in self.fields if f.visible(vals)]

    def _clamp(self) -> None:
        n = len(self.active())
        if n:
            self.focus = max(0, min(self.focus, n - 1))

    def key(self, k: str) -> bool:
        self._clamp()
        act = self.active()
        if not act:
            return False
        if k in ("tab", "down") and not isinstance(act[self.focus].widget, TextField):
            self.focus = (self.focus + 1) % len(act)
            return True
        if k in ("shift-tab", "up") and not isinstance(act[self.focus].widget, TextField):
            self.focus = (self.focus - 1) % len(act)
            return True
        if k in ("tab",):
            self.focus = (self.focus + 1) % len(act)
            return True
        if k in ("shift-tab",):
            self.focus = (self.focus - 1) % len(act)
            return True
        if k == "down" and isinstance(act[self.focus].widget, TextField):
            self.focus = (self.focus + 1) % len(act)
            return True
        if k == "up" and isinstance(act[self.focus].widget, TextField):
            self.focus = (self.focus - 1) % len(act)
            return True
        return act[self.focus].widget.key(k)

    def render(self, th: Theme, width: int) -> list[str]:
        self._clamp()
        act = self.active()
        lw = min(26, max((vlen(f.label) for f in act), default=10) + 2)
        out: list[str] = []
        for i, f in enumerate(act):
            focused = i == self.focus
            label = th.paint(pad(truncate(f.label, lw), lw),
                             "primary" if focused else "muted", bold=focused)
            out.append(label + f.widget.render(th, width - lw, focused))
            if focused and f.help:
                for line in R.wrap(f.help, width - lw - 2):
                    out.append(" " * lw + th.paint(line, "muted"))
        return out


class LogView:
    def __init__(self) -> None:
        self.offset = 0
        self.follow = True

    def key(self, k: str, total: int, height: int) -> bool:
        if k == "pgup":
            self.follow = False
            self.offset = max(0, self.offset - height)
            return True
        if k == "pgdn":
            self.offset = min(max(0, total - height), self.offset + height)
            if self.offset >= max(0, total - height):
                self.follow = True
            return True
        if k == "end":
            self.follow = True
            return True
        return False

    def render(self, th: Theme, entries: list[tuple], width: int,
               height: int) -> list[str]:
        if self.follow:
            self.offset = max(0, len(entries) - height)
        view = entries[self.offset:self.offset + height]
        tokens = {"info": "foreground", "success": "success",
                  "warning": "warning", "danger": "danger"}
        out = []
        import time as _t
        for ts, kind, msg in view:
            stamp = _t.strftime("%H:%M:%S", _t.localtime(ts))
            out.append(th.paint(stamp + " ", "muted")
                       + truncate(th.paint(msg, tokens.get(kind, "foreground")),
                                  width - 9))
        return out
