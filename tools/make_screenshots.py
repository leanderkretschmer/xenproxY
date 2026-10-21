#!/usr/bin/env python3
"""Render the real UI into docs/screens/*.svg.

Drives the actual renderer with stand-in data, so the images in the README
cannot drift away from the code. Run from the repository root:

    python3 tools/make_screenshots.py
"""
from __future__ import annotations

import os
import sys
import time

# The theme reads these to decide colour depth and glyph set; without them a
# non-tty run would render monochrome ASCII.
os.environ["COLORTERM"] = "truecolor"
os.environ.setdefault("TERM", "xterm-256color")
os.environ.pop("NO_COLOR", None)
os.environ.pop("NO_UNICODE", None)

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tools"))

from ansi2svg import render  # noqa: E402

from xenproxy import config, jobs as jobmod, migrate, pve  # noqa: E402

OUT = os.path.join(ROOT, "docs", "screens")
COLS, ROWS = 104, 30

STORAGES = [
    {"name": "nvme-pool", "type": "zfspool", "content": "images",
     "total": 2 << 40, "used": 700 << 30, "avail": 1340 << 30, "sparse": False,
     "nodes": ""},
    {"name": "local-zfs", "type": "zfspool", "content": "images",
     "total": 900 << 30, "used": 120 << 30, "avail": 780 << 30, "sparse": True,
     "nodes": ""},
    {"name": "ceph-vm", "type": "rbd", "content": "images",
     "total": 8 << 40, "used": 3 << 40, "avail": 5 << 40, "sparse": True,
     "nodes": ""},
]
BRIDGES = [{"name": "vmbr0", "state": "UP", "vlan_aware": True},
           {"name": "vmbr1", "state": "UP", "vlan_aware": False},
           {"name": "vmbr9", "state": "UNKNOWN", "vlan_aware": False}]

GUESTS = [
    ("minecraft-01", "running", 16 << 30, 6),
    ("gitlab", "running", 8 << 30, 4),
    ("mail-relay", "running", 2 << 30, 2),
    ("nextcloud", "running", 4 << 30, 2),
    ("build-agent-02", "halted", 4 << 30, 4),
    ("dns-secondary", "running", 1 << 30, 1),
    ("legacy-erp", "halted", 8 << 30, 4),
    ("monitoring", "running", 4 << 30, 2),
]

DETAIL = {
    "uuid": "6d139dc5-357c-0e5d-fc74-c4eb249a2ddf",
    "name": "minecraft-01", "power_state": "running",
    "memory": 16 << 30, "vcpus": 6, "cores_per_socket": 1,
    "firmware": "bios", "hvm": True, "snapshots": [],
    "disks": [{"device": "xvda", "virtual_size": 200 << 30,
               "sr_name": "Local SSD storage", "vdi": "4097f33c", "sr": "eca047ca",
               "label": "root"},
              {"device": "xvdb", "virtual_size": 500 << 30,
               "sr_name": "Local HDD storage", "vdi": "9b21ac04", "sr": "eca047ca",
               "label": "worlds"}],
    "nics": [{"device": "0", "mac": "0e:48:1b:f7:a5:49", "mtu": 1500,
              "network": "n1", "network_name": "DMZ", "vlan": 1051},
             {"device": "1", "mac": "0e:48:1b:f7:a5:50", "mtu": 1500,
              "network": "n2", "network_name": "Storage", "vlan": None}],
}


def patch() -> None:
    pve.storages = lambda **kw: STORAGES
    pve.bridges = lambda: BRIDGES
    pve.next_vmid = lambda: 210
    pve.vmid_free = lambda _v: True
    pve.is_pve_node = lambda: True
    pve.node_name = lambda: "pve-01"
    pve.version = lambda: "pve-manager/9.2.11"
    config.load = lambda: {
        "servers": [
            {"name": "xen-node-1", "kind": "xen", "host": "10.10.0.20",
             "user": "root", "password": "x", "port": 22, "verify_tls": False,
             "tls_pin": "794d2749ce97dd2a8855f9e9696c0564157f1444a2ea8f85",
             "added": time.time() - 86400 * 9},
            {"name": "xen-node-2", "kind": "xen", "host": "10.10.0.21",
             "user": "root", "password": "x", "port": 22, "verify_tls": False,
             "added": time.time() - 86400 * 3},
            {"name": "incus-01", "kind": "incus", "host": "10.10.0.40",
             "user": "root", "key": "/etc/xenproxy/keys/incus-01_ed25519",
             "port": 22, "added": time.time() - 86400},
        ],
        "settings": dict(config.DEFAULTS["settings"], check_updates=False),
    }
    config.save = lambda _c: None


