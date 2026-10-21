"""Interactive application shell.

Three ideas hold the navigation together, and the footer always names the
one that applies:

  * tab moves focus between the panes of a screen - menu, content, actions
  * the arrow keys move inside whatever has focus
  * enter runs the highlighted action, which is always drawn as a button

Every one of those targets is also a click target, so the pointer and the
keyboard never disagree about what is where.
"""
from __future__ import annotations

import threading
import time

from .. import (__repo__, __version__, config, jobs as jobmod, migrate,
                 pve, update)
from ..incus import IncusHost
from ..sshclient import (SSHClient, SSHError, host_key_known,
                         pick_host_key, scan_host_keys, trust_host_keys)
from ..text import scrub
from ..theme import RESET, Theme
from ..xen import XenHost
from . import render as R
from .hit import HitMap
from .term import Mouse, Term, pad, slice_cols, truncate, vlen
from .widgets import (Button, ButtonBar, Choice, Field, Form, LogView,
                      SelectList, TextField, Toggle)

NAV = [("servers", "Servers"), ("guests", "Guests"),
       ("jobs", "Jobs"), ("settings", "Settings")]

# Which panes tab walks through, per screen. The menu is dropped on narrow
# terminals, where it is not drawn at all.
RING = {
    "servers": ["nav", "list", "actions"],
    "guests": ["nav", "list", "actions"],
    "jobs": ["nav", "list", "actions"],
    "settings": ["nav", "form", "actions"],
    "wizard": ["form", "actions"],
    "joblog": ["nav", "log", "actions"],
}

