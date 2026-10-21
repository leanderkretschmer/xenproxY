"""Incus adapter driven through the `incus` CLI over SSH.

The inventory comes back as JSON, so unlike the `xe` text format there is
nothing a crafted instance name can forge - the structure is decided by the
encoder, not by the content. Disks travel through `incus export <name> -`,
whose tar stream is unpacked while it arrives: nothing is staged, neither on
the Incus host nor here.
"""
from __future__ import annotations

import fcntl
import json
import os
import re
import shlex
import struct
import time

from .sshclient import SSHClient, SSHError
from .text import scrub

INCUS = "incus"

# Instance names are hostnames: letters, digits and hyphens. Anything else is
# refused rather than quoted, because a name that cannot be a name is a sign
# that the answer did not come from Incus.
NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9-]{0,62}$")
SNAP_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,62}$")

MODES = {
    "offline": "Stop the instance first - fully consistent, downtime for the copy",
    "snapshot": "Snapshot the running instance and copy that - no downtime, "
                "temporary objects on the Incus host",
    "live": "Copy the volume while the instance runs - no downtime, nothing "
            "changes on Incus, crash-consistent",
}

_UNITS = {"": 1, "b": 1,
          "kb": 10 ** 3, "mb": 10 ** 6, "gb": 10 ** 9, "tb": 10 ** 12,
          "kib": 1 << 10, "mib": 1 << 20, "gib": 1 << 30, "tib": 1 << 40}


def parse_size(value) -> int:
    """Incus writes sizes as 4GiB, 512MiB, 2GB or plain bytes."""
    if isinstance(value, (int, float)):
        return int(value)
    text = str(value or "").strip()
    m = re.match(r"^(\d+(?:\.\d+)?)\s*([A-Za-z]*)$", text)
    if not m:
        return 0
    factor = _UNITS.get(m.group(2).lower())
    if factor is None:
        return 0
    return int(float(m.group(1)) * factor)


def parse_cpu(value) -> int:
    """limits.cpu is either a count or a pin set such as `0-3` or `1,3`."""
    text = str(value or "").strip()
    if not text:
        return 0
    if text.isdigit():
        return int(text)
    total = 0
    for part in text.split(","):
        part = part.strip()
        if "-" in part:
            a, _, b = part.partition("-")
            if a.isdigit() and b.isdigit():
                total += max(0, int(b) - int(a) + 1)
        elif part.isdigit():
            total += 1
    return total


