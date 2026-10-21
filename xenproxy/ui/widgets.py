"""Stateful, interactive components.

Every widget renders to a list of strings and, when handed a HitMap plus
the absolute screen position of its first character, registers what it drew
so the same element can be reached with the pointer. Keyboard and mouse
therefore always agree about what is where.
"""
from __future__ import annotations

from ..theme import RESET, Theme
from . import render as R
from .term import pad, strip_ansi, truncate, vlen


def _chip(th: Theme, text: str, state: str) -> str:
    """A button face. `state` is focused | hover | normal | disabled."""
    if state == "focused":
        return f"{th.bg('primary')}{th.fg('surface')}{text}{RESET}"
    if state == "hover":
        return f"{th.bg('surface')}{th.fg('accent')}{text}{RESET}"
    if state == "disabled":
        return th.paint(text, "border")
    return f"{th.bg('surface')}{th.fg('foreground')}{text}{RESET}"


class SelectList:
    """Arrow-key list. Items are (value, label, hint) triples."""

    def __init__(self, items: list[tuple] | None = None, *, cursor: int = 0,
                 pane: str = "list") -> None:
        self.all_items: list[tuple] = items or []
        self.items: list[tuple] = list(self.all_items)
        self.cursor = cursor
        self.offset = 0
        self.pane = pane
        self.height = 1
        self.query = ""

    def set_items(self, items: list[tuple], keep: bool = True) -> None:
        self.all_items = items
        self._apply(keep)

    def set_query(self, query: str) -> None:
        """Narrow the list to what matches. An inventory of fifty guests is
        a scroll; a name typed into it is one row."""
        self.query = query
        self._apply(True)

    def _apply(self, keep: bool = True) -> None:
        old = self.value if keep and self.items else None
        q = self.query.strip().lower()
        if q:
            # the hint may carry colour codes; match on what is readable
            self.items = [it for it in self.all_items
                          if q in strip_ansi(str(it[1])).lower()
                          or q in strip_ansi(str(it[0])).lower()
                          or (len(it) > 2 and q in strip_ansi(str(it[2])).lower())]
        else:
            self.items = list(self.all_items)
        if old is not None:
            for i, it in enumerate(self.items):
                if it[0] == old:
                    self.cursor = i
                    break
            else:
                self.cursor = min(self.cursor, max(0, len(self.items) - 1))
        else:
            self.cursor = min(self.cursor, max(0, len(self.items) - 1))
        self.cursor = max(0, self.cursor)

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

    def select(self, index: int) -> bool:
        if 0 <= index < len(self.items):
            changed = index != self.cursor
            self.cursor = index
            return changed or True
        return False

    def scroll(self, delta: int) -> bool:
        """Move the window without moving the selection - what a wheel does."""
        if len(self.items) <= self.height:
            return False
        top = max(0, min(self.offset + delta, len(self.items) - self.height))
        if top == self.offset:
            return False
        self.offset = top
        self.cursor = max(self.offset,
                          min(self.cursor, self.offset + self.height - 1))
        return True

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

    def render(self, th: Theme, width: int, height: int, *, focused: bool = True,
              hit=None, origin: tuple[int, int] | None = None,
              hover=None) -> list[str]:
        self.height = max(1, height)
        if not self.items:
            return [th.paint("  nothing here yet", "muted")]
        if self.cursor < self.offset:
            self.offset = self.cursor
        if self.cursor >= self.offset + height:
            self.offset = self.cursor - height + 1
        self.offset = max(0, min(self.offset, max(0, len(self.items) - height)))

        out = []
        for row, i in enumerate(range(self.offset,
                                      min(len(self.items), self.offset + height))):
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
            hovered = hover == (self.pane, i)
            if sel and focused:
                out.append(f"{th.bg('surface')}{th.fg('primary')}{line}{RESET}")
            elif sel:
                out.append(f"{th.bg('surface')}{th.fg('muted')}{line}{RESET}")
            elif hovered:
                out.append(f"{th.fg('accent')}{line}{RESET}")
            else:
                out.append(th.paint(line, "foreground"))
            if hit is not None and origin is not None:
                hit.add(origin[0], origin[1] + row, width, 1, "row",
                        (self.pane, i))
        if len(self.items) > height:
            out[-1] = truncate(out[-1], width - 5) + th.paint(
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

    hint = "type to edit, {tab} for the next field"

    def click(self, col: int) -> None:
        """Put the caret where the pointer landed. col is relative to the
        first character of the value."""
        self.cursor = max(0, min(len(self.value), col))

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

    def render(self, th: Theme, width: int, focused: bool, *, hit=None,
               origin=None, index: int = -1, hover=None) -> str:
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
        token = "primary" if focused else ("accent" if hover == index else "border")
        if hit is not None and origin is not None and index >= 0:
            hit.add(origin[0], origin[1], max(1, width), 1, "field", index)
            hit.add(origin[0] + 2, origin[1], max(1, width - 2), 1, "caret", index)
        return (th.paint(th.g["vline"] + " ", token)
                + truncate(body, max(1, width - 2)))


class Toggle:
    hint = "{space} or click toggles"

    def __init__(self, value: bool = False) -> None:
        self.value = bool(value)

    def key(self, k: str) -> bool:
        # enter is reserved for the screen's primary action, so it must not
        # also mean "flip this switch"
        if k in ("space", "left", "right", "h", "l"):
            self.value = not self.value
            return True
        return False

    def render(self, th: Theme, width: int = 0, focused: bool = False, *,
               hit=None, origin=None, index: int = -1, hover=None) -> str:
        label = f" {th.g['check']} on  " if self.value else f" {th.g['ring']} off "
        state = "focused" if focused else ("hover" if hover == index else "normal")
        if hit is not None and origin is not None and index >= 0:
            hit.add(origin[0], origin[1], vlen(label), 1, "toggle", index)
        return _chip(th, label, state)


class Choice:
    """A horizontal radio group."""

    hint = "{left}{right} or click to choose"

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
        if not self.options:
            return False
        if k in ("left", "h"):
            self.index = (self.index - 1) % len(self.options)
            return True
        if k in ("right", "l", "space"):
            self.index = (self.index + 1) % len(self.options)
            return True
        return False

    def render(self, th: Theme, width: int = 0, focused: bool = False, *,
               hit=None, origin=None, index: int = -1, hover=None) -> str:
        parts = []
        x = (origin[0] if origin else 0)
        for i, (_v, label) in enumerate(self.options):
            face = f" {label} "
            if i == self.index:
                state = "focused" if focused else "normal"
                parts.append(f"{th.bg('primary' if focused else 'accent')}"
                             f"{th.fg('surface')}{face}{RESET}")
            elif hover == (index, i):
                parts.append(_chip(th, face, "hover"))
            else:
                parts.append(th.paint(face, "muted"))
            if hit is not None and origin is not None and index >= 0:
                hit.add(x, origin[1], vlen(face), 1, "choice", (index, i))
            x += vlen(face)
        return "".join(parts)


class Button:
    def __init__(self, action: str, label: str, shortcut: str = "", *,
                 token: str = "primary", enabled: bool = True) -> None:
        self.action = action
        self.label = label
        self.shortcut = shortcut
        self.token = token
        self.enabled = enabled

    KEY_GLYPH = {"enter": "\u23ce", "esc": "esc", "space": "\u2423",
                 "tab": "\u21e5"}

    def face(self, th: Theme) -> str:
        shown = self.KEY_GLYPH.get(self.shortcut, self.shortcut) if th.uni \
            else self.shortcut
        key = f"  {shown}" if shown else ""
        return f" {self.label}{key} "


class ButtonBar:
    """A row of clickable actions.

    Buttons carry their own shortcut on the face, which is the whole point:
    the screen stops depending on an operator remembering a key list.
    """

    def __init__(self, buttons: list[Button] | None = None) -> None:
        self.buttons = buttons or []
        self.index = 0

    def set(self, buttons: list[Button]) -> None:
        keep = self.current.action if self.current else None
        self.buttons = buttons
        self.index = 0
        if keep:
            for i, b in enumerate(buttons):
                if b.action == keep:
                    self.index = i
                    break
        self._clamp()

    def _clamp(self) -> None:
        usable = [i for i, b in enumerate(self.buttons) if b.enabled]
        if not usable:
            self.index = 0
        elif self.index not in usable:
            self.index = usable[0]

    @property
    def current(self) -> Button | None:
        if 0 <= self.index < len(self.buttons):
            return self.buttons[self.index]
        return None

    def move(self, delta: int) -> bool:
        usable = [i for i, b in enumerate(self.buttons) if b.enabled]
        if len(usable) < 2:
            return False
        pos = usable.index(self.index) if self.index in usable else 0
        self.index = usable[(pos + delta) % len(usable)]
        return True

    def key(self, k: str) -> str | None:
        """Returns the action to run, or None."""
        if k in ("left", "h"):
            self.move(-1)
            return None
        if k in ("right", "l"):
            self.move(1)
            return None
        if k in ("enter", "space"):
            b = self.current
            return b.action if b and b.enabled else None
        for b in self.buttons:
            if b.shortcut and b.enabled and k == b.shortcut:
                return b.action
        return None

    def render(self, th: Theme, width: int, *, focused: bool = False,
               hit=None, origin=None, hover=None) -> list[str]:
        self._clamp()
        rows: list[str] = []
        line, x = "", 0
        for i, b in enumerate(self.buttons):
            face = b.face(th)
            w = vlen(face)
            if x and x + w + 1 > width:
                rows.append(line)
                line, x = "", 0
            if not b.enabled:
                state = "disabled"
            elif focused and i == self.index:
                state = "focused"
            elif hover == b.action:
                state = "hover"
            else:
                state = "normal"
            if hit is not None and origin is not None:
                hit.add(origin[0] + x, origin[1] + len(rows), w, 1,
                        "button", b.action)
            line += _chip(th, face, state) + " "
            x += w + 1
        if line:
            rows.append(line)
        return rows or [""]


class Field:
    def __init__(self, key: str, label: str, widget, *, help: str = "",
                 visible=None) -> None:
        self.key = key
        self.label = label
        self.widget = widget
        self.help = help
        self.visible = visible or (lambda _v: True)


class Form:
    """A vertical field list.

    Tab moves between fields and *stops* at the ends by returning False, so
    the screen around it can take focus on to its action bar. That is what
    makes tab mean one single thing everywhere: next control.
    """

    def __init__(self, fields: list[Field], *, wrap: bool = False) -> None:
        self.fields = fields
        self.focus = 0
        self.wrap = wrap

    def values(self) -> dict:
        out = {}
        for f in self.fields:
            out[f.key] = f.widget.value
        return out

    def active(self) -> list[Field]:
        vals = self.values()
        return [f for f in self.fields if f.visible(vals)]

    def _clamp(self) -> None:
        n = len(self.active())
        if n:
            self.focus = max(0, min(self.focus, n - 1))

    def focus_field(self, index: int) -> None:
        self._clamp()
        if 0 <= index < len(self.active()):
            self.focus = index

    @property
    def current(self):
        act = self.active()
        self._clamp()
        return act[self.focus] if act else None

    def key(self, k: str) -> bool:
        self._clamp()
        act = self.active()
        if not act:
            return False
        if k in ("tab", "down"):
            if self.focus + 1 < len(act):
                self.focus += 1
                return True
            if self.wrap:
                self.focus = 0
                return True
            return False
        if k in ("shift-tab", "up"):
            if self.focus > 0:
                self.focus -= 1
                return True
            if self.wrap:
                self.focus = len(act) - 1
                return True
            return False
        return act[self.focus].widget.key(k)

    def hint(self, th: Theme) -> str:
        f = self.current
        if not f:
            return ""
        raw = getattr(f.widget, "hint", "")
        return raw.format(tab="\u21e5" if th.uni else "tab", space="space",
                          left="\u2190" if th.uni else "<",
                          right="\u2192" if th.uni else ">")

    def render(self, th: Theme, width: int, *, focused: bool = True, hit=None,
               origin=None, hover=None) -> list[str]:
        self._clamp()
        act = self.active()
        lw = min(26, max((vlen(f.label) for f in act), default=10) + 2)
        out: list[str] = []
        for i, f in enumerate(act):
            is_focus = focused and i == self.focus
            label = th.paint(pad(truncate(f.label, lw), lw),
                             "primary" if is_focus else "muted", bold=is_focus)
            wo = (origin[0] + lw, origin[1] + len(out)) if origin else None
            if hit is not None and origin is not None:
                hit.add(origin[0], origin[1] + len(out), lw, 1, "field", i)
            out.append(label + f.widget.render(
                th, width - lw, is_focus, hit=hit, origin=wo, index=i,
                hover=hover))
            if is_focus and f.help:
                for line in R.wrap(f.help, width - lw - 2):
                    out.append(" " * lw + th.paint(line, "muted"))
        return out


class LogView:
    def __init__(self) -> None:
        self.offset = 0
        self.follow = True
        self.height = 10

    def scroll(self, delta: int, total: int) -> bool:
        top = max(0, min(self.offset + delta, max(0, total - self.height)))
        if top == self.offset:
            return False
        self.offset = top
        self.follow = self.offset >= max(0, total - self.height)
        return True

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
        self.height = max(1, height)
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
