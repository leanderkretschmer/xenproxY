"""Symlink-safe file access inside an untrusted mounted filesystem.

A migrated guest image is foreign data. Opening `<mnt>/etc/default/grub`
with a plain open() follows whatever symlink the image happens to carry -
an absolute link such as /etc/shadow resolves against the *host* root, so
a prepared image could make this tool overwrite host files while it runs
as root.

Every component is therefore opened with O_NOFOLLOW relative to the
directory above it, which makes the traversal refuse symlinks instead of
following them, and leaves no window between the check and the open.

Opens also carry O_NONBLOCK and the result is checked to be a regular
file: a FIFO sitting where a config file is expected would otherwise
block the opener for as long as nobody writes to it, which is enough to
wedge a migration with nothing but a named pipe in the image.
"""
from __future__ import annotations

import errno
import fcntl
import os
import stat as statmod

_DIR_FLAGS = os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC
if hasattr(os, "O_DIRECTORY"):
    _DIR_FLAGS |= os.O_DIRECTORY


class UnsafePath(OSError):
    """A path component was a symlink, or escaped the root."""


class SafeRoot:
    """Confined view of a directory tree."""

    def __init__(self, path: str) -> None:
        self.path = path
        self.fd = os.open(path, os.O_RDONLY | os.O_CLOEXEC |
                          getattr(os, "O_DIRECTORY", 0))

    def close(self) -> None:
        try:
            os.close(self.fd)
        except OSError:
            pass

    def __enter__(self) -> "SafeRoot":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # -- traversal ---------------------------------------------------
    def _descend(self, rel: str):
        parts = [p for p in rel.split("/") if p and p != "."]
        if not parts:
            raise UnsafePath(f"empty path: {rel!r}")
        if any(p == ".." for p in parts):
            raise UnsafePath(f"path escapes the root: {rel!r}")
        opened: list[int] = []
        cur = self.fd
        try:
            for part in parts[:-1]:
                fd = os.open(part, _DIR_FLAGS, dir_fd=cur)
                opened.append(fd)
                cur = fd
        except OSError as exc:
            for fd in reversed(opened):
                os.close(fd)
            if exc.errno in (errno.ELOOP, errno.ENOTDIR):
                raise UnsafePath(f"symlinked path component in {rel!r}") from exc
            raise
        return cur, parts[-1], opened

    def _open_raw(self, rel: str, flags: int, mode: int = 0o644) -> int:
        cur, name, opened = self._descend(rel)
        try:
            return os.open(name, flags | os.O_NOFOLLOW | os.O_CLOEXEC, mode,
                           dir_fd=cur)
        except OSError as exc:
            if exc.errno == errno.ELOOP:
                raise UnsafePath(f"{rel!r} is a symlink") from exc
            raise
        finally:
            for fd in reversed(opened):
                os.close(fd)

    def open(self, rel: str, flags: int, mode: int = 0o644) -> int:
        """Open a regular file. Anything else is refused."""
        fd = self._open_raw(rel, flags | os.O_NONBLOCK, mode)
        try:
            if not statmod.S_ISREG(os.fstat(fd).st_mode):
                raise UnsafePath(f"{rel!r} is not a regular file")
            # O_NONBLOCK was only there to survive the open; clear it so
            # ordinary reads and writes behave normally.
            fl = fcntl.fcntl(fd, fcntl.F_GETFL)
            fcntl.fcntl(fd, fcntl.F_SETFL, fl & ~os.O_NONBLOCK)
        except BaseException:
            os.close(fd)
            raise
        return fd

    def open_dir(self, rel: str) -> int:
        """Open a directory. O_DIRECTORY makes anything else fail."""
        return self._open_raw(rel, _DIR_FLAGS & ~os.O_NOFOLLOW | os.O_NOFOLLOW)

    # -- convenience -------------------------------------------------
    def exists(self, rel: str) -> bool:
        try:
            os.close(self.open(rel, os.O_RDONLY))
            return True
        except (OSError, UnsafePath):
            return False

    def is_dir(self, rel: str) -> bool:
        try:
            fd = self.open_dir(rel)
        except (OSError, UnsafePath):
            return False
        os.close(fd)
        return True

    def read_bytes(self, rel: str, limit: int = 64 << 20) -> bytes:
        fd = self.open(rel, os.O_RDONLY)
        with os.fdopen(fd, "rb") as fh:
            return fh.read(limit)

    def write_bytes(self, rel: str, data: bytes, mode: int = 0o644) -> None:
        fd = self.open(rel, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)

    def mkdir(self, rel: str, mode: int = 0o755) -> bool:
        """Create one directory. Returns False when it is already there."""
        cur, name, opened = self._descend(rel)
        try:
            os.mkdir(name, mode, dir_fd=cur)
            return True
        except FileExistsError:
            return False
        finally:
            for fd in reversed(opened):
                os.close(fd)

    def read_text(self, rel: str, limit: int = 4 << 20) -> str:
        fd = self.open(rel, os.O_RDONLY)
        with os.fdopen(fd, "rb") as fh:
            data = fh.read(limit)
        return data.decode("utf-8", "replace")

    def write_text(self, rel: str, text: str) -> None:
        fd = self.open(rel, os.O_WRONLY | os.O_TRUNC)
        with os.fdopen(fd, "wb") as fh:
            fh.write(text.encode("utf-8"))

    def backup(self, rel: str, suffix: str) -> bool:
        """Copy rel to rel+suffix unless the backup already exists."""
        dest = rel + suffix
        if self.exists(dest):
            return False
        data = self.read_text(rel)
        fd = self.open(dest, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as fh:
            fh.write(data.encode("utf-8"))
        return True

    def listdir(self, rel: str) -> list[str]:
        fd = self.open_dir(rel)
        try:
            return sorted(os.listdir(fd))
        finally:
            os.close(fd)

    def procfs_path(self, rel: str) -> tuple[str, int]:
        """A /proc/self/fd path for handing a verified file to a subprocess.

        The fd is already open on the real file, so the callee cannot be
        redirected by a symlink swapped in afterwards.
        """
        fd = self.open(rel, os.O_RDONLY)
        return f"/proc/self/fd/{fd}", fd
