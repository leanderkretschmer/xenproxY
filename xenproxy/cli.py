"""Command line entry point."""
from __future__ import annotations

import argparse
import json
import os
import sys
import time

from . import __version__, config, jobs as jobmod, migrate, pve, update
from .incus import IncusHost
from .sshclient import (KNOWN_HOSTS, HostKeyUnknown, SSHClient,
                        host_key_known, pick_host_key, scan_host_keys,
                        trust_host_keys)
from .xen import XenHost

KINDS = {"xen": XenHost, "incus": IncusHost}


def _server(cfg: dict, name: str | None) -> dict:
    servers = config.servers(cfg)
    if not servers:
        sys.exit("no source server configured - run `xenproxY` and press 'a'")
    if name:
        srv = config.find_server(cfg, name)
        if not srv:
            sys.exit(f"unknown server '{name}'")
        return srv
    if len(servers) > 1:
        sys.exit("more than one server configured - pass --server")
    return servers[0]


def _connect(srv: dict, warn: bool = True):
    """Build the adapter the stored server kind asks for."""
    ssh = SSHClient(srv["host"], srv.get("user", "root"),
                    srv.get("password") or None, srv.get("key") or None,
                    int(srv.get("port", 22)))
    kind = srv.get("kind", "xen")
    if kind == "incus":
        return IncusHost(ssh)
    xen = XenHost(ssh, verify_tls=bool(srv.get("verify_tls")),
                  tls_pin=srv.get("tls_pin", ""),
                  insecure_tls=bool(srv.get("insecure_tls")))
    if warn and not xen.tls_pin and not srv.get("verify_tls"):
        note = ("disk export will run unverified"
                if srv.get("insecure_tls") else "disk export will refuse to run")
        print("warning: no pinned XAPI certificate for "
              f"{srv.get('name', srv['host'])} - re-add the server to pin it "
              f"({note})", file=sys.stderr)
    return xen


def _ensure_host_key(host: str, port: int, args) -> bool:
    """Confirm the SSH host key before any credential is sent.

    Everything else hangs off this: the password travels over this
    session, and so does the TLS fingerprint that later anchors the disk
    export. Trusting whatever answers on first contact would hand both to
    anyone sitting in the path.
    """
    if host_key_known(host, port):
        return True
    keys = scan_host_keys(host, port)
    want = (getattr(args, "host_key_fingerprint", "") or "").strip()
    if want:
        # only the key that was actually named gets stored
        matched = [k for k in keys if k["fingerprint"] == want]
        if matched:
            trust_host_keys(matched)
            print(f"host key {matched[0]['type']} {want} matched and stored")
            return True
        print(f"host key of {host} does not match {want}:", file=sys.stderr)
        for k in keys:
            print(f"  offered {k['type']:<8} {k['fingerprint']}", file=sys.stderr)
        return False

    chosen = pick_host_key(keys)
    print(f"\n{host} is not in {KNOWN_HOSTS} yet. It offers:")
    for k in keys:
        mark = "  <- to be stored" if k is chosen else ""
        print(f"  {k['type']:<8} {k['bits']:>5} bit  {k['fingerprint']}{mark}")
    if getattr(args, "accept_host_key", False):
        trust_host_keys([chosen])
        print(f"accepted the {chosen['type']} key (--accept-host-key)")
        return True
    if not sys.stdin.isatty():
        print("refusing to trust it unattended - pass "
              "--host-key-fingerprint or --accept-host-key", file=sys.stderr)
        return False
    answer = input(f"Store the {chosen['type']} key? Compare its fingerprint "
                   "on the Xen host first. [y/N] ")
    if answer.strip().lower() not in ("y", "yes"):
        print("not stored")
        return False
    trust_host_keys([chosen])
    print(f"{chosen['type']} host key stored")
    return True


def _read_password(args) -> str:
    """Never from argv - the process list is readable by other users."""
    import getpass
    if getattr(args, "password_stdin", False):
        return sys.stdin.readline().rstrip("\n")
    env = os.environ.get("XENPROXY_PASSWORD")
    if env:
        return env
    return getpass.getpass("password: ")


def cmd_servers(args) -> int:
    cfg = config.load()
    rows = config.servers(cfg)
    if args.json:
        print(json.dumps([{k: v for k, v in s.items() if k != "password"}
                          for s in rows], indent=2))
        return 0
    if not rows:
        print("no servers configured")
        return 0
    for s in rows:
        auth = "key" if s.get("key") else ("password" if s.get("password") else "-")
        print(f"{s['name']:<20} {s.get('kind', 'xen'):<6} "
              f"{s.get('user', 'root')}@{s['host']}:"
              f"{s.get('port', 22):<6} auth={auth}")
    return 0