INCUS_GUESTS = [
    ("build-runner", "running", 4 << 30, 4),
    ("db-replica", "running", 8 << 30, 4),
    ("gitea", "running", 2 << 30, 2),
    ("sandbox", "halted", 2 << 30, 2),
    ("win-test", "running", 8 << 30, 4),
]

INCUS_DETAIL = {
    "uuid": "db-replica", "name": "db-replica", "power_state": "running",
    "memory": 8 << 30, "vcpus": 4, "cores_per_socket": 1,
    "firmware": "uefi", "hvm": True, "snapshots": ["nightly-20261020"],
    "extra_disks": [],
    "disks": [{"device": "root", "virtual_size": 120 << 30, "sr_name": "nvme",
               "vdi": "db-replica", "sr": "nvme", "label": "root"}],
    "nics": [{"device": "eth0", "mac": "10:66:6a:22:d8:17", "mtu": 1500,
              "network": "br0", "network_name": "br0", "vlan": 1051}],
}


def new_app():
    from xenproxy.ui.app import App
    app = App()
    app.term = type("T", (), {"invalidate": lambda self: None,
                              "set_mouse": lambda self, on: None})()
    return app


def fill_incus(app) -> None:
    from xenproxy.ui.app import _guest_hint
    app.active_server = config.load()["servers"][2]
    app.src = object()
    app.screen = "guests"
    app.nav_index = 1
    app.guests = [{"uuid": n, "name": n, "power_state": s, "memory": m,
                   "vcpus": c, "description": "", "template": False,
                   "tags": "default"}
                  for n, s, m, c in INCUS_GUESTS]
    app.guest_list.set_items(
        [(g["uuid"], g["name"], _guest_hint(g)) for g in app.guests])
    app.guest_list.cursor = 1
    app.detail_cache = {"db-replica": INCUS_DETAIL}
    app.guest_detail = INCUS_DETAIL


def fill_guests(app) -> None:
    app.active_server = config.load()["servers"][0]
    app.src = object()  # the wizard only needs a non-None handle here
    app.screen = "guests"
    app.nav_index = 1
    app.guests = [{"uuid": f"uuid-{i}", "name": n, "power_state": s,
                   "memory": m, "vcpus": c, "description": "", "template": False,
                   "tags": ""}
                  for i, (n, s, m, c) in enumerate(GUESTS)]
    from xenproxy.ui.app import _guest_hint
    app.guest_list.set_items(
        [(g["uuid"], g["name"], _guest_hint(g)) for g in app.guests])
    app.guest_list.cursor = 0
    app.detail_cache = {"uuid-0": DETAIL}
    app.guest_detail = DETAIL