class TarMember:
    """A bounded reader over one file inside a tar stream.

    The header carries the authoritative byte count of the disk image, which
    is why the archive is walked here instead of being handed to `tar -xO`:
    the size is needed to allocate the target volume, and a stream that
    turns out to be longer than its own header claimed must be refused
    rather than written past the end of that volume.
    """

    BLOCK = 512

    def __init__(self, proc, wanted: str, *, limit: int = 1 << 42) -> None:
        self.proc = proc
        self.raw = proc.stdout
        self.name = ""
        self.size = 0
        self.left = 0
        self._closed = False
        self._err = b""
        if proc.stderr is not None:
            # Non-blocking, so whatever the source has complained about so
            # far can be picked up at any point without waiting for it to
            # finish - that text is usually the real explanation.
            flags = fcntl.fcntl(proc.stderr, fcntl.F_GETFL)
            fcntl.fcntl(proc.stderr, fcntl.F_SETFL, flags | os.O_NONBLOCK)
        try:
            self._seek(wanted, limit)
        except SSHError as exc:
            tail = self.stderr
            self.close()
            raise SSHError(f"{exc}{' - ' + tail if tail else ''}") from exc
        except Exception:
            self.close()
            raise

    # -- diagnostics --------------------------------------------------
    def _drain_err(self) -> None:
        pipe = self.proc.stderr
        if pipe is None:
            return
        try:
            while True:
                chunk = pipe.read()
                if not chunk:
                    break
                self._err += chunk
        except (OSError, ValueError):
            pass

    @property
    def stderr(self) -> str:
        """Whatever the remote command has said, without blocking on it."""
        self._drain_err()
        return scrub(self._err.decode("utf-8", "replace").strip())[:400]

    # -- tar walking --------------------------------------------------
    def _read_exact(self, n: int) -> bytes:
        buf = bytearray()
        while len(buf) < n:
            chunk = self.raw.read(n - len(buf))
            if not chunk:
                break
            buf += chunk
        return bytes(buf)

    def _seek(self, wanted: str, limit: int) -> None:
        skipped = 0
        while True:
            head = self._read_exact(self.BLOCK)
            if len(head) < self.BLOCK or head == b"\0" * self.BLOCK:
                raise SSHError(
                    f"the export stream contains no {wanted} - is "
                    "this instance a virtual machine?")
            name = head[0:100].split(b"\0", 1)[0].decode("utf-8", "replace")
            prefix = head[345:500].split(b"\0", 1)[0].decode("utf-8", "replace")
            if prefix:
                name = f"{prefix}/{name}"
            size = _octal(head[124:136])
            kind = head[156:157]
            padded = (size + self.BLOCK - 1) // self.BLOCK * self.BLOCK
            if kind in (b"0", b"\0", b"") and name.endswith(wanted):
                if size <= 0:
                    raise SSHError(f"{name} in the export is empty")
                if size > limit:
                    raise SSHError(
                        f"{name} claims {size} bytes, more than the "
                        f"{limit} byte ceiling - refusing it")
                self.name = scrub(name)
                self.size = size
                self.left = size
                return
            # not ours: step over the payload, including its padding
            skipped += padded
            if skipped > (1 << 30):
                raise SSHError("the export stream does not look like an "
                               "instance backup")
            left = padded
            while left > 0:
                chunk = self.raw.read(min(1 << 20, left))
                if not chunk:
                    raise SSHError("the export stream ended early")
                left -= len(chunk)

    # -- payload ------------------------------------------------------
    def readinto(self, view) -> int:
        if self.left <= 0:
            return 0
        n = self.raw.readinto(view[:min(len(view), self.left)])
        if n:
            self.left -= n
        return n or 0

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        proc = self.proc
        try:
            if proc.stdout:
                proc.stdout.close()
        except OSError:
            pass
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except Exception:  # noqa: BLE001
                proc.kill()
        self._drain_err()
        try:
            if proc.stderr:
                proc.stderr.close()
        except OSError:
            pass


def _octal(field: bytes) -> int:
    raw = field.split(b"\0", 1)[0].strip()
    if raw[:1] == b"\x80" or field[:1] == b"\x80":
        # GNU base-256 encoding for sizes that do not fit in 11 octal digits
        return struct.unpack(">q", b"\0" + field[5:12])[0] if len(field) >= 12 else 0
    try:
        return int(raw or b"0", 8)
    except ValueError:
        return 0