def cmd_add(args) -> int:
    cfg = config.load()
    kind = args.kind
    name = args.name or input("name: ").strip()
    host = args.host or input("host: ").strip()
    user = args.user or (input("user [root]: ").strip() or "root")
    if not _ensure_host_key(host, int(args.port), args):
        return 1
    password = _read_password(args)
    srv = {"name": name, "kind": kind, "host": host, "user": user,
           "password": password, "port": int(args.port),
           "key": args.key or "",
           "verify_tls": bool(args.verify_tls),
           "insecure_tls": bool(args.insecure_tls), "added": time.time()}
    print("checking the connection ...")
    try:
        info = _connect(srv, warn=False).probe()
    except HostKeyUnknown:
        raise
    except Exception as exc:  # noqa: BLE001
        print(f"failed: {exc}", file=sys.stderr)
        return 1
    print(f"ok - {info['hostname']} ({info['version']})")
    if kind == "xen":
        try:
            srv["tls_pin"] = _connect(srv, warn=False).fetch_tls_pin()
            print(f"pinned XAPI certificate {srv['tls_pin'][:16]}...")
        except Exception as exc:  # noqa: BLE001
            tail = ("disk exports will run unverified" if srv["insecure_tls"]
                    else "disk exports will refuse to run until this is fixed")
            print(f"warning: could not pin the XAPI certificate ({exc}); {tail}",
                  file=sys.stderr)
    config.upsert_server(cfg, srv)
    config.save(cfg)
    print(f"saved to {config.CONF_FILE}")
    return 0


def cmd_test(args) -> int:
    cfg = config.load()
    srv = _server(cfg, args.server)
    try:
        info = _connect(srv).probe()
    except HostKeyUnknown:
        raise
    except Exception as exc:  # noqa: BLE001
        print(f"{srv['name']}: {exc}", file=sys.stderr)
        return 1
    print(f"{srv['name']}: {info['hostname']} ({info['version']}), "
          f"{len(info['hosts'])} host(s) in the pool")
    return 0


def cmd_list(args) -> int:
    cfg = config.load()
    srv = _server(cfg, args.server)
    xen = _connect(srv)
    xen.probe()
    vms = xen.list_vms(include_templates=args.templates)
    if args.json:
        print(json.dumps(vms, indent=2))
        return 0
    print(f"{'NAME':<36} {'STATE':<10} {'RAM':>9} {'CPU':>4}  UUID")
    for v in vms:
        print(f"{v['name'][:35]:<36} {v['power_state']:<10} "
              f"{migrate.human(v['memory']):>9} {v['vcpus']:>4}  {v['uuid']}")
    return 0


def cmd_show(args) -> int:
    cfg = config.load()
    srv = _server(cfg, args.server)
    xen = _connect(srv)
    xen.probe()
    uuid = _resolve(xen, args.vm)
    detail = xen.vm_detail(uuid)
    if args.json:
        print(json.dumps(detail, indent=2))
        return 0
    print(f"{detail['name']}  [{detail['power_state']}]")
    print(f"  uuid      {detail['uuid']}")
    print(f"  cpu       {detail['vcpus']} vCPU")
    print(f"  memory    {migrate.human(detail['memory'])}")
    print(f"  firmware  {detail['firmware']}")
    for d in detail["disks"]:
        print(f"  disk      {d['device']}  {migrate.human(d['virtual_size'])}  "
              f"{d['sr_name']}")
    for n in detail["nics"]:
        vlan = f"  vlan {n['vlan']}" if n.get("vlan") else ""
        print(f"  nic       {n['device']}  {n['mac']}  "
              f"{n.get('network_name', '')}{vlan}")
    return 0


def _resolve(xen, needle: str) -> str:
    vms = xen.list_vms()
    for v in vms:
        if v["uuid"] == needle:
            return needle
    hits = [v for v in vms if v["name"] == needle]
    if not hits:
        hits = [v for v in vms if needle.lower() in v["name"].lower()]
    if not hits:
        sys.exit(f"no guest matches '{needle}'")
    if len(hits) > 1:
        sys.exit("ambiguous: " + ", ".join(v["name"] for v in hits[:6]))
    return hits[0]["uuid"]