def make_job() -> jobmod.Job:
    plan = {"vm_name": "minecraft-01", "mode": "offline", "target_vmid": 210,
            "server": "xen-node-1", "vm_uuid": DETAIL["uuid"]}
    j = jobmod.Job(plan)
    j.save = lambda **kw: None
    j.add_steps(migrate.STEPS)
    now = time.time()
    j.created = now - 268
    states = [("connect", "done", "xen-node-1", 2),
              ("preflight", "done", "VMID 210 free, storage ok", 3),
              ("source", "done", "halted", 14),
              ("disks", "running", "", 249)]
    for key, st, detail, dur in states:
        s = j.step(key)
        s.state, s.detail = st, detail
        s.started = now - dur
        s.ended = now if st != "running" else 0
    j.state = jobmod.RUNNING
    j.bytes_total = 700 << 30
    j.bytes_done = int(j.bytes_total * 0.47)
    j.rate = 331 << 20
    j.current_disk = "xvdb (500.0 GiB)"
    msgs = [
        ("xen-node-1 - XCP-ng 8.3.0", "info"),
        ("minecraft-01: 2 disk(s), 700.0 GiB, 6 vCPU, 16.0 GiB RAM", "info"),
        ("storage nvme-pool (zfspool): 1.3 TiB free", "info"),
        ("clean shutdown of the source guest", "info"),
        ("source guest halted", "success"),
        ("allocating vm-210-disk-0 on nvme-pool - 200.0 GiB", "info"),
        ("streaming VDI 4097f33c -> vm-210-disk-0", "info"),
        ("200.0 GiB in 631s (324.6 MiB/s)", "info"),
        ("verifying the written volume", "info"),
        ("md5 eb8f85801ec2475ee150ee0c6fb3709c - written volume matches the stream",
         "success"),
        ("allocating vm-210-disk-1 on nvme-pool - 500.0 GiB", "info"),
        ("streaming VDI 9b21ac04 -> vm-210-disk-1", "info"),
    ]
    for i, (m, k) in enumerate(msgs):
        j.log.append((now - 260 + i * 20, k, m))
    return j


def shot(name: str, lines: list[str], title: str) -> None:
    os.makedirs(OUT, exist_ok=True)
    svg = render(lines, title=title, cols=COLS)
    path = os.path.join(OUT, name)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(svg)
    print(f"  {name}  ({len(svg) // 1024} KiB, {len(lines)} rows)")


def main() -> int:
    patch()

    app = new_app()
    app.overlay = None
    app.screen = "servers"
    app.nav_index = 0
    shot("servers.svg", app.compose(COLS, ROWS), "xenproxY - servers")

    app = new_app()
    fill_guests(app)
    shot("guests.svg", app.compose(COLS, ROWS), "xenproxY - guests")

    app = new_app()
    fill_guests(app)
    app.open_wizard()
    shot("wizard-mode.svg", app.compose(COLS, ROWS), "xenproxY - migration wizard")

    app = new_app()
    fill_guests(app)
    app.open_wizard()
    app.wizard.pages[1][1].fields[0].widget.value = "210"
    app.wizard.pages[2][1].fields[1].widget.index = 0
    app.wizard.page = 5
    shot("wizard-review.svg", app.compose(COLS, ROWS), "xenproxY - review")

    app = new_app()
    fill_guests(app)
    job = make_job()
    app.live_jobs = [job]
    app.open_job = job
    app.screen = "joblog"
    app.nav_index = 2
    app.tick = 3
    shot("job.svg", app.compose(COLS, ROWS), "xenproxY - migration running")

    app = new_app()
    fill_incus(app)
    shot("incus.svg", app.compose(COLS, ROWS), "xenproxY - guests on Incus")

    app = new_app()
    app.goto("settings")
    app.focus = "form"
    from xenproxy import update as _update
    app.update_state = {"state": _update.CURRENT, "error": "",
                        "info": {"version": __import__(
                            "xenproxy").__version__, "notes": ""}}
    shot("settings.svg", app.compose(COLS, ROWS), "xenproxY - settings")

    app = new_app()
    app.compose(COLS, ROWS)
    app.open_help()
    shot("help.svg", app.compose(COLS, ROWS), "xenproxY - keyboard and mouse")

    saved = config.load
    config.load = lambda: {"servers": [],
                           "settings": dict(config.DEFAULTS["settings"],
                                            check_updates=False)}
    app = new_app()
    app.refresh_servers()
    app.open_server_modal(first_run=True)
    app.overlay["form"].fields[1].widget.value = "xen-node-1"
    app.overlay["form"].fields[2].widget.value = "10.10.0.20"
    app.overlay["form"].fields[4].widget.value = "************"
    app.overlay["form"].focus = 4
    shot("setup.svg", app.compose(COLS, ROWS), "xenproxY - first run")
    config.load = saved
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
