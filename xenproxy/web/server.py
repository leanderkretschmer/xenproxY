"""A small HTTP front end for the same migrations the terminal UI drives.

Everything expensive is answered from the shared cache, so a page that has
been looked at once comes back instantly and the source host is asked again
only when the answer has gone stale or a refresh was requested.

The server is deliberately plain: the standard library's threading HTTP
server, a handful of JSON endpoints and one page. No framework, because the
tool installs onto a Proxmox node with nothing but Python on it.
"""
from __future__ import annotations

import json
import mimetypes
import os
import re
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .. import __version__, cache, config, jobs as jobmod, migrate, pve
from ..incus import IncusHost
from ..sshclient import SSHClient
from ..xen import XenHost
from . import auth

STATIC = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
MAX_BODY = 256 * 1024
NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 ._-]{0,62}$")

_jobs: dict = {}
_jobs_lock = threading.Lock()


# -- adapters ---------------------------------------------------------
def adapter(server: dict):
    ssh = SSHClient(server["host"], server.get("user", "root"),
                    server.get("password") or None, server.get("key") or None,
                    int(server.get("port", 22)))
    if server.get("kind", "xen") == "incus":
        return IncusHost(ssh)
    return XenHost(ssh, verify_tls=bool(server.get("verify_tls")),
                   tls_pin=server.get("tls_pin", ""),
                   insecure_tls=bool(server.get("insecure_tls")))


def public_server(s: dict) -> dict:
    """A server entry without its secrets."""
    return {"name": s.get("name", ""), "kind": s.get("kind", "xen"),
            "host": s.get("host", ""), "user": s.get("user", "root"),
            "port": int(s.get("port", 22)),
            "auth": "key" if s.get("key") else
                    ("password" if s.get("password") else "none"),
            "pinned": bool(s.get("tls_pin")), "added": s.get("added", 0)}


def guests_of(name: str, refresh: bool = False) -> list:
    key = ("guests", name)
    if refresh:
        cache.inventory.drop(key)
    hit = cache.inventory.get(key)
    if hit is not None:
        return hit
    srv = config.find_server(config.load(), name)
    if not srv:
        raise KeyError(name)
    host = adapter(srv)
    host.probe()
    return cache.inventory.put(key, host.list_vms())


def detail_of(name: str, ident: str, refresh: bool = False) -> dict:
    key = ("detail", name, ident)
    if refresh:
        cache.details.drop(key)
    hit = cache.details.get(key)
    if hit is not None:
        return hit
    srv = config.find_server(config.load(), name)
    if not srv:
        raise KeyError(name)
    return cache.details.put(key, adapter(srv).vm_detail(ident))


# -- job plumbing -----------------------------------------------------
def job_summary(job) -> dict:
    return {
        "id": job.id, "state": job.state, "vm_name": job.vm_name,
        "vmid": job.vmid, "mode": job.mode, "progress": job.progress,
        "bytes_done": job.bytes_done, "bytes_total": job.bytes_total,
        "rate": job.rate, "current_disk": job.current_disk,
        "error": job.error, "created": job.created,
        "duration": job.duration,
        "steps": [{"key": s.key, "title": s.title, "state": s.state,
                   "detail": s.detail, "duration": s.duration}
                  for s in job.steps],
    }


def start_migration(plan: dict) -> dict:
    srv = config.find_server(config.load(), plan.get("server", ""))
    if not srv:
        raise KeyError(plan.get("server", ""))
    host = adapter(srv)
    job = jobmod.Job(plan)
    mig = migrate.Migration(job, host, plan)
    with _jobs_lock:
        _jobs[job.id] = job
    threading.Thread(target=mig.run, daemon=True).start()
    return job_summary(job)