SIDE_W = 20


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
        self.focus = "list"
        self.toast = ""
        self.toast_kind = "info"
        self.toast_until = 0.0

        self.hit = HitMap()
        self.hover_action = ""
        self.hover_arg = None
        self.wide = True

        self.server_list = SelectList(pane="servers")
        self.guest_list = SelectList(pane="guests")
        self.job_list = SelectList(pane="jobs")
        self.settings_form: Form | None = None
        self.logview = LogView()
        self.bar = ButtonBar()

        self.active_server: dict | None = None
        self.src: XenHost | None = None
        self.guests: list[dict] = []
        self.guest_detail: dict | None = None
        self.detail_cache: dict = {}

        self.load_task: Task | None = None
        self.detail_task: Task | None = None
        self.probe_task: Task | None = None
        self.update_task: Task | None = None
        self.update_info: dict | None = None
        self.update_state: dict | None = None
        self.update_asked = False

        self.overlay = None      # dict describing a modal
        self.wizard = None       # WizardState
        self.live_jobs: list[jobmod.Job] = []
        self.open_job: jobmod.Job | None = None

        self.refresh_servers()
        if config.setting(self.cfg, "check_updates", True):
            # the check on start stays quiet unless there is news; being told
            # at every launch that GitHub was unreachable helps nobody
            self.update_task = Task(update.status)

    # -- helpers -----------------------------------------------------
    def notify(self, text: str, kind: str = "info", secs: float = 4.0) -> None:
        self.toast = scrub(text)
        self.toast_kind = kind
        self.toast_until = time.time() + secs

    def refresh_servers(self) -> None:
        items = []
        for s in config.servers(self.cfg):
            hint = f"{s.get('kind', 'xen')}  {s.get('host', '')}"
            items.append((s["name"], s["name"], hint))
        self.server_list.set_items(items)

    def connect(self, server: dict):
        """Build the adapter for the kind of source this entry describes."""
        ssh = SSHClient(server["host"], server.get("user", "root"),
                        server.get("password") or None,
                        server.get("key") or None,
                        int(server.get("port", 22)))
        if server.get("kind", "xen") == "incus":
            return IncusHost(ssh)
        return XenHost(ssh, verify_tls=bool(server.get("verify_tls")),
                       tls_pin=server.get("tls_pin", ""),
                       insecure_tls=bool(server.get("insecure_tls")))

    def select_server(self, name: str) -> None:
        server = config.find_server(self.cfg, name)
        if not server:
            return
        self.active_server = server
        self.src = self.connect(server)
        self.guests = []
        self.guest_detail = None
        self.detail_cache = {}
        self.goto("guests")
        self.load_guests()

    def load_guests(self) -> None:
        if not self.src:
            return
        self.load_task = Task(self._load_guests_inner)

    def _load_guests_inner(self) -> list[dict]:
        self.src.probe()
        return self.src.list_vms()

    def source_kind(self) -> str:
        return (self.active_server or {}).get("kind", "xen")

    def load_detail(self, uuid: str) -> None:
        if not self.src or uuid in self.detail_cache:
            self.guest_detail = self.detail_cache.get(uuid)
            return
        self.detail_task = Task(self.src.vm_detail, uuid)

    # -- focus --------------------------------------------------------
    def ring(self) -> list[str]:
        panes = list(RING.get(self.screen, ["list", "actions"]))
        if not self.wide and "nav" in panes:
            panes.remove("nav")
        if self.screen == "wizard" and self.wizard and \
                not self.wizard.form().active():
            panes = [p for p in panes if p != "form"]
        return panes or ["actions"]

    def focus_next(self, delta: int) -> None:
        panes = self.ring()
        try:
            i = panes.index(self.focus)
        except ValueError:
            i = 0
        self.focus = panes[(i + delta) % len(panes)]

    def goto(self, screen: str) -> None:
        self.screen = screen
        for i, (key, _label) in enumerate(NAV):
            if key == screen:
                self.nav_index = i
        # land on the content, not on the menu we just came from - and not
        # on the action bar either when the menu is hidden on a narrow screen
        panes = [p for p in self.ring() if p != "nav"]
        self.focus = panes[0] if panes else "actions"

    # -- main loop ---------------------------------------------------
    def run(self) -> int:
        if not config.servers(self.cfg):
            self.open_server_modal(first_run=True)
        with Term(mouse=bool(config.setting(self.cfg, "mouse", True))) as term:
            self.term = term
            while self.running:
                self.poll()
                cols, rows = term.size()
                term.draw(self.compose(cols, rows))
                ev = term.read_key(timeout=0.15)
                self.tick += 1
                if ev is not None:
                    try:
                        if isinstance(ev, Mouse):
                            self.on_mouse(ev)
                        else:
                            self.handle(ev)
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
                self.notify(f"connection failed: {self.probe_task.error}",
                            "danger", 8)
            else:
                info = self.probe_task.result or {}
                self.notify(f"reachable: {info.get('hostname', '')} "
                            f"({info.get('version', '')})", "success", 6)
            self.probe_task = None
        if self.update_task and self.update_task.done:
            st = self.update_task.result
            if self.update_task.error:
                st = {"state": update.UNREACHABLE,
                      "error": self.update_task.error, "info": None}
            self.update_state = st or {}
            self.update_info = (self.update_state.get("info")
                                if self.update_state.get("state") == update.NEWER
                                else None)
            text, kind = update.describe(self.update_state)
            if self.update_info:
                self.open_update_modal()
            elif self.update_asked:
                # an explicit check always says what it found - silence is
                # indistinguishable from a check that never ran
                self.notify(text, kind, 8)
            self.update_asked = False
            self.update_task = None

    # -- actions ------------------------------------------------------
    def buttons(self) -> list[Button]:
        """The action bar for the current screen.

        Each entry is both the keyboard shortcut and the click target, which
        is why the bar doubles as the screen's documentation.
        """
        if self.screen == "servers":
            has = bool(self.server_list.value)
            return [
                Button("srv.add", "Add server", "a"),
                Button("srv.open", "Open", "enter", enabled=has),
                Button("srv.test", "Test", "t", enabled=has),
                Button("srv.edit", "Edit", "e", enabled=has),
                Button("srv.remove", "Remove", "d", enabled=has),
            ]
        if self.screen == "guests":
            has = bool(self.guest_list.value)
            return [
                Button("gst.migrate", "Migrate", "m", enabled=has),
                Button("gst.refresh", "Reload", "r"),
                Button("gst.back", "Servers", "esc"),
            ]
        if self.screen == "jobs":
            jid = self.job_list.value
            live = any(j.id == jid for j in self.live_jobs)
            return [
                Button("job.open", "Open log", "enter", enabled=live),
                Button("job.cancel", "Cancel", "c", enabled=live),
            ]
        if self.screen == "settings":
            return [
                Button("set.save", "Save", "s"),
                Button("set.update", "Check for updates", "u"),
                Button("set.reinstall", "Reinstall", "i"),
            ]
        if self.screen == "joblog":
            j = self.open_job
            return [
                Button("log.back", "Back to jobs", "esc"),
                Button("log.cancel", "Cancel job", "c",
                       enabled=bool(j and j.state == jobmod.RUNNING)),
            ]
        if self.screen == "wizard" and self.wizard:
            w = self.wizard
            last = w.page == len(w.pages) - 1
            return [
                Button("wiz.back", "Back" if w.page else "Cancel", "esc"),
                Button("wiz.start" if last else "wiz.next",
                       "Start migration" if last else "Next", "enter",
                       token="success" if last else "primary"),
            ]
        return []

    def run_action(self, action: str) -> None:
        srv_name = self.server_list.value
        if action == "srv.add":
            self.open_server_modal()
        elif action == "srv.edit" and srv_name:
            self.open_server_modal(edit=config.find_server(self.cfg, srv_name))
        elif action == "srv.test" and srv_name:
            self.notify("testing the connection", "info", 2)
            self.probe_task = Task(
                self.connect(config.find_server(self.cfg, srv_name)).probe)
        elif action == "srv.remove" and srv_name:
            self.open_confirm(
                f"Remove the server '{srv_name}' from the configuration? "
                "Nothing on the Xen host is touched.",
                lambda: self._delete_server(srv_name), danger=True,
                yes="Remove")
        elif action == "srv.open" and srv_name:
            self.select_server(srv_name)
        elif action == "gst.migrate":
            self.open_wizard()
        elif action == "gst.refresh":
            self.detail_cache = {}
            self.load_guests()
        elif action == "gst.back":
            self.goto("servers")
        elif action == "job.open":
            self._open_job_log()
        elif action == "job.cancel":
            self._cancel_job(self.job_list.value)
        elif action == "set.save":
            self.save_settings()
        elif action == "set.update":
            self.notify("checking for updates", "info", 3)
            self.update_asked = True
            self.update_task = Task(update.status)
        elif action == "set.reinstall":
            self.open_confirm(
                "Re-run the installer? It fetches the current main branch "
                f"from {__repo__}, replaces /opt/xenproxy and leaves "
                "/etc/xenproxy alone. xenproxY closes afterwards so the new "
                "version starts fresh.", self.do_update, yes="Reinstall")
        elif action == "log.back":
            self.goto("jobs")
        elif action == "log.cancel" and self.open_job:
            self._cancel_job(self.open_job.id)
        elif action in ("wiz.back", "wiz.next", "wiz.start") and self.wizard:
            self.wizard.action(action)
        elif action == "app.help":
            self.open_help()
        elif action == "app.mouse":
            self.toggle_mouse()
        elif action == "app.quit":
            self.running = False

    def primary_for(self, pane: str) -> str:
        return {"servers": "srv.open", "guests": "gst.migrate",
                "jobs": "job.open"}.get(pane, "")

    def _open_job_log(self) -> None:
        jid = self.job_list.value
        for j in self.live_jobs:
            if j.id == jid:
                self.open_job = j
                self.logview = LogView()
                self.goto("joblog")
                self.focus = "log"
                return
        self.notify("only migrations from this session keep a live log",
                    "muted", 4)

    def _cancel_job(self, jid: str) -> None:
        for j in self.live_jobs:
            if j.id == jid and j.state == jobmod.RUNNING:
                self.open_confirm(
                    f"Cancel the migration of {j.vm_name}? Volumes already "
                    "written on Proxmox are removed.", j.cancel, danger=True,
                    yes="Cancel migration")
                return

    def _delete_server(self, name: str) -> None:
        config.remove_server(self.cfg, name)
        config.save(self.cfg)
        self.refresh_servers()
        if self.active_server and self.active_server.get("name") == name:
            self.active_server = None
            self.src = None
            self.guests = []
        self.notify(f"removed {name}", "success")

    def toggle_mouse(self) -> None:
        on = not bool(config.setting(self.cfg, "mouse", True))
        config.set_setting(self.cfg, "mouse", on)
        config.save(self.cfg)
        self.term.set_mouse(on)
        self.settings_form = None
        self.notify("mouse on - click to pick, wheel to scroll" if on else
                    "mouse off - the terminal handles selection again",
                    "success" if on else "info", 5)

    # -- layout ------------------------------------------------------
    def compose(self, cols: int, rows: int) -> list[str]:
        self.hit.reset()
        self.wide = cols >= 80
        head = self.header(cols)
        # measured first so the body knows its height, rendered again below
        # once the screens have had their say about which buttons are live
        bar_h = len(self.action_bar(cols))
        foot = self.footer(cols)
        body_h = max(3, rows - len(head) - bar_h - len(foot))
        body_y = len(head)

        if self.wide:
            content_w = cols - SIDE_W - 1
            side = self.sidebar(SIDE_W, body_h, 0, body_y)
            content = self.content(content_w, body_h, SIDE_W + 1, body_y)
            body = R.columns([side, content], [SIDE_W, content_w], gap=1)
        else:
            body = self.content(cols, body_h, 0, body_y)
        body = body[:body_h] + [""] * max(0, body_h - len(body))

        bar_rows = self.action_bar(cols, oy=body_y + body_h, register=True)
        bar_rows = (bar_rows + [""] * bar_h)[:bar_h]
        frame = head + body + bar_rows + foot
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
            right = th.paint(f"update {self.update_info['version']} ",
                             "warning") + right
        gap = max(0, cols - vlen(left) - vlen(right))
        return [left + " " * gap + right, R.divider(th, cols)]

    def action_bar(self, cols: int, oy: int = 0, register: bool = False) -> list[str]:
        """One row of buttons, in the same place on every screen."""
        th = self.th
        self.bar.set(self.buttons())
        if not self.bar.buttons:
            return [""]
        rows = self.bar.render(
            th, cols - 2, focused=self.focus == "actions",
            hit=self.hit if register else None,
            origin=(1, oy) if register else None,
            hover=self.hover_arg if self.hover_action == "button" else None)
        return [" " + r for r in rows[:2]]

    def footer(self, cols: int) -> list[str]:
        th = self.th
        if time.time() < self.toast_until and self.toast:
            return [R.divider(th, cols),
                    truncate(R.status_message(th, self.toast, self.toast_kind),
                             cols)]
        if self.overlay:
            ov = self.overlay
            if ov.get("form") is not None and ov.get("focus") == "form":
                keys = [("tab", "next field"), (th.g["enter"], "save"),
                        ("esc", "cancel")]
            else:
                keys = [(f"{'←→' if th.uni else '<>'}", "button"),
                        (th.g["enter"], "run"), ("esc", "close")]
            return [R.divider(th, cols), " " + R.help_bar(th, keys, cols - 2)]
        hints = {
            "nav": [(f"{th.g['up']}{th.g['down']}", "section")],
            "list": [(f"{th.g['up']}{th.g['down']}", "select"),
                     (th.g["enter"], "open")],
            "form": [(f"{th.g['up']}{th.g['down']}", "field"),
                     (f"{'←→' if th.uni else '<>'}", "change")],
            "log": [("pgup/pgdn", "scroll")],
            "actions": [(f"{'←→' if th.uni else '<>'}", "button"),
                        (th.g["enter"], "run")],
        }.get(self.focus, [])
        keys = list(hints) + [("tab", "next pane"), ("?", "help"), ("q", "quit")]
        return [R.divider(th, cols), " " + R.help_bar(th, keys, cols - 2)]

    def sidebar(self, width: int, height: int, ox: int, oy: int) -> list[str]:
        th = self.th
        inner = width - 2
        focused = self.focus == "nav"
        self.hit.add(ox, oy, width, height, "pane", "nav")
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
            text = f" {mark} {i + 1} {label}"
            line = pad(text, inner - vlen(badge)) + badge
            if sel:
                tok = "primary" if focused else "foreground"
                rows.append(f"{th.bg('surface')}{th.fg(tok)}"
                            f"{pad(line, inner)}{RESET}")
            elif self.hover_action == "nav" and self.hover_arg == i:
                rows.append(f"{th.fg('accent')}{pad(line, inner)}{RESET}")
            else:
                rows.append(th.paint(pad(line, inner), "muted"))
            self.hit.add(ox + 1, oy + 1 + i, inner, 1, "nav", i)
        return R.panel(th, rows, width, title="Menu", focused=focused,
                       height=height)

    def content(self, width: int, height: int, ox: int, oy: int) -> list[str]:
        fn = {
            "servers": self.screen_servers,
            "guests": self.screen_guests,
            "jobs": self.screen_jobs,
            "settings": self.screen_settings,
            "wizard": self.screen_wizard,
            "joblog": self.screen_joblog,
        }.get(self.screen, self.screen_servers)
        return fn(width, height, ox, oy)

    def _hover_for(self, *actions: str):
        return self.hover_arg if self.hover_action in actions else None

    # -- screens -----------------------------------------------------
    def screen_servers(self, width: int, height: int, ox: int,
                       oy: int) -> list[str]:
        th = self.th
        focused = self.focus == "list"
        if not config.servers(self.cfg):
            self.hit.add(ox, oy, width, height, "pane", "list")
            body = R.wrap("No Xen or Incus server configured yet. Use "
                          "'Add server' below - xenproxY checks the connection "
                          "and pins the certificate before it stores anything.",
                          width - 4)
            return R.panel(th, [""] + [th.paint(l, "muted") for l in body],
                           width, title="Servers", focused=focused,
                           height=height)

        left_w = max(26, int(width * 0.42))
        right_w = width - left_w - 1
        self.hit.add(ox, oy, left_w, height, "pane", "list")
        lst = self.server_list.render(
            th, left_w - 4, max(3, height - 4), focused=focused, hit=self.hit,
            origin=(ox + 1, oy + 1), hover=self._hover_for("row"))
        left = R.panel(th, lst, left_w, title="Servers",
                       subtitle=f"{len(config.servers(self.cfg))}",
                       focused=focused, height=height)
        right = self.server_detail_panel(right_w, height)
        return R.columns([left, right], [left_w, right_w], gap=1)

    def server_detail_panel(self, width: int, height: int) -> list[str]:
        th = self.th
        inner = width - 4
        srv = config.find_server(self.cfg, self.server_list.value or "")
        if not srv:
            return R.panel(th, [th.paint("  select a server", "muted")],
                           width, title="Connection", height=height)
        auth = "ssh key" if srv.get("key") else (
            "password" if srv.get("password") else "none")
        rows = [th.paint(srv.get("name", ""), "foreground", bold=True), ""]
        rows += R.key_value(th, [
            ("Kind", srv.get("kind", "xen")),
            ("Host", f"{srv.get('user', 'root')}@{srv.get('host')}:"
                     f"{srv.get('port', 22)}"),
            ("Auth", auth),
            ("Added", time.strftime(
                "%Y-%m-%d", time.localtime(srv.get("added", time.time())))),
        ], inner)
        rows += ["", R.divider(th, inner, "transport")]
        if srv.get("tls_pin"):
            rows.append(R.status_message(
                th, "certificate pinned " + srv["tls_pin"][:24] + "\u2026",
                "success"))
        elif srv.get("verify_tls"):
            rows.append(R.status_message(
                th, "certificate verified against the CA store", "success"))
        elif srv.get("kind", "xen") != "xen":
            rows.append(R.status_message(
                th, "disks travel over the SSH session", "success"))
        else:
            rows.append(R.status_message(
                th, "no pinned certificate - edit the server to pin it",
                "warning"))
        rows += ["", th.paint("  Open lists the guests on this server.",
                              "muted")]
        return R.panel(th, rows, width, title="Connection", height=height)

    def screen_guests(self, width: int, height: int, ox: int,
                      oy: int) -> list[str]:
        th = self.th
        focused = self.focus == "list"
        if not self.active_server:
            self.hit.add(ox, oy, width, height, "pane", "list")
            return R.panel(th, ["", th.paint("  Open a server first - press 1 "
                                             "or click 'Servers'.", "muted")],
                           width, title="Guests", height=height)
        if self.load_task and not self.load_task.done:
            spin = R.spinner_frame(th, self.tick)
            return R.panel(th, ["", f"  {spin} " + th.paint(
                "reading the inventory from Xen", "muted")],
                width, title="Guests", focused=focused, height=height)

        left_w = max(28, int(width * 0.46))
        right_w = width - left_w - 1
        inner_l = left_w - 4
        self.hit.add(ox, oy, left_w, height, "pane", "list")
        lst = self.guest_list.render(
            th, inner_l, max(3, height - 4), focused=focused, hit=self.hit,
            origin=(ox + 1, oy + 1), hover=self._hover_for("row"))
        left = R.panel(th, lst, left_w, title="Guests",
                       subtitle=str(len(self.guests)), focused=focused,
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
                    f" {dk['device']}  {migrate.human(dk['virtual_size'])}",
                    "foreground"))
                rows.append(th.paint(f"   {dk.get('sr_name', '')}", "muted"))
        nics = d.get("nics", [])
        if nics:
            rows += ["", R.divider(th, inner, f"network ({len(nics)})")]
            for n in nics:
                vlan = f"  VLAN {n['vlan']}" if n.get("vlan") else ""
                rows.append(th.paint(f" {n['device']}  {n['mac']}{vlan}",
                                     "foreground"))
                rows.append(th.paint(f"   {n.get('network_name', '')}", "muted"))
        if d.get("snapshots"):
            rows += ["", R.status_message(
                th, f"{len(d['snapshots'])} snapshot(s) on the source", "warning")]
        return R.panel(th, rows, width, title="Details", height=height)

    # -- migration wizard --------------------------------------------
    def open_wizard(self) -> None:
        uuid = self.guest_list.value
        if not uuid or not self.src:
            self.notify("select a guest first", "warning")
            return
        detail = self.detail_cache.get(uuid)
        if not detail:
            self.notify("details are still loading", "warning")
            self.load_detail(uuid)
            return
        if self.source_kind() == "xen" and \
                not (self.active_server or {}).get("password"):
            self.notify("this server has no stored password - the XAPI disk "
                        "export needs it (press 'e' on the server)", "danger", 9)
            return
        self.wizard = WizardState(self, detail)
        self.screen = "wizard"
        self.focus = self.ring()[0]

    def screen_wizard(self, width: int, height: int, ox: int,
                      oy: int) -> list[str]:
        return self.wizard.render(width, height, ox, oy) if self.wizard else []

    # -- jobs ---------------------------------------------------------
    def screen_jobs(self, width: int, height: int, ox: int,
                    oy: int) -> list[str]:
        th = self.th
        inner = width - 4
        focused = self.focus == "list"
        self.hit.add(ox, oy, width, height, "pane", "list")
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
            rows += self.job_list.render(
                th, inner, max(3, height - len(rows) - 4), focused=focused,
                hit=self.hit, origin=(ox + 1, oy + 1 + len(rows)),
                hover=self._hover_for("row"))
        elif not running:
            rows = [th.paint("  no migrations yet", "muted")]
        return R.panel(th, rows, width, title="Jobs", focused=focused,
                       height=height)

    def screen_joblog(self, width: int, height: int, ox: int,
                      oy: int) -> list[str]:
        th = self.th
        j = self.open_job
        inner = width - 4
        self.hit.add(ox, oy, width, height, "pane", "log")
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
        return R.panel(th, head, width, title=f"Job {j.id}", subtitle=j.state,
                       focused=self.focus == "log", height=height)

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
            Field("mouse", "Mouse", Toggle(s.get("mouse", True)),
                  help="Click to pick, wheel to scroll. While this is on the "
                       "terminal's own text selection is disabled - press 'm' "
                       "any time to flip it."),
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

    def screen_settings(self, width: int, height: int, ox: int,
                        oy: int) -> list[str]:
        th = self.th
        if self.settings_form is None:
            self.settings_form = self.build_settings_form()
        inner = width - 4
        self.hit.add(ox, oy, width, height, "pane", "form")
        rows = self.settings_form.render(
            th, inner, focused=self.focus == "form", hit=self.hit,
            origin=(ox + 1, oy + 1),
            hover=self._hover_for("field", "caret", "toggle", "choice"))
        hint = self.settings_form.hint(th)
        if hint and self.focus == "form":
            rows += ["", th.paint("  " + hint, "info")]
        if self.update_state:
            text, kind = update.describe(self.update_state)
            rows += ["", R.divider(th, inner, "updates")]
            # wrapped, not truncated: the reason is the whole point of the
            # line, and the install command has to be copyable
            wrapped = R.wrap(text, inner - 2)
            rows.append(R.status_message(th, wrapped[0], kind))
            rows += [th.paint("  " + l, {"danger": "danger", "warning":
                                         "warning"}.get(kind, "muted"))
                     for l in wrapped[1:]]
            rows += [""] + [th.paint("  " + l, "muted") for l in
                            R.wrap(update.install_command(), inner - 2)]
        rows += ["", R.divider(th, inner, "about")]
        rows += R.key_value(th, [
            ("Version", __version__),
            ("Node", f"{pve.node_name()} - {pve.version()}"),
            ("Config", config.CONF_FILE),
            ("Jobs", jobmod.JOB_DIR),
        ], inner)
        return R.panel(th, rows, width, title="Settings",
                       focused=self.focus == "form", height=height)

    def save_settings(self) -> None:
        if not self.settings_form:
            return
        vals = self.settings_form.values()
        for k, v in vals.items():
            config.set_setting(self.cfg, k, v)
        config.save(self.cfg)
        self.th = Theme(config.setting(self.cfg, "theme"),
                        config.setting(self.cfg, "border"))
        self.term.set_mouse(bool(vals.get("mouse", True)))
        self.term.invalidate()
        self.notify("settings saved", "success")

    # -- overlays ------------------------------------------------------
    def open_server_modal(self, first_run: bool = False,
                          edit: dict | None = None) -> None:
        e = edit or {}
        form = Form([
            Field("kind", "Source", Choice(
                [("xen", "XenServer / XCP-ng"), ("incus", "Incus")],
                e.get("kind", "xen")),
                help="Xen guests are pulled through the XAPI disk export, "
                     "Incus guests through `incus export` over this SSH "
                     "session."),
            Field("name", "Name", TextField(e.get("name", ""),
                                            placeholder="xen-node-1")),
            Field("host", "Host", TextField(e.get("host", ""),
                                            placeholder="192.168.0.10")),
            Field("user", "User", TextField(e.get("user", "root"))),
            Field("password", "Password",
                  TextField(e.get("password", ""), masked=True),
                  help="Needed for the XAPI disk export, even when SSH uses "
                       "a key."),
            Field("key", "SSH key", TextField(e.get("key", ""),
                                              placeholder="/etc/xenproxy/keys/id"),
                  help="Optional path to a private key. With a key set, the "
                       "password is not used for the SSH session."),
            Field("port", "SSH port", TextField(str(e.get("port", 22)),
                                                numeric=True)),
            Field("verify_tls", "Verify TLS", Toggle(e.get("verify_tls", False)),
                  help="XCP-ng ships a self-signed certificate; leave this off "
                       "and xenproxY pins its fingerprint over SSH instead.",
                  visible=lambda v: v.get("kind") == "xen"),
            Field("insecure_tls", "Allow unverified",
                  Toggle(e.get("insecure_tls", False)),
                  help="Only if the fingerprint cannot be pinned. The host "
                       "password then travels over an unverified channel.",
                  visible=lambda v: v.get("kind") == "xen"),
        ])
        self.overlay = {
            "kind": "server", "form": form, "first_run": first_run,
            "edit": bool(edit), "testing": None, "tested": False,
            "focus": "form",
            "bar": ButtonBar([Button("cancel", "Cancel", "esc"),
                              Button("test", "Test connection", "ctrl-t"),
                              Button("save", "Save", "enter")]),
        }

    def open_update_modal(self) -> None:
        self.overlay = {
            "kind": "update", "info": self.update_info, "focus": "actions",
            "bar": ButtonBar([Button("later", "Later", "n"),
                              Button("update", "Update now", "y",
                                     token="success")]),
        }

    def open_confirm(self, text: str, on_yes, danger: bool = False,
                     yes: str = "Yes") -> None:
        self.overlay = {
            "kind": "confirm", "text": text, "on_yes": on_yes,
            "danger": danger, "focus": "actions",
            "bar": ButtonBar([Button("no", "Cancel", "n"),
                              Button("yes", yes, "y",
                                     token="danger" if danger else "primary")]),
        }

    def open_help(self) -> None:
        self.overlay = {
            "kind": "help", "focus": "actions",
            "bar": ButtonBar([Button("close", "Close", "esc")]),
        }

    HELP_KEYS = [
        ("tab / shift-tab", "next pane: menu, content, actions"),
        ("1 2 3 4", "jump straight to a section"),
        ("up / down", "move inside the focused list or form"),
        ("left / right", "change a value, or pick another button"),
        ("enter", "run the highlighted button"),
        ("esc", "leave a screen, close a dialog"),
        ("?", "this help"),
        ("q", "quit"),
    ]
    HELP_MOUSE = [
        ("click", "pick a menu entry, a row, a field or a button"),
        ("click again", "run the action of the selected row"),
        ("wheel", "scroll the list or log under the pointer"),
        ("shift-M", "mouse reporting off and on - off restores the"),
        ("", "terminal's own text selection"),
    ]

    def _overlay_body(self, w: int) -> tuple[list[str], str, str]:
        th = self.th
        ov = self.overlay
        inner = w - 4
        if ov["kind"] == "help":
            body = [th.paint("Keyboard", "primary", bold=True)]
            body += R.key_value(th, self.HELP_KEYS, inner, key_width=18)
            body += ["", th.paint("Pointer", "primary", bold=True)]
            body += R.key_value(th, self.HELP_MOUSE, inner, key_width=18)
            return body, "Help", "primary"
        if ov["kind"] == "server":
            title = "Add source server" if not ov["edit"] \
                else "Edit source server"
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
                chosen = pick_host_key(ov["host_key"])
                for key in ov["host_key"]:
                    pick = key is chosen
                    body.append(th.paint(
                        f"  {key['type']:<8} {key['bits']:>5} bit  "
                        f"{key['fingerprint']}", "foreground" if pick else "muted",
                        bold=pick))
                body += ["", th.paint(f"Only the {chosen['type']} key is stored.",
                                      "muted")]
                return body, "Confirm the SSH host key", "warning"
            body += ["__FORM__"]
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
                    th, f"XAPI certificate pinned {pin[:16]}…" if pin
                    else "XAPI certificate could not be pinned",
                    "success" if pin else "warning"))
            return body, title, "primary"
        if ov["kind"] == "update":
            info = ov["info"] or {}
            body = [th.paint(f"xenproxY {info.get('version')} is available.",
                             "foreground", bold=True),
                    th.paint(f"You are running {__version__}.", "muted"), ""]
            if info.get("notes"):
                body += [R.divider(th, inner, "release notes")]
                body += [th.paint(l, "muted") for l in
                         R.wrap(info["notes"], inner)[:8]]
                body += [""]
            body += [th.paint("The installer replaces /opt/xenproxy and keeps "
                              "your configuration.", "foreground")]
            return body, "Update available", "warning"
        body = [""] + [th.paint(l, "foreground") for l in
                       R.wrap(ov["text"], inner)]
        return body, "Confirm", "danger" if ov.get("danger") else "primary"

    def draw_overlay(self, frame: list[str], cols: int, rows: int) -> list[str]:
        th = self.th
        ov = self.overlay
        w = min(74, cols - 6)
        inner = w - 4
        body, title, token = self._overlay_body(w)

        # A modal owns the pointer while it is open, so the map underneath it
        # is discarded rather than left reachable through the gaps.
        self.hit.reset()

        form = ov.get("form") if ov["kind"] == "server" and not ov.get("host_key") \
            else None
        # measure first so the box position is known before registering zones
        probe = list(body)
        if form is not None:
            probe[probe.index("__FORM__"):probe.index("__FORM__") + 1] = \
                form.render(th, inner, focused=False)
            hint = form.hint(th)
            probe += [""] + ([th.paint(hint, "info")] if hint else [])
        bar_rows = ov["bar"].render(th, inner, focused=False)
        probe += [""] + bar_rows
        box_h = len(probe) + 2
        top = max(0, (rows - box_h) // 2)
        left = max(0, (cols - w) // 2)

        final = list(body)
        if form is not None:
            fi = final.index("__FORM__")
            form_origin = (left + 1, top + 1 + fi)
            final[fi:fi + 1] = form.render(
                th, inner, focused=ov.get("focus") == "form", hit=self.hit,
                origin=form_origin,
                hover=self._hover_for("field", "caret", "toggle", "choice"))
            hint = form.hint(th)
            final += [""] + ([th.paint(hint, "info")] if hint else [])
        bar_y = top + 1 + len(final) + 1
        final += [""] + ov["bar"].render(
            th, inner, focused=ov.get("focus", "actions") == "actions",
            hit=self.hit, origin=(left + 1, bar_y),
            hover=self._hover_for("button"))

        box = R.panel(th, final, w, title=title, focused=True, token=token)
        out = list(frame)
        while len(out) < rows:
            out.append("")
        for i, line in enumerate(box):
            y = top + i
            if 0 <= y < rows:
                # column-exact cut on both sides, so the frame around the
                # dialog stays whole instead of being sheared off
                prefix = pad(slice_cols(out[y], 0, left), left)
                tail = slice_cols(out[y], left + w)
                out[y] = prefix + line + (RESET + tail if tail else "")
        return out

    # -- key handling --------------------------------------------------
    def typing(self) -> bool:
        """True while a text field has focus, so letters stay letters."""
        if self.focus != "form":
            return False
        form = self._active_form()
        f = form.current if form else None
        return isinstance(getattr(f, "widget", None), TextField)

    def handle(self, k: str) -> None:
        if self.overlay:
            return self.handle_overlay(k)
        if k == "ctrl-c":
            self.running = False
            return
        if k in ("tab", "shift-tab"):
            # tab means one thing everywhere: the next control. Inside a form
            # that is the next field, and past its last field the action bar.
            if self.focus == "form":
                form = self._active_form()
                if form is not None and form.key(k):
                    if self.screen == "wizard" and self.wizard:
                        self.wizard._sync_help()
                    return
            self.focus_next(1 if k == "tab" else -1)
            return
        if not self.typing():
            if k == "?":
                self.open_help()
                return
            if k == "M":
                self.toggle_mouse()
                return
            if k == "q":
                self.running = False
                return
            if k in ("1", "2", "3", "4") and self.screen != "wizard":
                self.goto(NAV[int(k) - 1][0])
                return

        if self.focus == "nav":
            if k in ("up", "k"):
                self.goto(NAV[(self.nav_index - 1) % len(NAV)][0])
                self.focus = "nav"
                return
            if k in ("down", "j"):
                self.goto(NAV[(self.nav_index + 1) % len(NAV)][0])
                self.focus = "nav"
                return
            if k == "enter":
                self.goto(NAV[self.nav_index][0])
                return
        if self.focus == "actions":
            action = self.bar.key(k)
            if action:
                self.run_action(action)
                return
            if k in ("left", "right", "h", "l", "enter", "space"):
                return

        if self.screen == "wizard" and self.wizard:
            return self.wizard.key(k)
        if self.screen == "joblog":
            return self.handle_joblog(k)
        if self.screen == "settings":
            return self.handle_settings(k)

        pane = {"servers": self.server_list, "guests": self.guest_list,
                "jobs": self.job_list}.get(self.screen)
        if pane is not None and self.focus == "list" and pane.key(k):
            if self.screen == "guests":
                self.guest_detail = self.detail_cache.get(self.guest_list.value)
            return
        if k == "enter" and self.focus == "list":
            self.run_action(self.primary_for(self.screen))
            return
        if k == "esc" and self.screen != "servers":
            self.goto("servers")
            return
        for b in self.buttons():
            if b.enabled and b.shortcut == k:
                self.run_action(b.action)
                return

    def handle_settings(self, k: str) -> None:
        if self.focus == "form" and self.settings_form:
            if self.settings_form.key(k):
                return
        if k == "esc":
            self.goto("servers")
            return
        for b in self.buttons():
            if b.enabled and b.shortcut == k:
                self.run_action(b.action)
                return

    def handle_joblog(self, k: str) -> None:
        j = self.open_job
        if k == "esc":
            self.goto("jobs")
            return
        if j and self.focus == "log" and self.logview.key(k, len(j.log), 10):
            return
        for b in self.buttons():
            if b.enabled and b.shortcut == k:
                self.run_action(b.action)
                return

    def handle_overlay(self, k: str) -> None:
        ov = self.overlay
        if ov["kind"] == "server" and ov.get("host_key"):
            if k in ("y", "Y", "enter"):
                offered = ov.pop("host_key")
                # store only the key that was shown as the one to accept
                trust_host_keys([pick_host_key(offered)])
                self.notify("host key stored", "success")
                self._test_overlay_server()
            elif k in ("n", "N", "esc"):
                ov.pop("host_key", None)
                ov.pop("pending_save", None)
                self.notify("host key not stored - nothing was sent", "warning", 6)
            return
        if k == "tab" and ov.get("form") is not None:
            if ov.get("focus") == "form" and ov["form"].key("tab"):
                return
            ov["focus"] = "actions" if ov.get("focus") == "form" else "form"
            return
        if k == "shift-tab" and ov.get("form") is not None:
            if ov.get("focus") == "form" and ov["form"].key("shift-tab"):
                return
            ov["focus"] = "actions" if ov.get("focus") == "form" else "form"
            return
        if k == "esc":
            self.close_overlay("cancel")
            return
        if ov.get("focus") == "form" and ov.get("form") is not None:
            if k == "enter":
                self.overlay_action("save")
                return
            t = ov.get("testing")
            if t and t.done:
                ov["testing"] = None
            if ov["form"].key(k):
                return
        action = ov["bar"].key(k)
        if action:
            self.overlay_action(action)

    def overlay_action(self, action: str) -> None:
        ov = self.overlay
        if not ov:
            return
        if ov["kind"] == "server":
            if action == "cancel":
                self.close_overlay("cancel")
            elif action == "test":
                self._test_overlay_server()
            elif action == "save":
                self._save_overlay_server()
            return
        if ov["kind"] == "update":
            self.overlay = None
            if action == "update":
                self.do_update()
            return
        if ov["kind"] == "help":
            self.overlay = None
            return
        if ov["kind"] == "confirm":
            fn = ov["on_yes"]
            self.overlay = None
            if action == "yes":
                fn()

    def close_overlay(self, _why: str = "") -> None:
        ov = self.overlay
        if ov and ov["kind"] == "server" and ov.get("first_run") \
                and not config.servers(self.cfg):
            self.running = False
        self.overlay = None

    # -- mouse ---------------------------------------------------------
    def on_mouse(self, ev: Mouse) -> None:
        if ev.kind == "move":
            z = self.hit.at(ev.x, ev.y)
            self.hover_action = z.action if z else ""
            self.hover_arg = z.arg if z else None
            return
        if ev.kind in ("wheel-up", "wheel-down"):
            return self._wheel(ev)
        if ev.kind != "press" or ev.button != 1:
            return
        z = self.hit.at(ev.x, ev.y)
        if not z:
            return
        if self.overlay:
            return self._click_overlay(z, ev)
        if z.action == "nav":
            self.goto(NAV[z.arg][0])
            self.focus = "nav"
            return
        if z.action == "row":
            pane, index = z.arg
            self.focus = "list"
            lst = {"servers": self.server_list, "guests": self.guest_list,
                   "jobs": self.job_list}.get(pane)
            if lst is None:
                return
            if lst.cursor == index:
                self.run_action(self.primary_for(pane))
            else:
                lst.select(index)
                if pane == "guests":
                    self.guest_detail = self.detail_cache.get(self.guest_list.value)
            return
        if z.action == "button":
            self.focus = "actions"
            for i, b in enumerate(self.bar.buttons):
                if b.action == z.arg:
                    if not b.enabled:
                        return
                    self.bar.index = i
            self.run_action(z.arg)
            return
        if z.action in ("field", "caret", "toggle", "choice"):
            return self._click_field(self._active_form(), z, ev)
        if z.action == "step" and self.wizard:
            self.wizard.goto_step(z.arg)
            return
        if z.action == "pane":
            if z.arg in self.ring():
                self.focus = z.arg
            return

    def _active_form(self) -> Form | None:
        if self.screen == "settings":
            return self.settings_form
        if self.screen == "wizard" and self.wizard:
            return self.wizard.form()
        return None

    def _click_field(self, form: Form | None, z, ev: Mouse) -> None:
        if form is None:
            return
        self.focus = "form"
        act = form.active()
        if z.action == "choice":
            index, option = z.arg
            if 0 <= index < len(act):
                form.focus_field(index)
                w = act[index].widget
                if isinstance(w, Choice) and 0 <= option < len(w.options):
                    w.index = option
            return
        index = z.arg
        if not 0 <= index < len(act):
            return
        form.focus_field(index)
        w = act[index].widget
        if z.action == "toggle" and isinstance(w, Toggle):
            w.value = not w.value
        elif z.action == "caret" and isinstance(w, TextField):
            w.click(ev.x - z.x)

    def _click_overlay(self, z, ev: Mouse) -> None:
        ov = self.overlay
        if z.action == "button":
            ov["focus"] = "actions"
            for i, b in enumerate(ov["bar"].buttons):
                if b.action == z.arg:
                    ov["bar"].index = i
            self.overlay_action(z.arg)
            return
        if z.action in ("field", "caret", "toggle", "choice"):
            ov["focus"] = "form"
            self._click_field(ov.get("form"), z, ev)

    def _wheel(self, ev: Mouse) -> None:
        step = -3 if ev.kind == "wheel-up" else 3
        z = self.hit.find(ev.x, ev.y, "pane")
        pane = z.arg if z else None
        if pane == "log" and self.open_job:
            self.logview.scroll(step, len(self.open_job.log))
            return
        lst = None
        if pane == "list":
            lst = {"servers": self.server_list, "guests": self.guest_list,
                   "jobs": self.job_list}.get(self.screen)
        if lst is not None:
            lst.scroll(step)
            if self.screen == "guests":
                self.guest_detail = self.detail_cache.get(self.guest_list.value)
            return
        if pane == "nav":
            self.goto(NAV[(self.nav_index + (1 if step > 0 else -1))
                          % len(NAV)][0])
            self.focus = "nav"

    # -- server modal plumbing -----------------------------------------
    def _overlay_server_dict(self) -> dict:
        v = self.overlay["form"].values()
        return {
            "name": (v.get("name") or "").strip(),
            "kind": v.get("kind") or "xen",
            "host": (v.get("host") or "").strip(),
            "user": (v.get("user") or "root").strip(),
            "password": v.get("password") or "",
            "key": (v.get("key") or "").strip(),
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
        host = self.connect(srv)
        info = host.probe()
        info["tls_pin"] = ""
        if srv.get("kind", "xen") == "xen":
            try:
                info["tls_pin"] = host.fetch_tls_pin()
            except SSHError:
                pass
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
        if not srv["tls_pin"] and srv.get("kind", "xen") == "xen":
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
                ov["bar"] = ButtonBar([Button("cancel", "Cancel", "n"),
                                       Button("accept", "Accept key", "y",
                                              token="warning")])
                ov["focus"] = "actions"
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
        self.kind = (app.active_server or {}).get("kind", "xen")
        self.modes = migrate.modes_for(self.kind)
        plan = migrate.default_plan(detail, (app.active_server or {}).get("name", ""))
        if plan["mode"] not in self.modes:
            plan["mode"] = next(iter(self.modes))
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

        self.pages: list[tuple[str, Form]] = [
            ("Mode", Form([
                Field("mode", "Copy mode", Choice(
                    [(k, k.capitalize()) for k in
                     ("offline", "snapshot", "live") if k in self.modes],
                    plan["mode"]),
                    help=self.modes[plan["mode"]]),
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
                Field("guest_prep", "Adapt guest for KVM",
                      Toggle(plan["guest_prep"]),
                      help="Pins net.ifnames=0 so a Xen eth0 keeps its name."),
                Field("verify", "Verify written volumes", Toggle(plan["verify"]),
                      help="Reads each volume back and compares checksums. "
                           "Roughly doubles the disk read time."),
                Field("onboot", "Start on host boot", Toggle(plan["onboot"])),
            ])),
            ("Review", Form([])),
        ]

    def form(self) -> Form:
        return self.pages[self.page][1]

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
        arrow = "→" if self.app.th.uni else "->"
        return [
            ("Source", f"{self.detail['name']}  ({self.detail.get('power_state')})"),
            ("Mode", f"{p['mode']} - {self.modes.get(p['mode'], '')}"),
            ("Disks", f"{len(self.detail.get('disks', []))}  "
                      f"{migrate.human(total)}  {arrow} {p['storage']}"),
            ("Target", f"VMID {p['target_vmid']}  {p['target_name']}"),
            ("Hardware", f"{p['sockets']}x{p['cores']} vCPU, {p['memory']} MiB, "
                         f"{p['bios']}/{p['machine']}, cpu={p['cpu']}"),
            ("Network", net),
            ("After", ("start, " if p["start_after"] else "")
                      + ("adapt guest, " if p["guest_prep"] else "")
                      + ("verify" if p["verify"] else "no verify")),
        ]

    # -- render ---------------------------------------------------------
    def render(self, width: int, height: int, ox: int, oy: int) -> list[str]:
        app = self.app
        th = app.th
        inner = width - 4
        app.hit.add(ox, oy, width, height, "pane", "form")
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
        crumb_line = truncate(sep.join(crumbs), inner)
        rows = [crumb_line, ""]
        # the finished steps are click targets, so a value can be corrected
        # without walking back through the pages
        x = ox + 1
        for i, c in enumerate(crumbs):
            w = vlen(c)
            if i <= self.page:
                app.hit.add(x, oy + 1, w, 1, "step", i)
            x += w + vlen(sep)

        title, form = self.pages[self.page]
        if title == "Review":
            rows += R.key_value(th, self.summary(), inner, key_width=12)
            rows += ["", R.divider(th, inner)]
            warn = self._warnings()
            for w in warn:
                rows.append(R.status_message(th, w, "warning"))
            if warn:
                rows.append("")
            rows.append(th.paint(
                f"{th.g['enter']} or 'Start migration' below begins the copy",
                "success", bold=True))
        else:
            rows += form.render(
                th, inner, focused=app.focus == "form", hit=app.hit,
                origin=(ox + 1, oy + 1 + len(rows)),
                hover=app._hover_for("field", "caret", "toggle", "choice"))
            hint = form.hint(th)
            if hint and app.focus == "form":
                rows += ["", th.paint("  " + hint, "info")]
        return R.panel(th, rows, width,
                       title=f"Migrate {self.detail['name']}",
                       subtitle=f"step {self.page + 1}/{len(self.pages)}",
                       focused=app.focus == "form", height=height)

    def _warnings(self) -> list[str]:
        p = self.collect()
        out = []
        if p["mode"] == "live":
            out.append("live mode produces a crash-consistent image - "
                       "the guest replays its journal on first boot")
        if p["mode"] == "snapshot" and self.kind == "incus":
            out.append("snapshot mode copies the snapshot into a temporary "
                       "instance on the Incus host and removes it afterwards - "
                       "that needs free space in the source pool")
        elif p["mode"] == "snapshot":
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
            out.append("the source has snapshots; the export resolves them "
                       "into one flat image")
        if self.detail.get("extra_disks"):
            out.append(f"{len(self.detail['extra_disks'])} extra block "
                       "device(s) are separate Incus volumes and are not "
                       "part of this copy")
        if p["bios"] == "ovmf":
            out.append("UEFI guest - an EFI disk is created on the target")
        return out

    # -- keys -----------------------------------------------------------
    def action(self, name: str) -> None:
        if name == "wiz.back":
            if self.page == 0:
                self.app.goto("guests")
                self.app.wizard = None
            else:
                self.page -= 1
                self.app.focus = self.app.ring()[0]
            return
        if name == "wiz.next":
            self.page = min(len(self.pages) - 1, self.page + 1)
            self._sync_help()
            self.app.focus = self.app.ring()[0]
            return
        if name == "wiz.start":
            self.start()

    def goto_step(self, index: int) -> None:
        if 0 <= index <= self.page:
            self.page = index
            self._sync_help()
            self.app.focus = self.app.ring()[0]

    def key(self, k: str) -> None:
        title, form = self.pages[self.page]
        if k == "esc":
            self.action("wiz.back")
            return
        if k == "enter":
            self.action("wiz.start" if title == "Review" else "wiz.next")
            return
        if self.app.focus == "form" and form.key(k):
            self._sync_help()

    def _sync_help(self) -> None:
        for _title, form in self.pages:
            for f in form.fields:
                if f.key == "mode":
                    f.help = self.modes.get(f.widget.value, "")
                if f.key == "net_mode":
                    f.help = migrate.NET_MODES.get(f.widget.value, "No NIC at all.")

    def start(self) -> None:
        plan = self.collect()
        app = self.app
        job = jobmod.Job(plan)
        mig = migrate.Migration(job, app.src, plan)
        app.live_jobs.insert(0, job)
        app.open_job = job
        app.logview = LogView()
        app.wizard = None
        app.goto("joblog")
        app.focus = "log"
        threading.Thread(target=mig.run, daemon=True).start()
        app.notify(f"migration of {plan['vm_name']} started", "success")
