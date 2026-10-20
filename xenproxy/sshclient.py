"""SSH transport.

Key auth is preferred. Password auth goes through SSH_ASKPASS so that no
sshpass package is required and the secret never lands on disk.
"""
from __future__ import annotations

import atexit
import os
import shlex
import stat
import subprocess
import tempfile

from .text import scrub

KNOWN_HOSTS = "/etc/xenproxy/known_hosts"

# Strict, against our own known_hosts. accept-new would mean the very first
# connection trusts whatever answers - and that connection is the one that
# carries the password and fetches the TLS fingerprint, so a man in the
# middle there would be handed both. The host key is therefore confirmed by
# the operator before any credential is sent.
BASE_OPTS = [
    "-o", "StrictHostKeyChecking=yes",
    "-o", f"UserKnownHostsFile={KNOWN_HOSTS}",
    "-o", "GlobalKnownHostsFile=/dev/null",
    "-o", "ConnectTimeout=15",
    "-o", "BatchMode=no",
    "-o", "LogLevel=ERROR",
]
# aes128-gcm roughly doubles throughput over the default cipher on a LAN.
FAST_OPTS = ["-o", "Compression=no", "-c", "aes128-gcm@openssh.com"]

_ASKPASS = None
_ASKPASS_DIR = None
_ASKPASS_BODY = '#!/bin/sh\nprintf "%s\\n" "$XENPROXY_SSH_PW"\n'


def _askpass_ok(path: str) -> bool:
    """Only reuse a helper we still own and nobody else can write."""
    try:
        st = os.lstat(path)
    except OSError:
        return False
    return (stat.S_ISREG(st.st_mode)
            and st.st_uid == os.geteuid()
            and stat.S_IMODE(st.st_mode) == 0o700)


def _cleanup_askpass() -> None:
    global _ASKPASS, _ASKPASS_DIR
    for path in (_ASKPASS,):
        if path:
            try:
                os.unlink(path)
            except OSError:
                pass
    if _ASKPASS_DIR:
        try:
            os.rmdir(_ASKPASS_DIR)
        except OSError:
            pass
    _ASKPASS = _ASKPASS_DIR = None


def _askpass_script() -> str:
    """An executable ssh can call to obtain the password.

    It lives in a private 0700 directory rather than straight in /tmp, is
    re-verified before every reuse, and is removed at exit - it is an
    executable that speaks a secret, so it must not be something another
    local user can read, replace or keep.
    """
    global _ASKPASS, _ASKPASS_DIR
    if _ASKPASS and _askpass_ok(_ASKPASS):
        return _ASKPASS
    if _ASKPASS_DIR is None or not os.path.isdir(_ASKPASS_DIR):
        _ASKPASS_DIR = tempfile.mkdtemp(prefix="xenproxy-")
        os.chmod(_ASKPASS_DIR, 0o700)
        atexit.register(_cleanup_askpass)
    path = os.path.join(_ASKPASS_DIR, "askpass.sh")
    try:
        os.unlink(path)
    except OSError:
        pass
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o700)
    with os.fdopen(fd, "w") as fh:
        fh.write(_ASKPASS_BODY)
    _ASKPASS = path
    return path


class SSHError(RuntimeError):
    pass


class HostKeyUnknown(SSHError):
    """The host key has not been confirmed yet."""

    def __init__(self, host: str, port: int, keys: list) -> None:
        super().__init__(f"host key of {host} has not been confirmed")
        self.host = host
        self.port = port
        self.keys = keys


def _target(host: str, port: int) -> str:
    return host if int(port) == 22 else f"[{host}]:{int(port)}"


