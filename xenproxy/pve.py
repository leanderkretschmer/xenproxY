"""Proxmox VE adapter - runs locally on the PVE node."""
from __future__ import annotations

import json
import os
import re
import shlex
import subprocess

QM = "/usr/sbin/qm"
PVESM = "/usr/sbin/pvesm"
PVESH = "/usr/bin/pvesh"


class PVEError(RuntimeError):
    pass


def run(argv: list[str], *, timeout: int = 120, check: bool = True,
        text_input: str | None = None) -> subprocess.CompletedProcess:
    cp = subprocess.run(argv, capture_output=True, text=True, timeout=timeout,
                        input=text_input,
                        stdin=None if text_input is not None else subprocess.DEVNULL)
    if check and cp.returncode != 0:
        raise PVEError((cp.stderr or cp.stdout or " ".join(argv)).strip())
    return cp


def sh(cmd: str, *, timeout: int = 120, check: bool = True) -> subprocess.CompletedProcess:
    cp = subprocess.run(cmd, shell=True, capture_output=True, text=True,
                        timeout=timeout, stdin=subprocess.DEVNULL)
    if check and cp.returncode != 0:
        raise PVEError((cp.stderr or cp.stdout or cmd).strip())
    return cp


def is_pve_node() -> bool:
    return os.path.exists(QM) and os.path.isdir("/etc/pve")


def node_name() -> str:
    try:
        return run(["hostname"], timeout=10).stdout.strip()
    except Exception:
        return "localhost"


def version() -> str:
    try:
        return run(["/usr/bin/pveversion"], timeout=20).stdout.strip().splitlines()[0]
    except Exception:
        return "unknown"


# -- storage ---------------------------------------------------------
def storages(*, images_only: bool = True) -> list[dict]:
    """Storages usable for VM disks, with free space."""
    out = []
    try:
        cp = run([PVESM, "status"], timeout=60)
    except Exception:
        return out
    cfg = _storage_cfg()
    for line in cp.stdout.splitlines()[1:]:
        f = line.split()
        if len(f) < 7:
            continue
        name, stype, status = f[0], f[1], f[2]
        total, used, avail = int(f[3]), int(f[4]), int(f[5])
        content = cfg.get(name, {}).get("content", "")
        if images_only and "images" not in content:
            continue
        if status != "active":
            continue
        out.append({
            "name": name, "type": stype, "content": content,
            "total": total * 1024, "used": used * 1024, "avail": avail * 1024,
            "sparse": cfg.get(name, {}).get("sparse", "") == "1",
            "nodes": cfg.get(name, {}).get("nodes", ""),
        })
    out.sort(key=lambda s: -s["avail"])
    return out


def dir_storages() -> list[dict]:
    """Storages backed by a filesystem path - usable for staging."""
    cfg = _storage_cfg()
    out = []
    for name, c in cfg.items():
        if c.get("__type__") in ("dir", "nfs", "cifs") and c.get("path"):
            out.append({"name": name, "path": c["path"]})
    return out


def _storage_cfg() -> dict:
    cfg: dict = {}
    cur = None
    try:
        with open("/etc/pve/storage.cfg", encoding="utf-8") as fh:
            for line in fh:
                m = re.match(r"^(\w+):\s*(\S+)", line)
                if m:
                    cur = m.group(2)
                    cfg[cur] = {"__type__": m.group(1)}
                    continue
                m = re.match(r"^\s+(\w+)\s+(.*)$", line.rstrip())
                if m and cur:
                    cfg[cur][m.group(1)] = m.group(2).strip()
    except OSError:
        pass
    return cfg


def storage_path(volid: str) -> str:
    return run([PVESM, "path", volid], timeout=60).stdout.strip()


def alloc_disk(storage: str, vmid: int, name: str, size_bytes: int) -> str:
    size_kb = (size_bytes + 1023) // 1024
    cp = run([PVESM, "alloc", storage, str(vmid), name, str(size_kb)], timeout=600)
    m = re.search(r"'([^']+)'", cp.stdout)
    return m.group(1) if m else f"{storage}:{name}"


def free_disk(volid: str) -> None:
    run([PVESM, "free", volid], timeout=300, check=False)


