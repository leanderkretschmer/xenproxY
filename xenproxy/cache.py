"""A small time-to-live store.

Both front ends ask the same expensive questions - what guests does this
host have, what does this one look like - and neither should pay for the
answer twice. Entries are kept per key with their own lifetime, and a
refresh can be asked for explicitly after something that changes them.
"""
from __future__ import annotations

import threading
import time


class TTLCache:
    def __init__(self, default_ttl: float = 120.0, limit: int = 512) -> None:
        self.default_ttl = default_ttl
        self.limit = limit
        self._data: dict = {}
        self._lock = threading.Lock()

    def get(self, key, default=None):
        with self._lock:
            hit = self._data.get(key)
            if hit is None:
                return default
            at, ttl, value = hit
            if time.monotonic() - at > ttl:
                self._data.pop(key, None)
                return default
            return value

    def age(self, key) -> float:
        """Seconds since this answer was produced, or -1 when there is none."""
        with self._lock:
            hit = self._data.get(key)
            return time.monotonic() - hit[0] if hit else -1.0

    def put(self, key, value, ttl: float | None = None):
        with self._lock:
            if len(self._data) >= self.limit:
                # drop the oldest rather than grow without bound
                oldest = min(self._data, key=lambda k: self._data[k][0])
                self._data.pop(oldest, None)
            self._data[key] = (time.monotonic(),
                               self.default_ttl if ttl is None else ttl, value)
        return value

    def fetch(self, key, produce, ttl: float | None = None):
        """Return the cached answer, or produce and keep a fresh one."""
        hit = self.get(key, _MISS)
        if hit is not _MISS:
            return hit
        return self.put(key, produce(), ttl)

    def drop(self, *keys) -> None:
        with self._lock:
            if not keys:
                self._data.clear()
            for k in keys:
                self._data.pop(k, None)

    def drop_prefix(self, prefix) -> None:
        """Forget every key that starts with this tuple."""
        with self._lock:
            for k in [k for k in self._data
                      if isinstance(k, tuple) and k[:len(prefix)] == prefix]:
                self._data.pop(k, None)


class _Missing:
    pass


_MISS = _Missing()

# Shared by the terminal interface and the web server.
inventory = TTLCache(default_ttl=180.0)
details = TTLCache(default_ttl=600.0)
