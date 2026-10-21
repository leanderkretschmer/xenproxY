"""Click targets.

Every frame is rebuilt from scratch, and so is the map of what sits where.
Widgets register the absolute screen rectangle they drew into, and a mouse
event is resolved against the map rather than against the layout code - the
renderer stays the single source of truth for geometry.
"""
from __future__ import annotations


class Zone:
    __slots__ = ("x", "y", "w", "h", "action", "arg")

    def __init__(self, x: int, y: int, w: int, h: int, action: str, arg=None) -> None:
        self.x, self.y, self.w, self.h = x, y, w, h
        self.action = action
        self.arg = arg

    def contains(self, x: int, y: int) -> bool:
        return self.x <= x < self.x + self.w and self.y <= y < self.y + self.h

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return (f"Zone({self.action}, {self.arg!r}, "
                f"{self.x},{self.y} {self.w}x{self.h})")


class HitMap:
    """Rectangles in screen coordinates, innermost registered last."""

    def __init__(self) -> None:
        self.zones: list[Zone] = []

    def reset(self) -> None:
        self.zones.clear()

    def add(self, x: int, y: int, w: int, h: int, action: str, arg=None) -> None:
        if w > 0 and h > 0:
            self.zones.append(Zone(x, y, w, h, action, arg))

    def at(self, x: int, y: int) -> Zone | None:
        """The topmost zone under the pointer.

        Panels are registered before their contents, so walking backwards
        finds the row or button before the pane that holds it.
        """
        for z in reversed(self.zones):
            if z.contains(x, y):
                return z
        return None

    def find(self, x: int, y: int, *actions: str) -> Zone | None:
        """The topmost zone of one of `actions` - used for wheel events,
        which belong to the enclosing pane rather than the row under the
        pointer."""
        for z in reversed(self.zones):
            if z.action in actions and z.contains(x, y):
                return z
        return None
