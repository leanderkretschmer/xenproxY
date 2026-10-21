"""Migration engine: XCP-ng or Incus guest -> Proxmox VE guest.

The source is reached through an adapter - `xen.XenHost` or
`incus.IncusHost` - and every step below goes through the same handful of
adapter methods, so neither side knows about the other.
"""
from __future__ import annotations

import hashlib
import os
import re
import shlex
import subprocess
import time

from . import pve
from .jobs import DONE, FAILED, RUNNING, SKIPPED, Job
from .safeio import SafeRoot, UnsafePath
from .incus import MODES as INCUS_MODES
from .sshclient import SSHError
from .xen import valid_mac

CHUNK = 4 << 20
_BRIDGE = re.compile(r"^[A-Za-z][A-Za-z0-9_.-]{0,14}$")
MODES = {
    "offline": "Shut the guest down first - fully consistent, downtime for the copy",
    "snapshot": "Snapshot the running guest, copy that - no downtime, temporary objects on Xen",
    "live": "Copy the volume while the guest runs - no downtime, no change on Xen, crash-consistent",
}
SOURCE_MODES = {"xen": MODES, "incus": INCUS_MODES}


def modes_for(kind: str) -> dict:
    """The copy modes the given source kind can actually deliver."""
    return SOURCE_MODES.get(kind or "xen", MODES)


NET_MODES = {
    "keep": "Reuse MAC and map the Xen network onto a Proxmox bridge",
    "new": "Fresh MAC and a bridge/VLAN you pick",
    "later": "Attach the NIC with the link down - configure it when you cut over",
}

STEPS = [
    ("connect", "Connect to the source host"),
    ("preflight", "Pre-flight checks"),
    ("source", "Prepare the source"),
    ("disks", "Copy disks"),
    ("createvm", "Create the Proxmox guest"),
    ("guestprep", "Adapt the guest for KVM"),
    ("startvm", "Start the guest"),
    ("cleanup", "Clean up"),
]


def human(n: float) -> str:
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if abs(n) < 1024:
            return f"{n:.1f} {unit}" if unit != "B" else f"{int(n)} B"
        n /= 1024
    return f"{n:.1f} PiB"