# -- network ---------------------------------------------------------
def bridges() -> list[dict]:
    out = []
    vlan_aware = set()
    cur = None
    try:
        with open("/etc/network/interfaces", encoding="utf-8") as fh:
            for line in fh:
                m = re.match(r"^iface\s+(\S+)", line)
                if m:
                    cur = m.group(1)
                if cur and re.match(r"^\s+bridge-vlan-aware\s+yes", line):
                    vlan_aware.add(cur)
    except OSError:
        pass
    try:
        cp = run(["ip", "-br", "link", "show", "type", "bridge"], timeout=20)
        for line in cp.stdout.splitlines():
            f = line.split()
            if not f:
                continue
            name = f[0].split("@")[0]
            out.append({"name": name, "state": f[1] if len(f) > 1 else "",
                        "vlan_aware": name in vlan_aware})
    except Exception:
        pass
    out.sort(key=lambda b: b["name"])
    return out


def bridge_in_use(bridge: str) -> int:
    n = 0
    for d in ("/etc/pve/qemu-server", "/etc/pve/lxc"):
        try:
            for fn in os.listdir(d):
                if not fn.endswith(".conf"):
                    continue
                with open(os.path.join(d, fn), encoding="utf-8") as fh:
                    if f"bridge={bridge}" in fh.read():
                        n += 1
        except OSError:
            pass
    return n


# -- VM lifecycle ----------------------------------------------------
def next_vmid() -> int:
    try:
        return int(run([PVESH, "get", "/cluster/nextid"], timeout=30).stdout.strip().strip('"'))
    except Exception:
        return 100


def vmid_free(vmid: int) -> bool:
    for base in ("/etc/pve/nodes",):
        for root, _dirs, files in os.walk(base):
            if f"{vmid}.conf" in files:
                return False
    return True


def vm_exists(vmid: int) -> bool:
    return not vmid_free(vmid)


def create_vm(vmid: int, opts: dict) -> None:
    argv = [QM, "create", str(vmid)]
    for k, v in opts.items():
        if v is None or v == "":
            continue
        argv += [f"--{k}", str(v)]
    run(argv, timeout=180)


def set_vm(vmid: int, opts: dict) -> None:
    argv = [QM, "set", str(vmid)]
    for k, v in opts.items():
        if v is None or v == "":
            continue
        argv += [f"--{k}", str(v)]
    run(argv, timeout=180)


def destroy_vm(vmid: int, purge: bool = True) -> None:
    argv = [QM, "destroy", str(vmid)]
    if purge:
        argv += ["--purge", "--destroy-unreferenced-disks", "1"]
    run(argv, timeout=600, check=False)


def start_vm(vmid: int) -> None:
    run([QM, "start", str(vmid)], timeout=300)


def shutdown_vm(vmid: int, timeout: int = 180) -> None:
    run([QM, "shutdown", str(vmid), "--timeout", str(timeout)],
        timeout=timeout + 60, check=False)


def vm_status(vmid: int) -> str:
    try:
        return run([QM, "status", str(vmid)], timeout=30).stdout.strip().split()[-1]
    except Exception:
        return "unknown"


def vm_config(vmid: int) -> dict:
    try:
        cp = run([QM, "config", str(vmid)], timeout=30)
    except PVEError:
        return {}
    cfg = {}
    for line in cp.stdout.splitlines():
        if ":" in line:
            k, v = line.split(":", 1)
            cfg[k.strip()] = v.strip()
    return cfg


def list_vms() -> list[dict]:
    try:
        cp = run([QM, "list"], timeout=60)
    except PVEError:
        return []
    vms = []
    for line in cp.stdout.splitlines()[1:]:
        f = line.split()
        if len(f) >= 3 and f[0].isdigit():
            vms.append({"vmid": int(f[0]), "name": f[1], "status": f[2]})
    return vms


def agent_ping(vmid: int) -> bool:
    cp = run([QM, "agent", str(vmid), "ping"], timeout=30, check=False)
    return cp.returncode == 0


def guest_exec(vmid: int, argv: list[str], timeout: int = 60) -> str:
    cp = run([QM, "guest", "exec", str(vmid), "--timeout", str(timeout), "--"] + argv,
             timeout=timeout + 30, check=False)
    try:
        data = json.loads(cp.stdout)
    except (json.JSONDecodeError, ValueError):
        return cp.stdout
    return (data.get("out-data", "") or "") + (data.get("err-data", "") or "")


def qemu_img_convert(src: str, dst: str, src_fmt: str = "vpc",
                     dst_fmt: str = "raw", timeout: int = 14400) -> None:
    run(["qemu-img", "convert", "-f", src_fmt, "-O", dst_fmt, "-W", src, dst],
        timeout=timeout)


def qemu_img_info(path: str, fmt: str | None = None) -> dict:
    argv = ["qemu-img", "info", "--output=json"]
    if fmt:
        argv += ["-f", fmt]
    argv.append(path)
    try:
        return json.loads(run(argv, timeout=120).stdout)
    except (PVEError, json.JSONDecodeError, ValueError):
        return {}