def cmd_migrate(args) -> int:
    cfg = config.load()
    srv = _server(cfg, args.server)
    xen = _connect(srv)
    xen.probe()
    uuid = _resolve(xen, args.vm)
    detail = xen.vm_detail(uuid)

    plan = migrate.default_plan(detail, srv["name"])
    if args.mode not in migrate.modes_for(srv.get("kind", "xen")):
        sys.exit(f"mode '{args.mode}' is not available for a "
                 f"{srv.get('kind', 'xen')} source")
    plan["mode"] = args.mode
    plan["storage"] = args.storage or config.setting(cfg, "default_storage", "")
    plan["target_vmid"] = int(args.vmid) if args.vmid else plan["target_vmid"]
    plan["target_name"] = args.name or plan["target_name"]
    plan["net_mode"] = args.net
    plan["bridge"] = args.bridge or config.setting(cfg, "default_bridge", "")
    if args.vlan is not None:
        plan["vlan"] = args.vlan
    plan["start_after"] = not args.no_start
    plan["guest_prep"] = not args.no_prep
    plan["verify"] = not args.no_verify
    plan["onboot"] = bool(args.onboot)
    if not plan["storage"]:
        sys.exit("--storage is required (or set a default in the settings)")

    job = jobmod.Job(plan)
    mig = migrate.Migration(job, xen, plan)

    if args.dry_run:
        print(json.dumps({"plan": plan,
                          "disks": [{"device": d["device"],
                                     "size": d["virtual_size"]}
                                    for d in detail["disks"]]}, indent=2))
        return 0

    seen = 0
    import threading
    t = threading.Thread(target=mig.run, daemon=True)
    t.start()
    while t.is_alive() or seen < len(job.log):
        while seen < len(job.log):
            ts, kind, msg = job.log[seen]
            seen += 1
            mark = {"success": "+", "warning": "!", "danger": "x"}.get(kind, " ")
            print(f"[{time.strftime('%H:%M:%S', time.localtime(ts))}] {mark} {msg}",
                  flush=True)
        if job.bytes_total and job.state == jobmod.RUNNING and not args.quiet:
            pct = job.progress * 100
            print(f"\r    {pct:5.1f}%  {migrate.human(job.bytes_done)}"
                  f" / {migrate.human(job.bytes_total)}   ", end="", flush=True)
        time.sleep(0.4)
    print()
    if job.state == jobmod.DONE:
        print(f"done in {job.duration:.0f}s - VMID {plan['target_vmid']}")
        return 0
    print(f"failed: {job.error}", file=sys.stderr)
    return 1


def cmd_jobs(args) -> int:
    rows = jobmod.load_jobs(args.limit)
    if args.json:
        print(json.dumps(rows, indent=2))
        return 0
    if not rows:
        print("no jobs recorded")
        return 0
    for r in rows:
        when = time.strftime("%Y-%m-%d %H:%M", time.localtime(r.get("created", 0)))
        print(f"{r.get('id', ''):<24} {when}  {r.get('vm_name', ''):<28} "
              f"-> {r.get('vmid', ''):<8} {r.get('mode', ''):<9} {r.get('state', '')}")
    return 0


def cmd_update(args) -> int:
    st = update.status()
    text, _kind = update.describe(st)
    print(text)
    if args.check:
        return 0 if st["state"] in (update.NEWER, update.CURRENT,
                                    update.NONE) else 1
    if st["state"] != update.NEWER and not args.force:
        if st["state"] == update.UNREACHABLE:
            return 1
        print("pass --force to re-run the installer anyway")
        return 0
    if not args.yes:
        prompt = ("install it now? [y/N] " if st["state"] == update.NEWER
                  else f"re-run {update.install_command()}? [y/N] ")
        if input(prompt).strip().lower() not in ("y", "yes"):
            return 0
    import subprocess
    cp = subprocess.run(["bash", "-lc", update.install_command()], text=True)
    return cp.returncode


def cmd_serve(args) -> int:
    from .web import auth
    from .web.server import serve
    if args.new_token:
        print("new token: " + auth.reset())
    return serve(args.host, int(args.port), verbose=args.verbose)


