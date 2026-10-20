"""Interactive application shell."""
from __future__ import annotations

import threading
import time

from .. import __version__, config, jobs as jobmod, migrate, pve, update
from ..sshclient import (SSHClient, SSHError, host_key_known,
                         scan_host_keys, trust_host_keys)
from ..text import scrub
from ..theme import RESET, Theme
from ..xen import XenHost
from . import render as R
from .term import Term, pad, truncate, vlen
from .widgets import Choice, Field, Form, LogView, SelectList, TextField, Toggle

NAV = [("servers", "Servers"), ("guests", "Guests"),
       ("jobs", "Jobs"), ("settings", "Settings")]


def _short_bytes(n: float) -> str:
    """Compact size for narrow list columns: 16G, 512M."""
    for unit in ("B", "K", "M", "G", "T"):
        if abs(n) < 1024:
            return f"{n:.0f}{unit}"
        n /= 1024
    return f"{n:.0f}P"


def _guest_hint(g: dict) -> str:
    state = {"running": "run", "halted": "off", "suspended": "sus"}.get(
        g.get("power_state", ""), g.get("power_state", "")[:3])
    return f"{state} {_short_bytes(g.get('memory', 0))} {g.get('vcpus', 0)}c"


class Task:
    """A piece of background work the UI can poll."""

    def __init__(self, fn, *args, **kw) -> None:
        self.done = False
        self.result = None
        self.error = ""
        self.started = time.time()
        self._t = threading.Thread(target=self._run, args=(fn, args, kw), daemon=True)
        self._t.start()

    def _run(self, fn, args, kw) -> None:
        try:
            self.result = fn(*args, **kw)
        except Exception as exc:  # noqa: BLE001 - shown in the UI
            self.error = str(exc)
        finally:
            self.done = True