class IncusHost:
    kind = "incus"
    needs_password = False          # the disk stream rides the SSH session
    label = "Incus"
    MODES = MODES

    def __init__(self, ssh: SSHClient, **_ignored) -> None:
        self.ssh = ssh
        self._info: dict | None = None
        self._cache: dict = {}
        self.snapshot_name = ""
        self.temp_instance = ""

    # -- plumbing -----------------------------------------------------
    def _run(self, args: str, timeout: int = 60) -> str:
        cp = self.ssh.run(f"{INCUS} {args}", timeout=timeout)
        if cp.returncode != 0:
            raise SSHError(scrub((cp.stderr or cp.stdout).strip())
                           or "incus command failed")
        return cp.stdout

    def _json(self, args: str, timeout: int = 60):
        out = self._run(args, timeout=timeout)
        try:
            return json.loads(out)
        except ValueError as exc:
            raise SSHError("incus did not return JSON - "
                           f"{scrub(out.strip()[:120])}") from exc

    @staticmethod
    def _name(value: str) -> str:
        name = (value or "").strip()
        if not NAME_RE.match(name):
            raise SSHError(f"refusing an implausible instance name: {name!r}")
        return name

    # -- connection ---------------------------------------------------
    def probe(self) -> dict:
        ok, msg = self.ssh.test()
        if not ok:
            raise SSHError(msg)
        cp = self.ssh.run(
            "hostname; "
            "grep -m1 PRETTY_NAME /etc/os-release 2>/dev/null | cut -d= -f2 "
            "| tr -d '\"'; "
            f"command -v {INCUS} >/dev/null && echo INCUS_OK || echo INCUS_MISSING; "
            f"{INCUS} version 2>/dev/null | sed -n 's/^Server version: *//p'",
            timeout=30)
        lines = [l.strip() for l in cp.stdout.splitlines()]
        while len(lines) < 4:
            lines.append("")
        if "INCUS_MISSING" in lines[2]:
            raise SSHError("no `incus` client found - is this an Incus host?")
        ver = scrub(lines[3]) or "unknown"
        self._info = {
            "hostname": scrub(lines[0]),
            "os": scrub(lines[1]) or "unknown",
            "version": f"Incus {ver}".strip(),
            "hosts": [],
            "dom0_free_kb": 0,
        }
        return self._info

    # -- inventory ----------------------------------------------------
    def _instances(self, refresh: bool = False) -> list[dict]:
        if refresh or "list" not in self._cache:
            data = self._json("list --format json", timeout=90)
            if not isinstance(data, list):
                raise SSHError("unexpected answer from `incus list`")
            self._cache["list"] = data
        return self._cache["list"]

    def list_vms(self, include_templates: bool = False) -> list[dict]:
        out = []
        for inst in self._instances(refresh=True):
            if not isinstance(inst, dict):
                continue
            name = (inst.get("name") or "").strip()
            if not NAME_RE.match(name):
                continue
            if inst.get("type") != "virtual-machine":
                # containers have no block device to hand to KVM
                continue
            cfg = inst.get("expanded_config") or inst.get("config") or {}
            out.append({
                "uuid": name,
                "name": name,
                "description": scrub(inst.get("description") or ""),
                "power_state": _state(inst.get("status")),
                "memory": parse_size(cfg.get("limits.memory")),
                "vcpus": parse_cpu(cfg.get("limits.cpu")),
                "template": False,
                "tags": ",".join(inst.get("profiles") or []),
            })
        out.sort(key=lambda v: v["name"].lower())
        return out

    def _instance(self, name: str, refresh: bool = True) -> dict:
        name = self._name(name)
        data = self._json(f"query /1.0/instances/{shlex.quote(name)}?recursion=1",
                          timeout=60)
        if not isinstance(data, dict) or data.get("name") != name:
            raise SSHError(f"incus returned no record for {name}")
        return data

    def vm_detail(self, name: str) -> dict:
        inst = self._instance(name)
        cfg = inst.get("expanded_config") or {}
        devices = inst.get("expanded_devices") or {}
        if inst.get("type") != "virtual-machine":
            raise SSHError(f"{name} is a container, not a virtual machine - "
                           "xenproxY migrates virtual machines")
        snaps = [s.get("name", "") for s in (inst.get("snapshots") or [])
                 if isinstance(s, dict)]
        return {
            "uuid": name,
            "name": name,
            "description": scrub(inst.get("description") or ""),
            "power_state": _state(inst.get("status")),
            "memory": parse_size(cfg.get("limits.memory")),
            "vcpus": parse_cpu(cfg.get("limits.cpu")),
            "cores_per_socket": 1,
            "hvm": True,
            # Incus boots VMs through OVMF unless CSM puts them back on a
            # legacy BIOS, and Proxmox has to be told the same thing or the
            # guest will not find its bootloader.
            "firmware": "bios" if str(
                cfg.get("security.csm", "")).lower() in ("true", "1", "yes")
                else "uefi",
            "snapshots": snaps,
            "disks": self._disks(name, inst, devices),
            "nics": self._nics(cfg, devices),
            "extra_disks": [k for k, d in devices.items()
                            if isinstance(d, dict) and d.get("type") == "disk"
                            and d.get("path") not in ("/", None)
                            and d.get("pool")],
        }

    def _disks(self, name: str, inst: dict, devices: dict) -> list[dict]:
        root = devices.get("root") if isinstance(devices.get("root"), dict) else {}
        pool = str(root.get("pool") or "")
        size = parse_size(root.get("size"))
        if not size:
            size = self._volume_size(pool, name)
        if not size:
            state = inst.get("state") or {}
            disk = (state.get("disk") or {}).get("root") or {}
            size = parse_size(disk.get("total") or disk.get("usage"))
        return [{
            "vdi": name,
            "device": "root",
            "label": "root",
            "virtual_size": size,
            "physical_size": size,
            "sr": pool,
            "sr_name": pool or "default",
            "sm_config": {},
            "bootable": True,
            "userdevice": "0",
        }]

    def _volume_size(self, pool: str, name: str) -> int:
        if not pool or not NAME_RE.match(pool):
            return 0
        try:
            out = self._run(
                f"storage volume get {shlex.quote(pool)} "
                f"virtual-machine/{shlex.quote(name)} size", timeout=45)
        except SSHError:
            return 0
        return parse_size(out.strip())

    @staticmethod
    def _nics(cfg: dict, devices: dict) -> list[dict]:
        nics = []
        for key, dev in sorted(devices.items()):
            if not isinstance(dev, dict) or dev.get("type") != "nic":
                continue
            iface = dev.get("name") or key
            # A NIC that was never given an explicit address carries its
            # generated one in volatile config, not in the device.
            mac = (dev.get("hwaddr")
                   or cfg.get(f"volatile.{key}.hwaddr")
                   or cfg.get(f"volatile.{iface}.hwaddr") or "")
            vlan = dev.get("vlan")
            nics.append({
                "uuid": key,
                "device": scrub(str(iface)),
                "mac": scrub(str(mac)).lower(),
                "mtu": int(parse_size(dev.get("mtu")) or 1500),
                "network": scrub(str(dev.get("network")
                                     or dev.get("parent") or "")),
                "network_name": scrub(str(dev.get("network")
                                          or dev.get("parent") or "")),
                "vlan": int(vlan) if str(vlan or "").isdigit() else None,
            })
        return nics

    def sr_list(self) -> list[dict]:
        try:
            pools = self._json("storage list --format json", timeout=45)
        except SSHError:
            return []
        out = []
        for p in pools if isinstance(pools, list) else []:
            if not isinstance(p, dict):
                continue
            cfg = p.get("config") or {}
            out.append({"uuid": scrub(str(p.get("name", ""))),
                        "name": scrub(str(p.get("name", ""))),
                        "type": scrub(str(p.get("driver", ""))),
                        "content": "images",
                        "size": parse_size(cfg.get("size")),
                        "used": 0})
        return out

    # -- power --------------------------------------------------------
    def power_state(self, name: str) -> str:
        try:
            inst = self._instance(name)
        except SSHError:
            return "unknown"
        return _state(inst.get("status"))

    def shutdown(self, name: str, force: bool = False, timeout: int = 600) -> None:
        q = shlex.quote(self._name(name))
        verb = f"stop {q}" + (" --force" if force else " --timeout 300")
        self._run(verb, timeout=timeout)

    def start(self, name: str, timeout: int = 300) -> None:
        self._run(f"start {shlex.quote(self._name(name))}", timeout=timeout)

    def snapshot(self, name: str, label: str, timeout: int = 900) -> str:
        name = self._name(name)
        if not SNAP_RE.match(label):
            raise SSHError(f"refusing an implausible snapshot name: {label!r}")
        self._run(f"snapshot create {shlex.quote(name)} {shlex.quote(label)}",
                  timeout=timeout)
        return label

    def snapshot_destroy(self, label: str, instance: str = "") -> None:
        inst = self._name(instance or self._snap_owner)
        if not SNAP_RE.match(label):
            return
        self._run(f"snapshot delete {shlex.quote(inst)} {shlex.quote(label)}",
                  timeout=300)

    _snap_owner = ""

    # -- source preparation -------------------------------------------
    def prepare(self, plan: dict, detail: dict, log) -> str:
        """Put the source into the state the chosen mode promises.

        Returns a short note for the job step; `release` undoes whatever was
        created here.
        """
        name = self._name(plan["vm_uuid"])
        mode = plan.get("mode", "offline")
        for disk in detail.get("disks", []):
            disk["export_source"] = name
        if detail.get("extra_disks"):
            log(f"{len(detail['extra_disks'])} extra block device(s) are "
                "attached as separate Incus volumes and are not part of the "
                "instance export - copy them by hand", "warning")

        if mode == "offline":
            state = self.power_state(name)
            if state == "running":
                log("stopping the source instance")
                self.shutdown(name, timeout=900)
                for _ in range(90):
                    if self.power_state(name) != "running":
                        break
                    time.sleep(2)
                else:
                    raise SSHError("the source instance did not stop")
                log("source instance stopped", "success")
            else:
                log("source instance was already stopped")
            return "stopped"

        if mode == "snapshot":
            label = f"xenproxy-{time.strftime('%Y%m%d-%H%M%S')}"
            log(f"creating snapshot {label}")
            self.snapshot(name, label)
            self.snapshot_name = label
            self._snap_owner = name
            # `incus export` always exports the instance as it stands now, so
            # the snapshot is copied into a throwaway instance and that is
            # what gets exported.
            tmp = f"{name[:40]}-xenproxy-tmp"
            if not NAME_RE.match(tmp):
                raise SSHError("cannot derive a temporary instance name")
            log(f"copying the snapshot into {tmp}")
            # no --instance-only here: copying *from* a snapshot already
            # names a single point in time, and incus refuses the flag
            self._run(f"copy {shlex.quote(name)}/{shlex.quote(label)} "
                      f"{shlex.quote(tmp)}", timeout=3600)
            self.temp_instance = tmp
            for disk in detail.get("disks", []):
                disk["export_source"] = tmp
            log(f"snapshot staged as {tmp}", "success")
            return label

        log("exporting the volume while the instance runs - nothing on the "
            "Incus host is changed")
        log("the image is crash-consistent; the guest will replay its journal",
            "warning")
        return "no change on Incus"

    def release(self, ok: bool, log) -> None:
        if self.temp_instance:
            try:
                self._run(f"delete {shlex.quote(self.temp_instance)} --force",
                          timeout=900)
                log(f"temporary instance {self.temp_instance} removed", "success")
            except SSHError as exc:
                log(f"could not remove {self.temp_instance}: {exc}", "warning")
            self.temp_instance = ""
        if self.snapshot_name:
            try:
                self.snapshot_destroy(self.snapshot_name)
                log("snapshot removed from Incus", "success")
            except SSHError as exc:
                log(f"could not remove the snapshot: {exc}", "warning")
            self.snapshot_name = ""

    # -- disk stream --------------------------------------------------
    def open_disk(self, disk: dict, log=None):
        """A raw byte stream of the instance's root volume.

        `incus export` writes an uncompressed tar to stdout; the member
        `backup/virtual-machine.img` inside it is the raw disk. Compression is
        turned off on purpose - the bytes go onto a local volume, so paying
        for gzip on the source CPU would only make it slower.
        """
        ident = self._name(disk.get("export_source") or disk.get("vdi"))
        # --quiet matters: without it the progress bar is written to the
        # same stdout as the tarball and corrupts the stream.
        cmd = (f"{INCUS} --quiet export {shlex.quote(ident)} - "
               "--instance-only --compression none")
        proc = self.ssh.popen_stdout(cmd)
        return TarMember(proc, "virtual-machine.img")

    def export_available(self, disk: dict) -> tuple[bool, str]:
        """Cheap probe: open the stream, read a little, close again."""
        try:
            stream = self.open_disk(disk)
        except SSHError as exc:
            return False, str(exc)
        try:
            buf = bytearray(512)
            if stream.readinto(memoryview(buf)) < 512:
                return False, "short read from incus export"
            return True, f"ok ({stream.name}, {stream.size} bytes)"
        finally:
            stream.close()

    # -- parity with the Xen adapter ----------------------------------
    def pinned(self) -> bool:
        return True       # the SSH session is the authenticated channel

    def fetch_tls_pin(self) -> str:
        return ""


def _state(status) -> str:
    return {"running": "running", "stopped": "halted", "frozen": "suspended",
            "error": "error"}.get(str(status or "").lower(), "unknown")