# -- request handling -------------------------------------------------
class Handler(BaseHTTPRequestHandler):
    server_version = f"xenproxY/{__version__}"
    protocol_version = "HTTP/1.1"

    # -- helpers ------------------------------------------------------
    def log_message(self, fmt, *args):            # quieter than the default
        if self.server.verbose:
            super().log_message(fmt, *args)

    def _send(self, code: int, body: bytes, ctype: str,
              extra: dict | None = None) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy",
                         "default-src 'self'; img-src 'self' data:; "
                         "style-src 'self' 'unsafe-inline'; "
                         "script-src 'self' 'unsafe-inline'")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def json(self, code: int, payload) -> None:
        self._send(code, json.dumps(payload).encode("utf-8"),
                   "application/json; charset=utf-8")

    def fail(self, code: int, message: str) -> None:
        self.json(code, {"error": message})

    def bearer(self) -> str:
        head = self.headers.get("Authorization", "")
        return head[7:].strip() if head.lower().startswith("bearer ") else ""

    def cookie_token(self) -> str:
        raw = self.headers.get("Cookie", "")
        for part in raw.split(";"):
            k, _, v = part.strip().partition("=")
            if k == "xenproxy":
                return urllib.parse.unquote(v)
        return ""

    def body(self) -> dict:
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            return {}
        if length <= 0 or length > MAX_BODY:
            return {}
        try:
            return json.loads(self.rfile.read(length).decode("utf-8"))
        except (OSError, ValueError):
            return {}

    # -- routing ------------------------------------------------------
    def do_GET(self):
        self.route("GET")

    def do_HEAD(self):
        self.route("GET")

    def do_POST(self):
        self.route("POST")

    def route(self, method: str) -> None:
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"
        query = urllib.parse.parse_qs(parsed.query)

        if path.startswith("/api/"):
            # Only the header is accepted here. The cookie exists so the
            # page can load; a request another site provokes carries the
            # cookie but cannot set a header, so it gets nowhere.
            if not auth.matches(self.bearer()):
                return self.fail(401, "a bearer token is required")
            return self.api(method, path[4:], query)

        if method != "GET":
            return self.fail(405, "method not allowed")
        offered = (query.get("token", [""])[0] or self.cookie_token()
                   or self.bearer())
        if not auth.matches(offered):
            return self._send(401, b"xenproxY: append ?token=... to the URL. "
                                   b"The token is printed when the server "
                                   b"starts and kept in "
                                   b"/etc/xenproxy/web-token.\n",
                              "text/plain; charset=utf-8")
        if path == "/":
            return self.page(offered)
        if path.startswith("/static/"):
            return self.static(path[len("/static/"):])
        return self._send(404, b"not found\n", "text/plain; charset=utf-8")

    def page(self, tok: str) -> None:
        try:
            with open(os.path.join(STATIC, "index.html"), "rb") as fh:
                html = fh.read()
        except OSError:
            return self._send(500, b"index.html is missing\n", "text/plain")
        html = html.replace(b"__TOKEN__", json.dumps(tok).encode("utf-8"))
        html = html.replace(b"__VERSION__", __version__.encode("utf-8"))
        cookie = (f"xenproxy={urllib.parse.quote(tok)}; Path=/; HttpOnly; "
                  "SameSite=Strict; Max-Age=86400")
        self._send(200, html, "text/html; charset=utf-8",
                   {"Set-Cookie": cookie})

    def static(self, name: str) -> None:
        # a fixed directory and a name with no separators in it: there is
        # nothing here for a traversal to walk through
        if not re.match(r"^[A-Za-z0-9._-]+$", name) or name.startswith("."):
            return self._send(404, b"not found\n", "text/plain")
        path = os.path.join(STATIC, name)
        if not os.path.isfile(path):
            return self._send(404, b"not found\n", "text/plain")
        with open(path, "rb") as fh:
            data = fh.read()
        ctype = mimetypes.guess_type(name)[0] or "application/octet-stream"
        self._send(200, data, ctype, {"Cache-Control": "max-age=300"})

    # -- the api ------------------------------------------------------
    def api(self, method: str, path: str, query: dict) -> None:
        refresh = query.get("refresh", ["0"])[0] not in ("0", "", "false")
        parts = [p for p in path.split("/") if p]
        try:
            if method == "GET" and parts == ["state"]:
                cfg = config.load()
                return self.json(200, {
                    "version": __version__,
                    "node": pve.node_name(), "pve": pve.version(),
                    "is_node": pve.is_pve_node(),
                    "servers": [public_server(s) for s in config.servers(cfg)],
                    "settings": cfg.get("settings", {}),
                })
            if method == "GET" and parts == ["targets"]:
                return self.json(200, {
                    "storages": pve.storages(), "bridges": pve.bridges(),
                    "next_vmid": pve.next_vmid(),
                    "modes": migrate.MODES,
                    "incus_modes": migrate.modes_for("incus"),
                    "net_modes": migrate.NET_MODES,
                })
            if method == "GET" and len(parts) == 3 and parts[0] == "servers" \
                    and parts[2] == "guests":
                name = parts[1]
                if not NAME_RE.match(name):
                    return self.fail(400, "implausible server name")
                return self.json(200, {"guests": guests_of(name, refresh),
                                       "age": cache.inventory.age(
                                           ("guests", name))})
            if method == "GET" and len(parts) == 4 and parts[0] == "servers" \
                    and parts[2] == "guests":
                name, ident = parts[1], parts[3]
                if not NAME_RE.match(name):
                    return self.fail(400, "implausible server name")
                return self.json(200, detail_of(name, ident, refresh))
            if method == "GET" and parts == ["jobs"]:
                with _jobs_lock:
                    live = [job_summary(j) for j in _jobs.values()]
                return self.json(200, {"live": live,
                                       "history": jobmod.load_jobs(40)})
            if method == "GET" and len(parts) == 2 and parts[0] == "jobs":
                with _jobs_lock:
                    job = _jobs.get(parts[1])
                if not job:
                    return self.fail(404, "no live job with that id")
                out = job_summary(job)
                out["log"] = [{"at": t, "kind": k, "text": m}
                              for t, k, m in job.log]
                return self.json(200, out)
            if method == "POST" and parts == ["migrate"]:
                plan = self.body()
                if not isinstance(plan, dict) or not plan.get("server"):
                    return self.fail(400, "a plan with a server is required")
                return self.json(202, start_migration(plan))
            if method == "POST" and len(parts) == 3 and parts[0] == "jobs" \
                    and parts[2] == "cancel":
                with _jobs_lock:
                    job = _jobs.get(parts[1])
                if not job:
                    return self.fail(404, "no live job with that id")
                job.cancel()
                return self.json(200, job_summary(job))
        except KeyError as exc:
            return self.fail(404, f"unknown server {exc}")
        except Exception as exc:                  # noqa: BLE001
            return self.fail(502, str(exc)[:400])
        self.fail(404, "no such endpoint")


class Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True
    verbose = False


def serve(host: str = "127.0.0.1", port: int = 8800,
          verbose: bool = False) -> int:
    tok = auth.token()
    httpd = Server((host, port), Handler)
    httpd.verbose = verbose
    shown = host if host not in ("0.0.0.0", "::") else pve.node_name()
    print(f"xenproxY {__version__} web interface")
    print(f"  http://{shown}:{port}/?token={tok}")
    if host not in ("127.0.0.1", "::1", "localhost"):
        print("  warning: this is reachable from the network. The token is "
              "the only thing in the way, and the connection is not "
              "encrypted - put it behind a reverse proxy with TLS.")
    print("  token file: " + auth.TOKEN_FILE)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped")
    finally:
        httpd.server_close()
    return 0