class App:
    def __init__(self) -> None:
        self.cfg = config.load()
        self.th = Theme(config.setting(self.cfg, "theme", "tokyo-night"),
                        config.setting(self.cfg, "border", "rounded"))
        self.running = True
        self.tick = 0
        self.screen = "servers"
        self.nav_index = 0
        self.focus = "content"
        self.toast = ""
        self.toast_kind = "info"
        self.toast_until = 0.0

        self.server_list = SelectList()
        self.guest_list = SelectList()
        self.job_list = SelectList()
        self.settings_form: Form | None = None
        self.logview = LogView()

        self.active_server: dict | None = None
        self.xen: XenHost | None = None
        self.guests: list[dict] = []
        self.guest_detail: dict | None = None
        self.detail_cache: dict = {}

        self.load_task: Task | None = None
        self.detail_task: Task | None = None
        self.probe_task: Task | None = None
        self.update_task: Task | None = None
        self.update_info: dict | None = None

        self.overlay = None      # dict describing a modal
        self.wizard = None       # WizardState
        self.live_jobs: list[jobmod.Job] = []
        self.open_job: jobmod.Job | None = None

        self.refresh_servers()
        if config.setting(self.cfg, "check_updates", True):
            self.update_task = Task(update.check)

    # -- helpers -----------------------------------------------------
    def notify(self, text: str, kind: str = "info", secs: float = 4.0) -> None:
        self.toast = scrub(text)
        self.toast_kind = kind
        self.toast_until = time.time() + secs

    def refresh_servers(self) -> None:
        items = []
        for s in config.servers(self.cfg):
            hint = f"{s.get('user', 'root')}@{s.get('host', '')}"
            items.append((s["name"], s["name"], hint))
        self.server_list.set_items(items)

    def connect(self, server: dict) -> XenHost:
        ssh = SSHClient(server["host"], server.get("user", "root"),
                        server.get("password") or None,
                        server.get("key") or None,
                        int(server.get("port", 22)))
        return XenHost(ssh, verify_tls=bool(server.get("verify_tls")),
                       tls_pin=server.get("tls_pin", ""),
                       insecure_tls=bool(server.get("insecure_tls")))

    def select_server(self, name: str) -> None:
        server = config.find_server(self.cfg, name)
        if not server:
            return
        self.active_server = server
        self.xen = self.connect(server)
        self.guests = []
        self.guest_detail = None
        self.detail_cache = {}
        self.screen = "guests"
        self.nav_index = 1
        self.load_guests()

    def load_guests(self) -> None:
        if not self.xen:
            return
        self.load_task = Task(self._load_guests_inner)

    def _load_guests_inner(self) -> list[dict]:
        self.xen.probe()
        return self.xen.list_vms()

    def load_detail(self, uuid: str) -> None:
        if not self.xen or uuid in self.detail_cache:
            self.guest_detail = self.detail_cache.get(uuid)
            return
        self.detail_task = Task(self.xen.vm_detail, uuid)

    # -- main loop ---------------------------------------------------
    def run(self) -> int:
        if not config.servers(self.cfg):
            self.open_server_modal(first_run=True)
        with Term() as term:
            self.term = term
            while self.running:
                self.poll()
                cols, rows = term.size()
                term.draw(self.compose(cols, rows))
                key = term.read_key(timeout=0.15)
                self.tick += 1
                if key:
                    try:
                        self.handle(key)
                    except Exception as exc:  # noqa: BLE001
                        self.notify(str(exc), "danger", 6)
        return 0

    def poll(self) -> None:
        self.poll_overlay()
        if self.load_task and self.load_task.done:
            if self.load_task.error:
                self.notify(self.load_task.error, "danger", 8)
            else:
                self.guests = self.load_task.result or []
                self.guest_list.set_items(
                    [(g["uuid"], g["name"], _guest_hint(g))
                     for g in self.guests])
            self.load_task = None
        if self.detail_task and self.detail_task.done:
            if self.detail_task.error:
                self.notify(self.detail_task.error, "danger", 8)
            else:
                d = self.detail_task.result
                if d:
                    self.detail_cache[d["uuid"]] = d
                    self.guest_detail = d
            self.detail_task = None
        if self.probe_task and self.probe_task.done:
            if self.probe_task.error:
                self.notify(f"connection failed: {self.probe_task.error}", "danger", 8)
            else:
                info = self.probe_task.result or {}
                self.notify(f"reachable: {info.get('hostname', '')} "
                            f"({info.get('version', '')})", "success", 6)
            self.probe_task = None
        if self.update_task and self.update_task.done:
            self.update_info = self.update_task.result
            if self.update_info:
                self.open_update_modal()
            self.update_task = None

    # -- layout ------------------------------------------------------
    def compose(self, cols: int, rows: int) -> list[str]:
        th = self.th
        head = self.header(cols)
        foot = self.footer(cols)
        body_h = max(3, rows - len(head) - len(foot))

        side_w = 18 if cols >= 80 else 0
        if side_w:
            content_w = cols - side_w - 1
            side = self.sidebar(side_w, body_h)
            content = self.content(content_w, body_h)
            body = R.columns([side, content], [side_w, content_w], gap=1)
        else:
            body = self.content(cols, body_h)
        frame = head + body[:body_h] + foot
        if self.overlay:
            frame = self.draw_overlay(frame, cols, rows)
        return frame

    def header(self, cols: int) -> list[str]:
        th = self.th
        left = th.paint(" xenproxY ", "primary", bold=True)
        if self.active_server:
            left += th.paint(f" {th.g['tri_right']} {self.active_server['name']}",
                             "muted")
        right = th.paint(f"v{__version__} ", "muted")
        if self.update_info:
            right = th.paint(f"update {self.update_info['version']} ", "warning") + right
        gap = max(0, cols - vlen(left) - vlen(right))
        return [left + " " * gap + right, R.divider(th, cols)]

    def footer(self, cols: int) -> list[str]:
        th = self.th
        if time.time() < self.toast_until and self.toast:
            return [R.divider(th, cols),
                    truncate(R.status_message(th, self.toast, self.toast_kind), cols)]
        keys = {
            "servers": [("a", "add"), ("t", "test"), ("e", "edit"), ("d", "delete"),
                        (th.g["enter"], "open"), ("tab", "nav"), ("q", "quit")],
            "guests": [("m", "migrate"), ("r", "refresh"), (th.g["enter"], "details"),
                       ("tab", "nav"), ("q", "quit")],
            "jobs": [(th.g["enter"], "open"), ("c", "cancel"), ("r", "refresh"),
                     ("tab", "nav"), ("q", "quit")],
            "settings": [("tab", "field"), ("s", "save"), ("u", "check updates"),
                         ("q", "quit")],
            "wizard": [(th.g["enter"], "next"), ("esc", "back"), ("tab", "field")],
            "joblog": [("esc", "back"), ("c", "cancel"), ("pgup/pgdn", "scroll")],
        }.get(self.screen, [("q", "quit")])
        return [R.divider(th, cols), " " + R.help_bar(th, keys, cols - 2)]

    def sidebar(self, width: int, height: int) -> list[str]:
        th = self.th
        inner = width - 2
        rows = []
        for i, (key, label) in enumerate(NAV):
            sel = key == self.screen or (self.screen in ("wizard", "joblog")
                                         and key == NAV[self.nav_index][0])
            mark = th.g["arrow"] if sel else " "
            badge = ""
            if key == "jobs":
                active = sum(1 for j in self.live_jobs if j.state == jobmod.RUNNING)
                if active:
                    badge = th.paint(f" {active}", "warning")
            line = pad(f" {mark} {label}", inner - vlen(badge)) + badge
            if sel:
                rows.append(f"{th.bg('surface')}{th.fg('primary')}"
                            f"{pad(line, inner)}{RESET}")
            else:
                rows.append(th.paint(pad(line, inner), "muted"))
        rows.append("")
        return R.panel(th, rows, width, title="", height=height)

    def content(self, width: int, height: int) -> list[str]:
        fn = {
            "servers": self.screen_servers,
            "guests": self.screen_guests,
            "jobs": self.screen_jobs,
            "settings": self.screen_settings,
            "wizard": self.screen_wizard,
            "joblog": self.screen_joblog,
        }.get(self.screen, self.screen_servers)
        return fn(width, height)

    # -- screens -----------------------------------------------------
    def screen_servers(self, width: int, height: int) -> list[str]:
        th = self.th
        inner = width - 4
        if not config.servers(self.cfg):
            body = R.wrap("No Xen server configured yet. Press 'a' to add one - "
                          "xenproxY will check the connection before storing it.",
                          inner)
            return R.panel(th, [""] + [th.paint(l, "muted") for l in body],
                           width, title="Servers", focused=True, height=height)
        lst = self.server_list.render(th, inner, max(3, height - 8))
        detail: list[str] = []
        name = self.server_list.value
        srv = config.find_server(self.cfg, name) if name else None
        if srv:
            auth = "ssh key" if srv.get("key") else ("password" if srv.get("password")
                                                     else "none")
            detail = R.key_value(th, [
                ("Host", f"{srv.get('user', 'root')}@{srv.get('host')}:"
                         f"{srv.get('port', 22)}"),
                ("Auth", auth),
                ("TLS", "pinned " + srv["tls_pin"][:16] + "\u2026"
                        if srv.get("tls_pin")
                        else ("verified" if srv.get("verify_tls")
                              else "UNPINNED - re-add to pin")),
                ("Added", time.strftime("%Y-%m-%d",
                                        time.localtime(srv.get("added", time.time())))),
            ], inner)
        block = lst + ([""] + [R.divider(th, inner, "connection")] + detail
                       if detail else [])
        return R.panel(th, block, width, title="Servers",
                       subtitle=f"{len(config.servers(self.cfg))}",
                       focused=True, height=height)

    def screen_guests(self, width: int, height: int) -> list[str]:
        th = self.th
        if not self.active_server:
            return R.panel(th, [th.paint("  pick a server first", "muted")],
                           width, title="Guests", height=height)
        if self.load_task and not self.load_task.done:
            spin = R.spinner_frame(th, self.tick)
            return R.panel(th, ["", f"  {spin} " + th.paint(
                "reading the inventory from Xen", "muted")],
                width, title="Guests", focused=True, height=height)

        left_w = max(28, int(width * 0.46))
        right_w = width - left_w - 1
        inner_l = left_w - 4
        lst = self.guest_list.render(th, inner_l, max(3, height - 4))
        left = R.panel(th, lst, left_w, title="Guests",
                       subtitle=str(len(self.guests)), focused=True,
                       height=height)

        uuid = self.guest_list.value
        if uuid and (not self.guest_detail or self.guest_detail.get("uuid") != uuid):
            cached = self.detail_cache.get(uuid)
            if cached:
                self.guest_detail = cached
            elif not self.detail_task:
                self.load_detail(uuid)
        right = self.guest_detail_panel(right_w, height)
        return R.columns([left, right], [left_w, right_w], gap=1)

    def guest_detail_panel(self, width: int, height: int) -> list[str]:
        th = self.th
        inner = width - 4
        d = self.guest_detail
        if self.detail_task and not self.detail_task.done:
            return R.panel(th, ["", f"  {R.spinner_frame(th, self.tick)} " +
                                th.paint("loading details", "muted")],
                           width, title="Details", height=height)
        if not d:
            return R.panel(th, [th.paint("  select a guest", "muted")],
                           width, title="Details", height=height)
        state = d.get("power_state", "")
        tok = {"running": "success", "halted": "muted",
               "suspended": "warning"}.get(state, "muted")
        rows = [th.paint(d.get("name", ""), "foreground", bold=True),
                R.badge(th, state, tok, solid=state == "running"), ""]
        rows += R.key_value(th, [
            ("UUID", d.get("uuid", "")[:36]),
            ("vCPU", f"{d.get('vcpus')} ({d.get('cores_per_socket')} per socket)"),
            ("Memory", migrate.human(d.get("memory", 0))),
            ("Firmware", d.get("firmware", "")),
        ], inner)
        disks = d.get("disks", [])
        if disks:
            rows += ["", R.divider(th, inner, f"disks ({len(disks)})")]
            for dk in disks:
                rows.append(th.paint(
                    f" {dk['device']}  {migrate.human(dk['virtual_size'])}", "foreground"))
                rows.append(th.paint(f"   {dk.get('sr_name', '')}", "muted"))
        nics = d.get("nics", [])
        if nics:
            rows += ["", R.divider(th, inner, f"network ({len(nics)})")]
            for n in nics:
                vlan = f"  VLAN {n['vlan']}" if n.get("vlan") else ""
                rows.append(th.paint(f" {n['device']}  {n['mac']}{vlan}", "foreground"))
                rows.append(th.paint(f"   {n.get('network_name', '')}", "muted"))
        if d.get("snapshots"):
            rows += ["", R.status_message(
                th, f"{len(d['snapshots'])} snapshot(s) on the source", "warning")]
        return R.panel(th, rows, width, title="Details", height=height)

    # -- migration wizard --------------------------------------------
    def open_wizard(self) -> None:
        uuid = self.guest_list.value
        if not uuid or not self.xen:
            self.notify("select a guest first", "warning")
            return
        detail = self.detail_cache.get(uuid)
        if not detail:
            self.notify("details are still loading", "warning")
            self.load_detail(uuid)
            return
        if not (self.active_server or {}).get("password"):
            self.notify("this server has no stored password - the XAPI disk "
                        "export needs it (press 'e' on the server)", "danger", 9)
            return
        self.wizard = WizardState(self, detail)
        self.screen = "wizard"

    def screen_wizard(self, width: int, height: int) -> list[str]:
        return self.wizard.render(width, height) if self.wizard else []

    # -- jobs ---------------------------------------------------------
    def screen_jobs(self, width: int, height: int) -> list[str]:
        th = self.th
        inner = width - 4
        rows: list[str] = []
        running = [j for j in self.live_jobs if j.state == jobmod.RUNNING]
        if running:
            rows.append(th.paint("active", "primary", bold=True))
            for j in running:
                rows.append(th.paint(
                    f" {R.spinner_frame(th, self.tick)} {j.vm_name} "
                    f"{th.g['tri_right']} VMID {j.vmid}  [{j.mode}]", "foreground"))
                rows.append("  " + R.progress_bar(th, j.progress, inner - 2))
                detail = f"  {migrate.human(j.bytes_done)} / " \
                         f"{migrate.human(j.bytes_total)}"
                if j.rate:
                    detail += f"   {migrate.human(j.rate)}/s"
                if j.current_disk:
                    detail += f"   {j.current_disk}"
                rows.append(th.paint(detail, "muted"))
                rows.append("")

        history = jobmod.load_jobs(40)
        items = []
        for h in history:
            tok = {"done": th.g["check"], "failed": th.g["cross"],
                   "running": th.g["dot"]}.get(h.get("state"), th.g["ring"])
            label = f"{tok} {h.get('vm_name', '')}  {th.g['tri_right']} {h.get('vmid')}"
            hint = f"{h.get('mode', '')}  {h.get('state', '')}"
            items.append((h.get("id"), label, hint))
        self.job_list.set_items(items)
        if items:
            rows.append(R.divider(th, inner, "history"))
            rows += self.job_list.render(th, inner, max(3, height - len(rows) - 4))
        elif not running:
            rows = [th.paint("  no migrations yet", "muted")]
        return R.panel(th, rows, width, title="Jobs", focused=True, height=height)

    def screen_joblog(self, width: int, height: int) -> list[str]:
        th = self.th
        j = self.open_job
        inner = width - 4
        if not j:
            return R.panel(th, [th.paint("  no job selected", "muted")], width,
                           title="Job", height=height)
        head = [R.breadcrumb(th, ["Jobs", j.vm_name, j.id], inner), ""]
        for s in j.steps:
            icon = {"done": th.g["check"], "failed": th.g["cross"],
                    "running": R.spinner_frame(th, self.tick),
                    "skipped": th.g["ellipsis"]}.get(s.state, th.g["ring"])
            tok = {"done": "success", "failed": "danger", "running": "primary",
                   "skipped": "muted"}.get(s.state, "muted")
            extra = f"  {s.detail}" if s.detail else ""
            dur = f"  {s.duration:.0f}s" if s.started else ""
            head.append(th.paint(f" {icon} {s.title}{extra}{dur}", tok))
        if j.bytes_total:
            head += ["", "  " + R.progress_bar(th, j.progress, inner - 2)]
        head += ["", R.divider(th, inner, "log")]
        log_h = max(3, height - len(head) - 3)
        head += self.logview.render(th, j.log, inner, log_h)
        return R.panel(th, head, width, title=f"Job {j.id}",
                       subtitle=j.state, focused=True, height=height)

    # -- settings -----------------------------------------------------
    def build_settings_form(self) -> Form:
        s = self.cfg.get("settings", {})
        stores = [(x["name"], x["name"]) for x in pve.storages()] or [("", "none")]
        bridges = [(b["name"], b["name"]) for b in pve.bridges()] or [("", "none")]
        return Form([
            Field("theme", "Theme", Choice(
                [(k, k) for k in ("tokyo-night", "dracula", "nord", "gruvbox",
                                  "default", "high-contrast")],
                s.get("theme", "tokyo-night"))),
            Field("border", "Border", Choice(
                [(k, k) for k in ("rounded", "single", "double", "ascii")],
                s.get("border", "rounded"))),
            Field("default_storage", "Default storage", Choice(
                stores, s.get("default_storage", ""))),
            Field("default_bridge", "Default bridge", Choice(
                bridges, s.get("default_bridge", ""))),
            Field("verify_transfers", "Verify transfers",
                  Toggle(s.get("verify_transfers", True)),
                  help="Read every written volume back and compare it with the "
                       "checksum of the received stream."),
            Field("guest_prep", "Adapt guests", Toggle(s.get("guest_prep", True)),
                  help="Pin net.ifnames=0 so a Xen eth0 keeps its name under KVM."),
            Field("check_updates", "Check for updates",
                  Toggle(s.get("check_updates", True))),
        ])

    def screen_settings(self, width: int, height: int) -> list[str]:
        th = self.th
        if self.settings_form is None:
            self.settings_form = self.build_settings_form()
        inner = width - 4
        rows = self.settings_form.render(th, inner)
        rows += ["", R.divider(th, inner, "about")]
        rows += R.key_value(th, [
            ("Version", __version__),
            ("Node", f"{pve.node_name()} - {pve.version()}"),
            ("Config", config.CONF_FILE),
            ("Jobs", jobmod.JOB_DIR),
        ], inner)
        if self.update_info:
            rows += ["", R.status_message(
                th, f"version {self.update_info['version']} is available - press 'u'",
                "warning")]
        return R.panel(th, rows, width, title="Settings", focused=True, height=height)

    def save_settings(self) -> None:
        if not self.settings_form:
            return
        for k, v in self.settings_form.values().items():
            config.set_setting(self.cfg, k, v)
        config.save(self.cfg)
        self.th = Theme(config.setting(self.cfg, "theme"),
                        config.setting(self.cfg, "border"))
        self.term.invalidate()
        self.notify("settings saved", "success")

    # -- overlays ------------------------------------------------------
    def open_server_modal(self, first_run: bool = False, edit: dict | None = None) -> None:
        e = edit or {}
        form = Form([
            Field("name", "Name", TextField(e.get("name", ""),
                                            placeholder="xen-node-1")),
            Field("host", "Host", TextField(e.get("host", ""),
                                            placeholder="192.168.0.10")),
            Field("user", "User", TextField(e.get("user", "root"))),
            Field("password", "Password", TextField(e.get("password", ""), masked=True),
                  help="Needed for the XAPI disk export, even when SSH uses a key."),
            Field("port", "SSH port", TextField(str(e.get("port", 22)), numeric=True)),
            Field("verify_tls", "Verify TLS", Toggle(e.get("verify_tls", False)),
                  help="XCP-ng ships a self-signed certificate; leave this off "
                       "and xenproxY pins its fingerprint over SSH instead."),
            Field("insecure_tls", "Allow unverified",
                  Toggle(e.get("insecure_tls", False)),
                  help="Only if the fingerprint cannot be pinned. The host "
                       "password then travels over an unverified channel."),
        ])
        self.overlay = {"kind": "server", "form": form, "first_run": first_run,
                        "edit": bool(edit), "testing": None, "tested": False}

    def open_update_modal(self) -> None:
        self.overlay = {"kind": "update", "info": self.update_info}

    def open_confirm(self, text: str, on_yes, danger: bool = False) -> None:
        self.overlay = {"kind": "confirm", "text": text, "on_yes": on_yes,
                        "danger": danger, "choice": False}

    def draw_overlay(self, frame: list[str], cols: int, rows: int) -> list[str]:
        th = self.th
        ov = self.overlay
        w = min(74, cols - 6)
        inner = w - 4
        if ov["kind"] == "server":
            title = "Add Xen server" if not ov["edit"] else "Edit Xen server"
            body = []
            if ov.get("first_run"):
                body += [th.paint("Welcome to xenproxY.", "primary", bold=True),
                         th.paint("Add the XenServer/XCP-ng host you want to "
                                  "migrate from.", "muted"), ""]
            if ov.get("host_key"):
                body += [th.paint("This host is not known yet.", "warning",
                                  bold=True),
                         th.paint("Compare the fingerprint with the Xen host "
                                  "before accepting - nothing has been sent "
                                  "yet.", "muted"), ""]
                for key in ov["host_key"]:
                    body.append(th.paint(
                        f"  {key['type']:<8} {key['bits']:>5} bit  "
                        f"{key['fingerprint']}", "foreground"))
                body += ["", R.help_bar(th, [("y", "accept and continue"),
                                             ("n", "cancel")], inner)]
                box = R.panel(th, body, w, title="Confirm the SSH host key",
                              focused=True, token="warning")
                top = max(0, (rows - len(box)) // 2)
                left = max(0, (cols - w) // 2)
                out = list(frame)
                while len(out) < rows:
                    out.append("")
                for i, line in enumerate(box):
                    yy = top + i
                    if 0 <= yy < rows:
                        out[yy] = pad(truncate(out[yy], left, ""), left) + line
                return out
            body += ov["form"].render(th, inner)
            body += [""]
            t = ov.get("testing")
            if t and not t.done:
                body.append(f" {R.spinner_frame(th, self.tick)} " +
                            th.paint("checking the connection", "muted"))
            elif t and t.done and t.error:
                body.append(R.status_message(th, t.error, "danger"))
            elif ov.get("tested"):
                info = ov["testing"].result if ov.get("testing") else {}
                pin = info.get("tls_pin", "")
                body.append(R.status_message(
                    th, f"{info.get('hostname', '')} - {info.get('version', '')}",
                    "success"))
                body.append(R.status_message(
                    th, f"XAPI certificate pinned {pin[:16]}\u2026" if pin
                    else "XAPI certificate could not be pinned",
                    "success" if pin else "warning"))
            body += ["", R.help_bar(th, [("tab", "field"), ("ctrl-t", "test"),
                                         (th.g["enter"], "save"), ("esc", "cancel")],
                                    inner)]
            box = R.panel(th, body, w, title=title, focused=True)
        elif ov["kind"] == "update":
            info = ov["info"] or {}
            body = [th.paint(f"xenproxY {info.get('version')} is available.",
                             "foreground", bold=True),
                    th.paint(f"You are running {__version__}.", "muted"), ""]
            if info.get("notes"):
                body += [R.divider(th, inner, "release notes")]
                body += [th.paint(l, "muted") for l in
                         R.wrap(info["notes"], inner)[:8]]
                body += [""]
            body += [th.paint("Update now? The installer replaces /opt/xenproxy "
                              "and keeps your configuration.", "foreground"), "",
                     R.help_bar(th, [("y", "update"), ("n", "later"),
                                     ("esc", "close")], inner)]
            box = R.panel(th, body, w, title="Update available",
                          focused=True, token="warning")
        else:
            body = [""] + [th.paint(l, "foreground") for l in
                           R.wrap(ov["text"], inner)] + [""]
            yes = "  Yes  "
            no = "  No  "
            if ov["choice"]:
                row = (f"{th.bg('danger' if ov['danger'] else 'primary')}"
                       f"{th.fg('surface')}{yes}{RESET}" + th.paint(no, "muted"))
            else:
                row = th.paint(yes, "muted") + (
                    f"{th.bg('primary')}{th.fg('surface')}{no}{RESET}")
            body += [row, "", R.help_bar(th, [(th.g["arrow"], "switch"),
                                              (th.g["enter"], "confirm"),
                                              ("esc", "cancel")], inner)]
            box = R.panel(th, body, w, title="Confirm", focused=True,
                          token="danger" if ov["danger"] else "primary")

        top = max(0, (rows - len(box)) // 2)
        left = max(0, (cols - w) // 2)
        out = list(frame)
        while len(out) < rows:
            out.append("")
        for i, line in enumerate(box):
            y = top + i
            if 0 <= y < rows:
                # hard cut - truncate() would leave an ellipsis column
                prefix = pad(truncate(out[y], left, ""), left)
                out[y] = prefix + line
        return out

    # -- key handling --------------------------------------------------
    def handle(self, k: str) -> None:
        if self.overlay:
            return self.handle_overlay(k)
        if self.screen == "wizard" and self.wizard:
            return self.wizard.key(k)
        if self.screen == "joblog":
            return self.handle_joblog(k)

        if k in ("q", "ctrl-c"):
            self.running = False
            return
        if k == "tab":
            self.nav_index = (self.nav_index + 1) % len(NAV)
            self.screen = NAV[self.nav_index][0]
            return
        if k == "shift-tab":
            self.nav_index = (self.nav_index - 1) % len(NAV)
            self.screen = NAV[self.nav_index][0]
            return
        if k in ("1", "2", "3", "4"):
            self.nav_index = int(k) - 1
            self.screen = NAV[self.nav_index][0]
            return

        handler = {
            "servers": self.handle_servers,
            "guests": self.handle_guests,
            "jobs": self.handle_jobs,
            "settings": self.handle_settings,
        }.get(self.screen)
        if handler:
            handler(k)

    def handle_servers(self, k: str) -> None:
        if self.server_list.key(k):
            return
        name = self.server_list.value
        if k == "a":
            self.open_server_modal()
        elif k == "e" and name:
            self.open_server_modal(edit=config.find_server(self.cfg, name))
        elif k == "t" and name:
            srv = config.find_server(self.cfg, name)
            self.notify("testing the connection", "info", 2)
            self.probe_task = Task(self.connect(srv).probe)
        elif k == "d" and name:
            self.open_confirm(
                f"Remove the server '{name}' from the configuration? "
                "Nothing on the Xen host is touched.",
                lambda: self._delete_server(name), danger=True)
        elif k == "enter" and name:
            self.select_server(name)

    def _delete_server(self, name: str) -> None:
        config.remove_server(self.cfg, name)
        config.save(self.cfg)
        self.refresh_servers()
        if self.active_server and self.active_server.get("name") == name:
            self.active_server = None
            self.xen = None
            self.guests = []
        self.notify(f"removed {name}", "success")

    def handle_guests(self, k: str) -> None:
        if self.guest_list.key(k):
            self.guest_detail = self.detail_cache.get(self.guest_list.value)
            return
        if k == "r":
            self.detail_cache = {}
            self.load_guests()
        elif k == "enter":
            self.load_detail(self.guest_list.value)
        elif k == "m":
            self.open_wizard()
        elif k == "esc":
            self.screen = "servers"
            self.nav_index = 0

    def handle_jobs(self, k: str) -> None:
        if self.job_list.key(k):
            return
        if k == "r":
            return
        if k == "enter":
            jid = self.job_list.value
            for j in self.live_jobs:
                if j.id == jid:
                    self.open_job = j
                    self.screen = "joblog"
                    self.logview = LogView()
                    return
            self.notify("only running jobs keep a live log", "muted", 3)
        elif k == "c":
            jid = self.job_list.value
            for j in self.live_jobs:
                if j.id == jid and j.state == jobmod.RUNNING:
                    self.open_confirm(
                        f"Cancel the migration of {j.vm_name}? Volumes already "
                        "written on Proxmox are removed.", j.cancel, danger=True)
                    return

    def handle_joblog(self, k: str) -> None:
        j = self.open_job
        if k == "esc":
            self.screen = "jobs"
            self.nav_index = 2
            return
        if k == "c" and j and j.state == jobmod.RUNNING:
            self.open_confirm(f"Cancel the migration of {j.vm_name}?",
                              j.cancel, danger=True)
            return
        if j:
            self.logview.key(k, len(j.log), 10)

    def handle_settings(self, k: str) -> None:
        if k == "s":
            self.save_settings()
            return
        if k == "u":
            self.notify("checking for updates", "info", 2)
            self.update_task = Task(update.check)
            return
        if self.settings_form:
            self.settings_form.key(k)

    def handle_overlay(self, k: str) -> None:
        ov = self.overlay
        if ov["kind"] == "server":
            if ov.get("host_key"):
                if k in ("y", "Y"):
                    trust_host_keys(ov.pop("host_key"))
                    self.notify("host key stored", "success")
                    self._test_overlay_server()
                elif k in ("n", "N", "esc"):
                    ov.pop("host_key", None)
                    ov.pop("pending_save", None)
                    self.notify("host key not stored - nothing was sent",
                                "warning", 6)
                return
            if k == "esc":
                if ov.get("first_run") and not config.servers(self.cfg):
                    self.running = False
                self.overlay = None
                return
            if k == "ctrl-t":
                self._test_overlay_server()
                return
            if k == "enter":
                self._save_overlay_server()
                return
            t = ov.get("testing")
            if t and t.done:
                ov["testing"] = None
            ov["form"].key(k)
        elif ov["kind"] == "update":
            if k in ("y", "Y"):
                self.overlay = None
                self.do_update()
            elif k in ("n", "N", "esc"):
                self.overlay = None
        else:
            if k in ("left", "right", "h", "l", "tab"):
                ov["choice"] = not ov["choice"]
            elif k == "enter":
                fn = ov["on_yes"]
                chosen = ov["choice"]
                self.overlay = None
                if chosen:
                    fn()
            elif k in ("esc", "n"):
                self.overlay = None
            elif k == "y":
                fn = ov["on_yes"]
                self.overlay = None
                fn()

    def _overlay_server_dict(self) -> dict:
        v = self.overlay["form"].values()
        return {
            "name": (v.get("name") or "").strip(),
            "host": (v.get("host") or "").strip(),
            "user": (v.get("user") or "root").strip(),
            "password": v.get("password") or "",
            "port": int(v.get("port") or 22),
            "verify_tls": bool(v.get("verify_tls")),
            "insecure_tls": bool(v.get("insecure_tls")),
            "added": time.time(),
        }

    def _test_overlay_server(self) -> None:
        srv = self._overlay_server_dict()
        if not srv["host"]:
            self.notify("a host is required", "warning")
            return
        self.overlay["tested"] = False
        self.overlay["testing"] = Task(self._probe_and_pin, srv)

    def _probe_and_pin(self, srv: dict) -> dict:
        """Confirm the host key first, then probe and pin.

        Nothing is sent until the operator has accepted the SSH host key,
        because that session carries the password and produces the TLS
        fingerprint everything else relies on.
        """
        if not host_key_known(srv["host"], int(srv.get("port", 22))):
            return {"needs_host_key": True,
                    "keys": scan_host_keys(srv["host"], int(srv.get("port", 22)))}
        xen = self.connect(srv)
        info = xen.probe()
        try:
            info["tls_pin"] = xen.fetch_tls_pin()
        except SSHError:
            info["tls_pin"] = ""
        return info

    def _save_overlay_server(self) -> None:
        ov = self.overlay
        srv = self._overlay_server_dict()
        if not srv["name"] or not srv["host"]:
            self.notify("name and host are required", "warning")
            return
        t = ov.get("testing")
        if not (t and t.done and not t.error):
            self.notify("checking the connection before saving", "info", 3)
            ov["testing"] = Task(self._probe_and_pin, srv)
            ov["pending_save"] = srv
            return
        srv["tls_pin"] = (t.result or {}).get("tls_pin", "")
        if not srv["tls_pin"]:
            self.notify("saved, but the XAPI certificate could not be pinned",
                        "warning", 7)
        config.upsert_server(self.cfg, srv)
        config.save(self.cfg)
        self.refresh_servers()
        self.overlay = None
        self.notify(f"{srv['name']} saved", "success")

    def poll_overlay(self) -> None:
        ov = self.overlay
        if not ov or ov.get("kind") != "server":
            return
        t = ov.get("testing")
        if t and t.done:
            if t.error:
                ov.pop("pending_save", None)
                ov["tested"] = False
            elif (t.result or {}).get("needs_host_key"):
                ov["host_key"] = t.result["keys"]
                ov["tested"] = False
                ov["testing"] = None
            else:
                ov["tested"] = True
                if ov.pop("pending_save", None):
                    self._save_overlay_server()

    def do_update(self) -> None:
        import subprocess
        self.notify("running the installer", "info", 10)
        cmd = update.install_command()
        try:
            cp = subprocess.run(["bash", "-lc", cmd], capture_output=True,
                                text=True, timeout=600)
            if cp.returncode == 0:
                self.notify("updated - restart xenproxY", "success", 10)
                self.running = False
            else:
                self.notify((cp.stderr or cp.stdout).strip()[:160], "danger", 10)
        except Exception as exc:  # noqa: BLE001
            self.notify(str(exc), "danger", 10)


class WizardState:
    """Six-step migration wizard."""

    def __init__(self, app: App, detail: dict) -> None:
        self.app = app
        self.detail = detail
        self.page = 0
        cfg = app.cfg
        plan = migrate.default_plan(detail, (app.active_server or {}).get("name", ""))
        plan["storage"] = config.setting(cfg, "default_storage", "") or plan["storage"]
        plan["bridge"] = config.setting(cfg, "default_bridge", "") or ""
        plan["verify"] = config.setting(cfg, "verify_transfers", True)
        plan["guest_prep"] = config.setting(cfg, "guest_prep", True)
        self.plan = plan

        stores = pve.storages()
        store_opts = [(s["name"], f"{s['name']} ({migrate.human(s['avail'])} free)")
                      for s in stores] or [("", "none available")]
        if plan["storage"] not in [s[0] for s in store_opts]:
            plan["storage"] = store_opts[0][0]
        bridges = pve.bridges()
        bridge_opts = [(b["name"], b["name"]) for b in bridges] or [("", "none")]
        if plan["bridge"] not in [b[0] for b in bridge_opts]:
            plan["bridge"] = bridge_opts[0][0]

        running = detail.get("power_state") == "running"
        self.pages: list[tuple[str, Form]] = [
            ("Mode", Form([
                Field("mode", "Copy mode", Choice(
                    [("offline", "Offline"), ("snapshot", "Snapshot"), ("live", "Live")],
                    plan["mode"]),
                    help=migrate.MODES[plan["mode"]]),
                Field("source_restart", "Restart source afterwards",
                      Toggle(False),
                      help="Only sensible if you are testing - the migrated guest "
                           "and the source share MAC and IP.",
                      visible=lambda v: v.get("mode") == "offline"),
            ])),
            ("Target", Form([
                Field("target_vmid", "VMID", TextField(str(plan["target_vmid"]),
                                                       numeric=True)),
                Field("target_name", "Name", TextField(plan["target_name"])),
                Field("storage", "Storage", Choice(store_opts, plan["storage"]),
                      help="Where the disks are allocated."),
            ])),
            ("Network", Form([
                Field("net_mode", "Network", Choice(
                    [("keep", "Keep"), ("new", "New"), ("later", "Later"),
                     ("skip", "None")], plan["net_mode"]),
                    help=migrate.NET_MODES.get(plan["net_mode"], "No NIC at all.")),
                Field("bridge", "Bridge", Choice(bridge_opts, plan["bridge"]),
                      visible=lambda v: v.get("net_mode") != "skip"),
                Field("vlan", "VLAN tag", TextField(str(plan["vlan"] or ""),
                                                    numeric=True),
                      help="Empty means untagged.",
                      visible=lambda v: v.get("net_mode") != "skip"),
                Field("mac", "MAC", TextField(plan["mac"]),
                      visible=lambda v: v.get("net_mode") == "keep"),
            ])),
            ("Hardware", Form([
                Field("cores", "Cores", TextField(str(plan["cores"]), numeric=True)),
                Field("sockets", "Sockets", TextField(str(plan["sockets"]),
                                                      numeric=True)),
                Field("memory", "Memory (MiB)", TextField(str(plan["memory"]),
                                                          numeric=True)),
                Field("bios", "Firmware", Choice(
                    [("seabios", "SeaBIOS"), ("ovmf", "UEFI")], plan["bios"])),
                Field("machine", "Machine", Choice(
                    [("pc", "i440fx"), ("q35", "q35")], plan["machine"])),
                Field("cpu", "CPU type", Choice(
                    [("host", "host"), ("x86-64-v2-AES", "x86-64-v2-AES"),
                     ("kvm64", "kvm64")], plan["cpu"])),
            ])),
            ("Options", Form([
                Field("start_after", "Start after migration",
                      Toggle(plan["start_after"])),
                Field("guest_prep", "Adapt guest for KVM", Toggle(plan["guest_prep"]),
                      help="Pins net.ifnames=0 so a Xen eth0 keeps its name."),
                Field("verify", "Verify written volumes", Toggle(plan["verify"]),
                      help="Reads each volume back and compares checksums. "
                           "Roughly doubles the disk read time."),
                Field("onboot", "Start on host boot", Toggle(plan["onboot"])),
            ])),
            ("Review", Form([])),
        ]
        if running and plan["mode"] == "offline":
            pass

    # -- plan ---------------------------------------------------------
    def collect(self) -> dict:
        p = dict(self.plan)
        for _title, form in self.pages:
            for k, v in form.values().items():
                p[k] = v
        for key in ("target_vmid", "cores", "sockets", "memory"):
            try:
                p[key] = int(str(p.get(key) or 0))
            except ValueError:
                p[key] = 0
        p["vlan"] = int(p["vlan"]) if str(p.get("vlan") or "").isdigit() else None
        p["vm_uuid"] = self.detail["uuid"]
        p["vm_name"] = self.detail["name"]
        return p

    def summary(self) -> list[tuple[str, str]]:
        p = self.collect()
        total = sum(d["virtual_size"] for d in self.detail.get("disks", []))
        net = {"keep": f"{p['mac']} on {p['bridge']}",
               "new": f"new MAC on {p['bridge']}",
               "later": f"{p['bridge']}, link down",
               "skip": "no NIC"}.get(p["net_mode"], "")
        if p.get("vlan") and p["net_mode"] != "skip":
            net += f", VLAN {p['vlan']}"
        return [
            ("Source", f"{self.detail['name']}  ({self.detail.get('power_state')})"),
            ("Mode", f"{p['mode']} - {migrate.MODES[p['mode']]}"),
            ("Disks", f"{len(self.detail.get('disks', []))}  "
                      f"{migrate.human(total)}  {th_arrow()} {p['storage']}"),
            ("Target", f"VMID {p['target_vmid']}  {p['target_name']}"),
            ("Hardware", f"{p['sockets']}x{p['cores']} vCPU, {p['memory']} MiB, "
                         f"{p['bios']}/{p['machine']}, cpu={p['cpu']}"),
            ("Network", net),
            ("After", ("start, " if p["start_after"] else "")
                      + ("adapt guest, " if p["guest_prep"] else "")
                      + ("verify" if p["verify"] else "no verify")),
        ]

    # -- render ---------------------------------------------------------
    def render(self, width: int, height: int) -> list[str]:
        th = self.app.th
        inner = width - 4
        titles = [t for t, _f in self.pages]
        crumbs = []
        for i, t in enumerate(titles):
            if i < self.page:
                crumbs.append(th.paint(f"{th.g['check']} {t}", "success"))
            elif i == self.page:
                crumbs.append(th.paint(f"{th.g['arrow']} {t}", "primary", bold=True))
            else:
                crumbs.append(th.paint(t, "muted"))
        sep = th.paint(f"  {th.g['sep']}  ", "border")
        rows = [truncate(sep.join(crumbs), inner), ""]

        title, form = self.pages[self.page]
        if title == "Review":
            rows += R.key_value(th, self.summary(), inner, key_width=12)
            rows += ["", R.divider(th, inner)]
            warn = self._warnings()
            for w in warn:
                rows.append(R.status_message(th, w, "warning"))
            if warn:
                rows.append("")
            rows.append(th.paint(f"{th.g['enter']} starts the migration",
                                 "success", bold=True))
        else:
            rows += form.render(th, inner)
        rows += ["", R.help_bar(th, [
            (th.g["enter"], "start" if title == "Review" else "next"),
            ("esc", "back" if self.page else "cancel"),
            ("tab", "field")], inner)]
        return R.panel(th, rows, width,
                       title=f"Migrate {self.detail['name']}",
                       subtitle=f"step {self.page + 1}/{len(self.pages)}",
                       focused=True, height=height)

    def _warnings(self) -> list[str]:
        p = self.collect()
        out = []
        if p["mode"] == "live":
            out.append("live mode produces a crash-consistent image - "
                       "the guest replays its journal on first boot")
        if p["mode"] == "snapshot":
            out.append("snapshot mode creates a temporary snapshot on Xen "
                       "and removes it afterwards")
        if p["mode"] == "offline" and self.detail.get("power_state") == "running":
            out.append("the source guest will be shut down")
        if p["net_mode"] == "keep" and p["start_after"] and \
                self.detail.get("power_state") == "running" and p["mode"] != "offline":
            out.append("source and target would run with the same MAC and IP")
        if not pve.vmid_free(int(p["target_vmid"] or 0)):
            out.append(f"VMID {p['target_vmid']} is already taken")
        if self.detail.get("snapshots"):
            out.append("the source has snapshots; XAPI exports the resolved image")
        return out

    # -- keys -----------------------------------------------------------
    def key(self, k: str) -> None:
        title, form = self.pages[self.page]
        if k == "esc":
            if self.page == 0:
                self.app.screen = "guests"
                self.app.wizard = None
            else:
                self.page -= 1
            return
        if k == "enter":
            if title == "Review":
                self.start()
            else:
                self.page = min(len(self.pages) - 1, self.page + 1)
                self._sync_help()
            return
        if k in ("ctrl-n",):
            self.page = min(len(self.pages) - 1, self.page + 1)
            return
        form.key(k)
        self._sync_help()

    def _sync_help(self) -> None:
        for title, form in self.pages:
            for f in form.fields:
                if f.key == "mode":
                    f.help = migrate.MODES.get(f.widget.value, "")
                if f.key == "net_mode":
                    f.help = migrate.NET_MODES.get(f.widget.value, "No NIC at all.")

    def start(self) -> None:
        plan = self.collect()
        app = self.app
        job = jobmod.Job(plan)
        mig = migrate.Migration(job, app.xen, plan)
        app.live_jobs.insert(0, job)
        app.open_job = job
        app.logview = LogView()
        app.screen = "joblog"
        app.wizard = None
        threading.Thread(target=mig.run, daemon=True).start()
        app.notify(f"migration of {plan['vm_name']} started", "success")


def th_arrow() -> str:
    return "→"