def cmd_token(args) -> int:
    from .web import auth
    print(auth.reset() if args.new else auth.token())
    return 0


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="xenproxY",
        description="Migrate XenServer/XCP-ng and Incus guests to Proxmox VE.")
    ap.add_argument("--version", action="version", version=f"xenproxY {__version__}")
    sub = ap.add_subparsers(dest="cmd")

    p = sub.add_parser("servers", help="list configured source servers")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_servers)

    p = sub.add_parser("add", help="add a source server")
    p.add_argument("--name")
    p.add_argument("--kind", choices=sorted(KINDS), default="xen",
                   help="xen for XenServer/XCP-ng, incus for an Incus host")
    p.add_argument("--host")
    p.add_argument("--user")
    p.add_argument("--key", help="path to an SSH private key to use instead "
                                 "of a password")
    p.add_argument("--host-key-fingerprint",
                   help="expected SSH host key, e.g. SHA256:abc... - compared "
                        "against what the host offers instead of prompting")
    p.add_argument("--accept-host-key", action="store_true",
                   help="store whatever host key the server offers, without "
                        "confirming it")
    p.add_argument("--password-stdin", action="store_true",
                   help="read the password from stdin instead of prompting; "
                        "XENPROXY_PASSWORD is also honoured")
    p.add_argument("--port", default=22)
    p.add_argument("--verify-tls", action="store_true",
                   help="verify the XAPI certificate against the CA store "
                        "instead of pinning it")
    p.add_argument("--insecure-tls", action="store_true",
                   help="allow the disk export over an unverified channel "
                        "if the certificate cannot be pinned")
    p.set_defaults(func=cmd_add)

    p = sub.add_parser("test", help="check a stored connection")
    p.add_argument("--server")
    p.set_defaults(func=cmd_test)

    p = sub.add_parser("list", help="list guests on a source server")
    p.add_argument("--server")
    p.add_argument("--templates", action="store_true")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_list)

    p = sub.add_parser("show", help="show one guest in detail")
    p.add_argument("vm")
    p.add_argument("--server")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_show)

    p = sub.add_parser("migrate", help="migrate a guest without the TUI")
    p.add_argument("vm", help="name or UUID on the source side")
    p.add_argument("--server")
    p.add_argument("--mode", choices=list(migrate.MODES), default="offline")
    p.add_argument("--storage")
    p.add_argument("--vmid")
    p.add_argument("--name")
    p.add_argument("--net", choices=list(migrate.NET_MODES) + ["skip"],
                   default="keep")
    p.add_argument("--bridge")
    p.add_argument("--vlan", type=int)
    p.add_argument("--no-start", action="store_true")
    p.add_argument("--no-prep", action="store_true")
    p.add_argument("--no-verify", action="store_true")
    p.add_argument("--onboot", action="store_true")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--quiet", action="store_true")
    p.set_defaults(func=cmd_migrate)

    p = sub.add_parser("jobs", help="show migration history")
    p.add_argument("--limit", type=int, default=20)
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_jobs)

    p = sub.add_parser("serve", help="run the web interface")
    p.add_argument("--host", default="127.0.0.1",
                   help="address to bind; the default keeps it on this node")
    p.add_argument("--port", default=8800, type=int)
    p.add_argument("--new-token", action="store_true",
                   help="discard the stored token and issue a new one")
    p.add_argument("--verbose", action="store_true")
    p.set_defaults(func=cmd_serve)

    p = sub.add_parser("token", help="print the web interface token")
    p.add_argument("--new", action="store_true")
    p.set_defaults(func=cmd_token)

    p = sub.add_parser("update", help="check for and install a new version")
    p.add_argument("--check", action="store_true",
                   help="only report what is published, install nothing")
    p.add_argument("--force", action="store_true",
                   help="re-run the installer even when no newer version is "
                        "published - pulls the current main branch")
    p.add_argument("--yes", "-y", action="store_true")
    p.set_defaults(func=cmd_update)
    return ap


def _explain_host_key(exc: HostKeyUnknown) -> int:
    print(f"the SSH host key of {exc.host} has not been confirmed. It offers:",
          file=sys.stderr)
    for k in exc.keys:
        print(f"  {k['type']:<8} {k['bits']:>5} bit  {k['fingerprint']}",
              file=sys.stderr)
    print("run `xenproxY add` for this server to confirm and store it.",
          file=sys.stderr)
    return 1


def main(argv: list[str] | None = None) -> int:
    ap = build_parser()
    args = ap.parse_args(argv)
    if not getattr(args, "cmd", None):
        if not sys.stdout.isatty():
            ap.print_help()
            return 0
        if not pve.is_pve_node():
            print("warning: this host does not look like a Proxmox VE node",
                  file=sys.stderr)
        from .ui.app import App
        try:
            return App().run()
        except KeyboardInterrupt:
            return 130
    try:
        return args.func(args)
    except HostKeyUnknown as exc:
        return _explain_host_key(exc)