def default_plan(detail: dict, server: str) -> dict:
    """Sensible defaults derived from the source guest."""
    nic = (detail.get("nics") or [{}])[0]
    return {
        "server": server,
        "vm_uuid": detail.get("uuid", ""),
        "vm_name": detail.get("name", ""),
        "mode": "offline" if detail.get("power_state") == "running" else "offline",
        "target_vmid": pve.next_vmid(),
        "target_name": re.sub(r"[^A-Za-z0-9.-]+", "-", detail.get("name", "vm")).strip("-")[:60] or "vm",
        "storage": "",
        "cores": detail.get("vcpus") or 2,
        "sockets": 1,
        "memory": max(512, (detail.get("memory") or 1 << 30) // (1 << 20)),
        "net_mode": "keep",
        "bridge": "",
        "vlan": nic.get("vlan"),
        "mac": nic.get("mac", ""),
        "bios": "ovmf" if detail.get("firmware") == "uefi" else "seabios",
        "machine": "pc",
        "scsihw": "virtio-scsi-single",
        "cpu": "host",
        "start_after": True,
        "guest_prep": True,
        "verify": True,
        "source_restart": False,
        "onboot": False,
    }


class Migration:
    def __init__(self, job: Job, src, plan: dict) -> None:
        self.job = job
        self.src = src
        self.plan = plan
        self.detail: dict = {}
        self.volumes: list[str] = []
        self.created_vmid = 0

    # -- helpers -----------------------------------------------------
    def _check_cancel(self) -> None:
        if self.job.cancel_requested:
            raise RuntimeError("cancelled by operator")

    def log(self, msg: str, kind: str = "info") -> None:
        self.job.say(msg, kind)

    # -- entry point -------------------------------------------------
    def run(self) -> bool:
        j = self.job
        j.add_steps(STEPS)
        j.state = RUNNING
        j.save()
        try:
            self._connect()
            self._preflight()
            self._source_prep()
            self._copy_disks()
            self._create_vm()
            self._guest_prep()
            self._start_vm()
            self._cleanup(ok=True)
            j.state = DONE
            j.finished = time.time()
            self.log(f"migration finished in {j.duration:.0f}s", "success")
            j.save()
            return True
        except Exception as exc:  # noqa: BLE001 - surfaced in the UI
            j.error = str(exc)
            j.state = FAILED
            j.finished = time.time()
            self.log(f"failed: {exc}", "danger")
            for s in j.steps:
                if s.state == RUNNING:
                    s.state = FAILED
                    s.ended = time.time()
            try:
                self._cleanup(ok=False)
            except Exception as cexc:  # noqa: BLE001
                self.log(f"cleanup problem: {cexc}", "warning")
            j.save()
            return False

    # -- steps -------------------------------------------------------
    def _connect(self) -> None:
        j = self.job
        j.begin("connect")
        info = self.src.probe()
        self.log(f"{info['hostname']} - {info['version']}")
        self.detail = self.src.vm_detail(self.plan["vm_uuid"])
        disks = self.detail.get("disks", [])
        j.bytes_total = sum(d["virtual_size"] for d in disks)
        self.log(f"{self.detail['name']}: {len(disks)} disk(s), "
                 f"{human(j.bytes_total)}, {self.detail['vcpus']} vCPU, "
                 f"{human(self.detail['memory'])} RAM")
        j.finish("connect", DONE, info["hostname"])

    def _preflight(self) -> None:
        j = self.job
        j.begin("preflight")
        p = self.plan

        if not pve.is_pve_node():
            raise RuntimeError("this does not look like a Proxmox VE node")
        vmid = int(p["target_vmid"])
        if not pve.vmid_free(vmid):
            raise RuntimeError(f"VMID {vmid} is already taken")

        stores = {s["name"]: s for s in pve.storages()}
        st = stores.get(p["storage"])
        if not st:
            raise RuntimeError(f"storage '{p['storage']}' is unavailable or holds no images")
        need = j.bytes_total
        if st["avail"] < need:
            raise RuntimeError(
                f"{st['name']} has {human(st['avail'])} free, {human(need)} needed")
        self.log(f"storage {st['name']} ({st['type']}): {human(st['avail'])} free")

        if p["net_mode"] != "skip" and p.get("bridge"):
            names = {b["name"] for b in pve.bridges()}
            if p["bridge"] not in names:
                raise RuntimeError(f"bridge '{p['bridge']}' does not exist")

        if not self.detail.get("disks"):
            raise RuntimeError("the source guest has no disks attached")

        # Only the XAPI export needs one; the Incus stream rides the SSH
        # session that is already authenticated.
        if getattr(self.src, "needs_password", True) and not self.src.ssh.password:
            raise RuntimeError("no host password stored - the XAPI disk export needs it")

        state = self.src.power_state(p["vm_uuid"])
        self.log(f"source power state: {state}")
        if p["mode"] == "live" and state != "running":
            self.log("live mode on a halted guest behaves like offline", "warning")
        if p["mode"] == "snapshot" and state != "running":
            self.log("snapshot mode on a halted guest - snapshot still works", "warning")
        j.finish("preflight", DONE, f"VMID {vmid} free, storage ok")

    def _source_prep(self) -> None:
        j = self.job
        j.begin("source")
        note = self.src.prepare(self.plan, self.detail, self.log) or ""
        j.finish("source", DONE, note)

    # -- disk copy ---------------------------------------------------
    def _copy_disks(self) -> None:
        j = self.job
        p = self.plan
        j.begin("disks")
        for idx, disk in enumerate(self.detail["disks"]):
            self._check_cancel()
            j.current_disk = f"{disk['device']} ({human(disk['virtual_size'])})"
            self.log(f"opening the source stream for {disk['device']}")
            stream = self.src.open_disk(disk, self.log)
            try:
                # The stream knows its own length - a tar header or the VDI
                # record - and that is what the target volume is sized from,
                # so a disk the inventory guessed wrong about still lands.
                size = int(getattr(stream, "size", 0) or disk["virtual_size"])
                if not size:
                    raise RuntimeError(
                        f"the source did not state a size for {disk['device']}")
                if size != disk["virtual_size"]:
                    self.log(f"{disk['device']} is {human(size)}, the inventory "
                             f"said {human(disk['virtual_size'])}")
                    j.bytes_total += size - disk["virtual_size"]
                    disk["virtual_size"] = size
                name = f"vm-{p['target_vmid']}-disk-{idx}"
                j.current_disk = f"{disk['device']} ({human(size)})"
                self.log(f"allocating {name} on {p['storage']} - {human(size)}")
                volid = pve.alloc_disk(p["storage"], int(p["target_vmid"]),
                                       name, size)
                self.volumes.append(volid)
                path = pve.storage_path(volid)
                disk["volid"] = volid
                disk["target_path"] = path
                self._stream(stream, path, size, disk)
            finally:
                stream.close()
        j.current_disk = ""
        j.finish("disks", DONE, f"{len(self.detail['disks'])} disk(s) copied")

    def _stream(self, stream, path: str, size: int, disk: dict) -> None:
        """Pull the source stream straight onto the target volume."""
        j = self.job
        self.log(f"streaming {stream.name} -> {os.path.basename(path)}")
        digest = hashlib.md5()
        fd = os.open(path, os.O_WRONLY)
        start = time.time()
        written = 0
        base = j.bytes_done
        try:
            dev_size = os.lseek(fd, 0, os.SEEK_END)
            os.lseek(fd, 0, os.SEEK_SET)
            if dev_size and dev_size < size:
                raise RuntimeError(
                    f"target volume is {human(dev_size)}, source needs {human(size)}")
            buf = bytearray(CHUNK)
            view = memoryview(buf)
            last = 0.0
            while written < size:
                self._check_cancel()
                # never accept more than the disk claims to be - a stream that
                # keeps going would otherwise run off the end of the volume
                want = min(CHUNK, size - written)
                n = stream.readinto(view[:want])
                if not n:
                    break
                chunk = view[:n]
                if self.plan.get("verify"):
                    digest.update(chunk)
                off = 0
                while off < n:
                    off += os.write(fd, chunk[off:])
                written += n
                j.bytes_done = base + written
                now = time.time()
                if now - last > 0.5:
                    elapsed = max(0.001, now - start)
                    j.rate = written / elapsed
                    last = now
                    j.save(throttle=True)
            if stream.readinto(memoryview(bytearray(1))):
                raise RuntimeError(
                    f"the export sent more than the declared {human(size)}")
            os.fsync(fd)
        finally:
            os.close(fd)

        elapsed = max(0.001, time.time() - start)
        self.log(f"{human(written)} in {elapsed:.0f}s ({human(written / elapsed)}/s)")
        if written != size:
            tail = getattr(stream, "stderr", "") or ""
            raise RuntimeError(
                f"short transfer: got {human(written)}, expected {human(size)}"
                + (f" - source said: {tail}" if tail else ""))
        j.bytes_done = base + size
        if self.plan.get("verify"):
            disk["md5"] = digest.hexdigest()
            self._verify(path, size, digest.hexdigest())

    def _verify(self, path: str, size: int, expect: str) -> None:
        """Read the written volume back and compare it with the stream hash."""
        self.log("verifying the written volume")
        digest = hashlib.md5()
        fd = os.open(path, os.O_RDONLY)
        try:
            left = size
            buf = bytearray(CHUNK)
            view = memoryview(buf)
            while left > 0:
                self._check_cancel()
                n = os.readv(fd, [view[:min(CHUNK, left)]])
                if not n:
                    break
                digest.update(view[:n])
                left -= n
        finally:
            os.close(fd)
        got = digest.hexdigest()
        if got != expect:
            raise RuntimeError(f"checksum mismatch: wrote {expect}, read back {got}")
        self.log(f"md5 {got} - written volume matches the stream", "success")

    # -- target guest ------------------------------------------------
    def _net_option(self) -> str | None:
        """Build the net0 property string from validated parts only.

        Each field lands in a comma-separated `qm` option, so anything that
        is not exactly what it claims to be is dropped rather than passed on.
        """
        p = self.plan
        if p["net_mode"] == "skip":
            return None
        bridge = str(p.get("bridge") or "")
        if not _BRIDGE.match(bridge):
            raise RuntimeError(f"refusing an implausible bridge name: {bridge!r}")
        parts = ["virtio"]
        if p["net_mode"] == "keep":
            mac = valid_mac(p.get("mac", ""))
            if not mac:
                self.log("the source MAC is malformed - letting Proxmox assign one",
                         "warning")
            else:
                parts = [f"virtio={mac.upper()}"]
        parts.append(f"bridge={bridge}")
        vlan = p.get("vlan")
        if vlan not in (None, ""):
            try:
                tag = int(vlan)
            except (TypeError, ValueError):
                raise RuntimeError(f"refusing an implausible VLAN tag: {vlan!r}")
            if not 1 <= tag <= 4094:
                raise RuntimeError(f"VLAN tag out of range: {tag}")
            parts.append(f"tag={tag}")
        if p["net_mode"] == "later":
            parts.append("link_down=1")
        return ",".join(parts)

    def _create_vm(self) -> None:
        j = self.job
        p = self.plan
        j.begin("createvm")
        vmid = int(p["target_vmid"])
        opts = {
            "name": p["target_name"],
            "ostype": "l26",
            "machine": p.get("machine") or "pc",
            "bios": p.get("bios") or "seabios",
            "cpu": p.get("cpu") or "host",
            "sockets": p.get("sockets") or 1,
            "cores": p.get("cores") or 2,
            "memory": p.get("memory") or 2048,
            "balloon": 0,
            "numa": 0,
            "scsihw": p.get("scsihw") or "virtio-scsi-single",
            "vga": "std",
            "agent": 0,
            "onboot": 1 if p.get("onboot") else 0,
            "tags": "xenproxy",
            "description": (
                f"Migrated from {getattr(self.src, 'label', 'source')} host "
                f"{self.src.ssh.host}.\n"
                f"Source guest: {self.detail.get('name')} "
                f"({self.plan['vm_uuid']})\nMode: {p['mode']}"),
        }
        net = self._net_option()
        if net:
            opts["net0"] = net
        pve.create_vm(vmid, opts)
        self.created_vmid = vmid
        self.log(f"created VM {vmid} ({p['target_name']})", "success")

        if opts["bios"] == "ovmf":
            # OVMF needs somewhere to keep its boot entries. Without an EFI
            # disk the firmware falls back to \EFI\BOOT\BOOTX64.EFI and a
            # guest that only installed its own entry never comes up.
            efi = f"{p['storage']}:1,efitype=4m,pre-enrolled-keys=0"
            try:
                pve.set_vm(vmid, {"efidisk0": efi})
                self.log("added an EFI disk for the UEFI firmware")
            except Exception as exc:  # noqa: BLE001
                self.log(f"could not add an EFI disk: {exc}", "warning")

        for idx, disk in enumerate(self.detail["disks"]):
            spec = f"{disk['volid']},iothread=1,discard=on,ssd=1"
            pve.set_vm(vmid, {f"scsi{idx}": spec})
            self.log(f"attached scsi{idx} = {disk['volid']}")
        pve.set_vm(vmid, {"boot": "order=scsi0", "ide2": "none,media=cdrom"})
        j.finish("createvm", DONE, f"VMID {vmid}")

    # -- guest adaptation --------------------------------------------
    def _guest_prep(self) -> None:
        j = self.job
        if not self.plan.get("guest_prep"):
            j.finish("guestprep", SKIPPED, "disabled")
            return
        j.begin("guestprep")
        disk = self.detail["disks"][0]
        path = disk["target_path"]
        try:
            report = guest_adapt(path, self.log)
        except Exception as exc:  # noqa: BLE001
            self.log(f"guest adaptation skipped: {exc}", "warning")
            j.finish("guestprep", SKIPPED, str(exc)[:60])
            return
        j.finish("guestprep", DONE, report or "nothing to change")

    def _start_vm(self) -> None:
        j = self.job
        if not self.plan.get("start_after"):
            j.finish("startvm", SKIPPED, "not requested")
            return
        j.begin("startvm")
        pve.start_vm(int(self.plan["target_vmid"]))
        self.log(f"VM {self.plan['target_vmid']} started", "success")
        j.finish("startvm", DONE, "running")

    def _cleanup(self, ok: bool) -> None:
        j = self.job
        j.begin("cleanup")
        try:
            self.src.release(ok, self.log)
        except SSHError as exc:
            self.log(f"source cleanup problem: {exc}", "warning")
        if ok and self.plan.get("source_restart"):
            try:
                self.src.start(self.plan["vm_uuid"])
                self.log("source guest started again on the source host",
                         "warning")
            except SSHError as exc:
                self.log(f"could not restart the source: {exc}", "warning")
        if not ok and self.created_vmid:
            self.log(f"removing the half-built VM {self.created_vmid}", "warning")
            pve.destroy_vm(self.created_vmid)
        elif not ok:
            for volid in self.volumes:
                pve.free_disk(volid)
        j.finish("cleanup", DONE)


# -- guest adaptation -------------------------------------------------
# The migrated image is foreign data: it was authored on another system and
# may contain symlinks pointing at host paths. Everything below therefore
# goes through SafeRoot, which refuses to traverse a symlink, and the mount
# itself carries nosuid,nodev,noexec.
MOUNT_OPTS = "nosuid,nodev,noexec"
PREP_MNT = "/mnt/.xenproxy-prep"


def _run(cmd: list[str], **kw) -> subprocess.CompletedProcess:
    kw.setdefault("stdin", subprocess.DEVNULL)
    return subprocess.run(cmd, capture_output=True, text=True, timeout=120, **kw)


def guest_adapt(device: str, log) -> str:
    """Mount the volume and make the guest KVM-ready.

    Two things routinely break a guest that has just changed hypervisor:
    the NIC is renamed, and a UEFI guest loses the firmware boot entry that
    used to point at its loader. Both are handled here.
    """
    changes: list[str] = []
    _run(["partx", "-u", device], check=False)
    _run(["udevadm", "settle"], check=False)

    parts = sorted(_candidate_parts(device))
    if not parts:
        raise RuntimeError("no partitions found on the target volume")

    os.makedirs(PREP_MNT, exist_ok=True)
    root = esp = ""
    for part in parts:
        if _run(["mount", "-o", MOUNT_OPTS, part, PREP_MNT]).returncode != 0:
            continue
        try:
            with SafeRoot(PREP_MNT) as sr:
                if not root and sr.is_dir("etc") and (sr.is_dir("boot")
                                                      or sr.is_dir("usr")):
                    root = part
                elif not esp and sr.is_dir("EFI"):
                    esp = part
        except OSError:
            pass
        _run(["umount", PREP_MNT])
        if root and esp:
            break
    if not root:
        _rmdir(PREP_MNT)
        raise RuntimeError("no Linux root filesystem found")

    try:
        log(f"root filesystem on {root}")
        if _run(["mount", "-o", MOUNT_OPTS, root, PREP_MNT]).returncode == 0:
            try:
                with SafeRoot(PREP_MNT) as sr:
                    changes = _adapt(sr, log)
            finally:
                _run(["umount", PREP_MNT])
        if esp:
            log(f"EFI system partition on {esp}")
            if _run(["mount", "-o", MOUNT_OPTS, esp, PREP_MNT]).returncode == 0:
                try:
                    with SafeRoot(PREP_MNT) as sr:
                        if _ensure_efi_fallback(sr, log):
                            changes.append("EFI fallback loader")
                except (OSError, UnsafePath) as exc:
                    log(f"could not inspect the EFI partition: {exc}", "warning")
                finally:
                    _run(["umount", PREP_MNT])
    finally:
        _rmdir(PREP_MNT)
    return ", ".join(changes)


def _rmdir(path: str) -> None:
    try:
        os.rmdir(path)
    except OSError:
        pass


FALLBACK_DIR = "EFI/BOOT"
FALLBACK = "EFI/BOOT/BOOTX64.EFI"
# shim first: it is what a secure-boot guest expects to be entered through,
# and it chain-loads the loader sitting next to it.
LOADERS = ("shimx64.efi", "grubx64.efi", "systemd-bootx64.efi", "bootx64.efi")


def _ensure_efi_fallback(sr: SafeRoot, log) -> bool:
    """Make the ESP bootable without a firmware boot entry.

    A UEFI guest boots through an NVRAM entry that names its loader, and
    that NVRAM stays behind on the source hypervisor - the target gets an
    empty one. Firmware then falls back to the removable-media path
    \\EFI\\BOOT\\BOOTX64.EFI, and a guest that never installed one goes
    looking for a PXE server instead of starting.
    """
    try:
        entries = sr.listdir("EFI")
    except (OSError, UnsafePath) as exc:
        log(f"cannot read the EFI directory: {exc}", "warning")
        return False
    have = {e.lower(): e for e in entries}
    if "boot" in have:
        try:
            names = {n.lower() for n in sr.listdir(f"EFI/{have['boot']}")}
        except (OSError, UnsafePath):
            names = set()
        if "bootx64.efi" in names:
            log("the ESP already carries a fallback boot loader")
            return False

    source = ""
    for want in LOADERS:
        for entry in entries:
            if entry.lower() == "boot":
                continue
            try:
                names = sr.listdir(f"EFI/{entry}")
            except (OSError, UnsafePath):
                continue
            for name in names:
                if name.lower() == want:
                    source = f"EFI/{entry}/{name}"
                    break
            if source:
                break
        if source:
            break
    if not source:
        log("no EFI loader found to copy into the fallback path - the guest "
            "may not boot without a firmware entry", "warning")
        return False

    data = sr.read_bytes(source)
    if not data:
        log(f"{source} is empty - not copying it", "warning")
        return False
    sr.mkdir(FALLBACK_DIR)
    sr.write_bytes(FALLBACK, data)
    log(f"copied {source} to {FALLBACK} so the firmware can find it",
        "success")
    if source.lower().endswith("shimx64.efi"):
        # shim looks for its payload beside itself, so grub has to follow
        vendor = source.rsplit("/", 1)[0]
        for name in sr.listdir(vendor):
            if name.lower() in ("grubx64.efi", "mmx64.efi"):
                sr.write_bytes(f"{FALLBACK_DIR}/{name.lower()}",
                               sr.read_bytes(f"{vendor}/{name}"))
                log(f"copied {name} next to it")
    return True


def _adapt(sr: SafeRoot, log) -> list[str]:
    changes: list[str] = []

    uses_eth = False
    try:
        uses_eth = bool(re.search(r"^\s*(iface|allow-hotplug|auto)\s+eth\d",
                                  sr.read_text("etc/network/interfaces"), re.M))
    except UnsafePath as exc:
        log(f"/etc/network/interfaces in the guest image {_why(exc)} - "
            "not reading it", "danger")
    except FileNotFoundError:
        pass
    except OSError as exc:
        log(f"cannot read /etc/network/interfaces: {exc}", "warning")

    if uses_eth:
        try:
            if _pin_ifnames(sr, log):
                changes.append("net.ifnames=0")
        except UnsafePath as exc:
            log(f"refusing to edit GRUB: {exc}", "danger")
            changes.append("grub untouched")
        except OSError as exc:
            log(f"could not edit GRUB: {exc}", "warning")
    else:
        log("no static ethN configuration - leaving GRUB alone")

    try:
        active = [l for l in sr.read_text("etc/fstab").splitlines()
                  if l.strip() and not l.strip().startswith("#")]
        if any(re.search(r"/dev/(xvd|sd)[a-z]", l) for l in active):
            log("fstab still names devices directly - check it before booting",
                "warning")
            changes.append("fstab warning")
        else:
            log("fstab uses UUIDs - nothing to change")
    except UnsafePath as exc:
        log(f"/etc/fstab in the guest image {_why(exc)} - not reading it",
            "danger")
    except FileNotFoundError:
        pass
    except OSError as exc:
        log(f"cannot read /etc/fstab: {exc}", "warning")

    try:
        imgs = [f for f in sr.listdir("boot") if f.startswith("initrd.img-")]
        if imgs:
            newest = sorted(imgs)[-1]
            if _has_virtio(sr, f"boot/{newest}"):
                log("virtio modules present in the initramfs")
            else:
                log(f"{newest} carries no virtio_scsi module - "
                    "the guest may not see its disk", "warning")
                changes.append("virtio warning")
    except (OSError, UnsafePath) as exc:
        log(f"cannot inspect the initramfs: {exc}", "warning")
    return changes


def _has_virtio(sr: SafeRoot, rel: str) -> bool:
    """Ask lsinitramfs through /proc/self/fd so no symlink can redirect it."""
    path, fd = sr.procfs_path(rel)
    try:
        cp = _run(["lsinitramfs", path], pass_fds=(fd,))
    finally:
        os.close(fd)
    if cp.returncode != 0:
        raise OSError(cp.stderr.strip() or "lsinitramfs failed")
    return "virtio_scsi" in cp.stdout or "virtio_blk" in cp.stdout


def _candidate_parts(device: str) -> list[str]:
    out = []
    for suffix in ("-part", "p", ""):
        for i in range(1, 9):
            cand = f"{device}{suffix}{i}"
            if os.path.exists(cand):
                out.append(cand)
        if out:
            break
    return out


def _why(exc: UnsafePath) -> str:
    """Turn the refusal into something an operator can act on."""
    text = str(exc)
    if "not a regular file" in text:
        return "is a device, FIFO or socket"
    if "symlink" in text:
        return "is a symlink"
    return "could not be opened safely"


GRUB_DEF = "etc/default/grub"
GRUB_CFG = "boot/grub/grub.cfg"
FLAGS = "net.ifnames=0 biosdevname=0"


def _pin_ifnames(sr: SafeRoot, log) -> bool:
    try:
        text = sr.read_text(GRUB_DEF)
    except UnsafePath as exc:
        # Worth shouting about: a plain open() here would have written
        # through the link, or hung on a FIFO, instead of failing.
        log(f"/{GRUB_DEF} in the guest image {_why(exc)} - refusing to use it",
            "danger")
        raise
    except FileNotFoundError:
        log("no /etc/default/grub - leaving the interface name alone", "warning")
        return False
    if "net.ifnames=0" in text:
        log("net.ifnames is already pinned")
        return False

    sr.backup(GRUB_DEF, ".pre-xenproxy")
    if re.search(r"^GRUB_CMDLINE_LINUX=", text, re.M):
        text = re.sub(
            r'^GRUB_CMDLINE_LINUX="(.*)"$',
            lambda m: f'GRUB_CMDLINE_LINUX="{(m.group(1) + " " + FLAGS).strip()}"',
            text, count=1, flags=re.M)
    else:
        text += f'\nGRUB_CMDLINE_LINUX="{FLAGS}"\n'
    sr.write_text(GRUB_DEF, text)

    n = 0
    try:
        cfg_text = sr.read_text(GRUB_CFG)
    except UnsafePath as exc:
        log(f"/{GRUB_CFG} in the guest image {_why(exc)} - leaving it alone",
            "danger")
        cfg_text = None
    except FileNotFoundError:
        cfg_text = None
    if cfg_text is not None:
        sr.backup(GRUB_CFG, ".pre-xenproxy")
        lines = cfg_text.splitlines(keepends=True)
        for i, line in enumerate(lines):
            if re.match(r"^\s*linux\s+\S+", line) and "net.ifnames=0" not in line:
                lines[i] = line.rstrip("\n") + f" {FLAGS}\n"
                n += 1
        sr.write_text(GRUB_CFG, "".join(lines))
    log(f"pinned net.ifnames=0 in /etc/default/grub and {n} grub.cfg entries",
        "success")
    return True