def _ensure_known_hosts() -> None:
    d = os.path.dirname(KNOWN_HOSTS)
    os.makedirs(d, mode=0o700, exist_ok=True)
    if not os.path.exists(KNOWN_HOSTS):
        fd = os.open(KNOWN_HOSTS, os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        os.close(fd)


def host_key_known(host: str, port: int = 22) -> bool:
    _ensure_known_hosts()
    cp = subprocess.run(["ssh-keygen", "-F", _target(host, port),
                         "-f", KNOWN_HOSTS],
                        capture_output=True, text=True, timeout=30,
                        stdin=subprocess.DEVNULL)
    return cp.returncode == 0 and bool(cp.stdout.strip())


def scan_host_keys(host: str, port: int = 22, timeout: int = 15) -> list[dict]:
    """Fetch the host keys without authenticating anything.

    ssh-keyscan sends no credentials, so this is safe to run before the
    operator has decided whether to trust the host.
    """
    cp = subprocess.run(["ssh-keyscan", "-T", str(timeout), "-p", str(int(port)),
                         host],
                        capture_output=True, text=True, timeout=timeout + 15,
                        stdin=subprocess.DEVNULL)
    out = []
    for line in cp.stdout.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        fp = subprocess.run(["ssh-keygen", "-lf", "-"], input=line + "\n",
                            capture_output=True, text=True, timeout=30)
        parts = fp.stdout.split()
        if len(parts) >= 2:
            out.append({"type": parts[-1].strip("()"), "bits": parts[0],
                        "fingerprint": parts[1], "line": line})
    if not out:
        raise SSHError(scrub((cp.stderr or "").strip())
                       or f"no SSH host key answered on {host}:{port}")
    return out


def trust_host_keys(keys: list[dict]) -> None:
    _ensure_known_hosts()
    existing = ""
    try:
        with open(KNOWN_HOSTS, encoding="utf-8") as fh:
            existing = fh.read()
    except OSError:
        pass
    with open(KNOWN_HOSTS, "a", encoding="utf-8") as fh:
        for k in keys:
            if k["line"] not in existing:
                fh.write(k["line"] + "\n")
    os.chmod(KNOWN_HOSTS, 0o600)


def forget_host(host: str, port: int = 22) -> None:
    if os.path.exists(KNOWN_HOSTS):
        subprocess.run(["ssh-keygen", "-R", _target(host, port), "-f", KNOWN_HOSTS],
                       capture_output=True, text=True, timeout=30,
                       stdin=subprocess.DEVNULL)


class SSHClient:
    def __init__(self, host: str, user: str = "root", password: str | None = None,
                 key: str | None = None, port: int = 22) -> None:
        self.host = host
        self.user = user
        self.password = password
        self.key = key
        self.port = int(port)

    # -- plumbing ----------------------------------------------------
    def _env(self) -> dict:
        env = os.environ.copy()
        if self.password and not self.key:
            env["SSH_ASKPASS"] = _askpass_script()
            env["SSH_ASKPASS_REQUIRE"] = "force"
            env.setdefault("DISPLAY", ":0")
            env["XENPROXY_SSH_PW"] = self.password
        return env

    def ssh_argv(self, *, fast: bool = False) -> list[str]:
        argv = ["ssh", *BASE_OPTS]
        if fast:
            argv += FAST_OPTS
        if self.key:
            argv += ["-i", self.key, "-o", "IdentitiesOnly=yes",
                     "-o", "PasswordAuthentication=no"]
        elif self.password:
            argv += ["-o", "PubkeyAuthentication=no",
                     "-o", "PreferredAuthentications=password,keyboard-interactive"]
        if self.port != 22:
            argv += ["-p", str(self.port)]
        argv.append(f"{self.user}@{self.host}")
        return argv

    def require_known_host(self) -> None:
        """Refuse to speak before the host key has been confirmed."""
        if host_key_known(self.host, self.port):
            return
        raise HostKeyUnknown(self.host, self.port,
                             scan_host_keys(self.host, self.port))

    def run(self, command: str, *, timeout: int = 60, check: bool = False,
            fast: bool = False) -> subprocess.CompletedProcess:
        self.require_known_host()
        argv = self.ssh_argv(fast=fast) + [command]
        try:
            cp = subprocess.run(argv, env=self._env(), capture_output=True,
                                text=True, timeout=timeout,
                                stdin=subprocess.DEVNULL)
        except subprocess.TimeoutExpired as exc:
            raise SSHError(f"timeout after {timeout}s: {command[:60]}") from exc
        if check and cp.returncode != 0:
            raise SSHError(scrub((cp.stderr or cp.stdout or "ssh failed").strip()))
        return cp

    def popen_stdout(self, command: str, *, fast: bool = True) -> subprocess.Popen:
        """Start a remote command and stream its stdout."""
        self.require_known_host()
        argv = self.ssh_argv(fast=fast) + [command]
        return subprocess.Popen(argv, env=self._env(), stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, bufsize=0,
                                stdin=subprocess.DEVNULL)

    # -- helpers -----------------------------------------------------
    def test(self, timeout: int = 20) -> tuple[bool, str]:
        try:
            cp = self.run("echo __xenproxy_ok__", timeout=timeout)
        except HostKeyUnknown:
            # Not a connection failure but a decision the operator owes us,
            # so it keeps its type all the way to the caller.
            raise
        except SSHError as exc:
            return False, str(exc)
        if "__xenproxy_ok__" in cp.stdout:
            return True, "connected"
        err = (cp.stderr or cp.stdout or "").strip().splitlines()
        return False, scrub(err[-1]) if err else f"exit {cp.returncode}"

    def deploy_key(self, pubkey: str) -> tuple[bool, str]:
        """Append a public key to the remote authorized_keys (opt-in)."""
        key = pubkey.strip()
        cmd = (
            "umask 077; mkdir -p ~/.ssh; touch ~/.ssh/authorized_keys; "
            f"grep -qxF {shlex.quote(key)} ~/.ssh/authorized_keys || "
            f"printf '%s\\n' {shlex.quote(key)} >> ~/.ssh/authorized_keys; "
            "echo __key_ok__"
        )
        try:
            cp = self.run(cmd, timeout=30)
        except SSHError as exc:
            return False, str(exc)
        if "__key_ok__" in cp.stdout:
            return True, "key installed"
        return False, scrub((cp.stderr or "failed").strip())
