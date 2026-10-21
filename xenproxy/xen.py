"""XCP-ng / XenServer adapter driven through the `xe` CLI over SSH."""
from __future__ import annotations

import base64
import hashlib
import http.client
import re
import shlex
import ssl
import urllib.error
import urllib.parse
import urllib.request

from .sshclient import SSHClient, SSHError
from .text import scrub, scrub_field

XE = "/opt/xensource/bin/xe"

UUID_RE = re.compile(r"^[0-9a-f]{8}(-[0-9a-f]{4}){3}-[0-9a-f]{12}$")



_MAC = re.compile(r"^[0-9a-f]{2}(:[0-9a-f]{2}){5}$")


def valid_mac(value: str) -> str:
    """Return the MAC when it is well formed, otherwise an empty string.

    It ends up inside a comma-separated `qm` property string, where a
    crafted value could otherwise smuggle in extra options.
    """
    mac = (value or "").strip().lower()
    return mac if _MAC.match(mac) else ""


class TamperedOutput(SSHError):
    """The xe output cannot be parsed unambiguously."""


def parse_records(text: str, *, strict: bool = True) -> list[dict]:
    """Parse the `key ( RO): value` block format emitted by xe.

    `xe` never prints a key twice inside one record, so a repeat means a
    free-text field carried a newline and the lines after it are forged.
    In strict mode that is refused rather than silently resolved, because
    which of the two values wins would otherwise be up to the attacker.
    """
    records: list[dict] = []
    cur: dict = {}
    for raw in text.splitlines():
        line = raw.rstrip()
        if not line.strip():
            continue
        m = re.match(r"\s*([A-Za-z0-9_.\-]+)\s*\((?:\s*\w+\s*)\)\s*:\s?(.*)$", line)
        if not m:
            if cur and records is not None:
                last = cur.get("__last__")
                if last:
                    cur[last] = scrub_field(cur[last] + " " + line.strip())
            continue
        key, val = scrub(m.group(1)), scrub_field(m.group(2).strip())
        # A record only ever starts at a real uuid. Free-text fields such as
        # name-description may contain newlines, and a crafted one could
        # otherwise forge a "uuid ( RO): ..." line and open a record under an
        # identifier of the attacker's choosing.
        if key == "uuid":
            if not UUID_RE.match(val):
                continue
            if "uuid" in cur:
                records.append(cur)
                cur = {}
        elif strict and key in cur:
            raise TamperedOutput(
                f"the xe output lists {key!r} twice in one record - a guest "
                "name or description probably contains a newline. Refusing "
                "to read it; fix the field on the Xen host.")
        cur[key] = val
        cur["__last__"] = key
    if cur.get("uuid"):
        records.append(cur)
    for r in records:
        r.pop("__last__", None)
    return records


def parse_map(value: str) -> dict:
    """Parse xe map fields like `a: 1; b: 2`."""
    out = {}
    for part in value.split(";"):
        if ":" in part:
            k, v = part.split(":", 1)
            out[scrub(k.strip())] = scrub(v.strip())
    return out


class XapiStream:
    """The disk export, wrapped so every source adapter looks the same.

    Carries its own byte count, which is what the target volume is sized
    from, and owns the HTTPS connection underneath the response so closing
    the stream really closes the socket.
    """

    def __init__(self, resp, conn, size: int) -> None:
        self.resp = resp
        self.conn = conn
        self.size = int(size)
        self.name = "export_raw_vdi"
        self.stderr = ""

    def readinto(self, view) -> int:
        return self.resp.readinto(view)

    def close(self) -> None:
        try:
            self.resp.close()
        except OSError:
            pass
        if self.conn is not None:
            try:
                self.conn.close()
            except OSError:
                pass
            self.conn = None


class XenHost:
    kind = "xen"
    needs_password = True       # the XAPI disk export authenticates with it
    label = "XenServer / XCP-ng"

    def __init__(self, ssh: SSHClient, verify_tls: bool = False,
                 tls_pin: str = "", insecure_tls: bool = False) -> None:
        self.ssh = ssh
        self.verify_tls = verify_tls
        self.tls_pin = (tls_pin or "").lower()
        self.insecure_tls = bool(insecure_tls)
        self._info: dict | None = None
        self.snapshot_uuid = ""

    # -- connection --------------------------------------------------
    def probe(self) -> dict:
        """Verify reachability and collect host facts."""
        ok, msg = self.ssh.test()
        if not ok:
            raise SSHError(msg)
        cp = self.ssh.run(
            "hostname; "
            "grep -m1 PRETTY_NAME /etc/os-release 2>/dev/null | cut -d= -f2 | tr -d '\"'; "
            f"test -x {XE} && echo XE_OK || echo XE_MISSING; "
            f"{XE} host-list params=uuid --minimal 2>/dev/null; "
            "df -k / | awk 'NR==2{print $4}'",
            timeout=30)
        lines = [l.strip() for l in cp.stdout.splitlines()]
        while len(lines) < 5:
            lines.append("")
        if "XE_MISSING" in lines[2]:
            raise SSHError("no `xe` binary found - is this a XenServer/XCP-ng host?")
        self._info = {
            "hostname": scrub(lines[0]),
            "version": scrub(lines[1]) or "unknown",
            "hosts": [h for h in lines[3].split(",") if h],
            "dom0_free_kb": int(lines[4]) if lines[4].isdigit() else 0,
        }
        return self._info

    def _xe(self, args: str, timeout: int = 60) -> str:
        cp = self.ssh.run(f"{XE} {args}", timeout=timeout)
        if cp.returncode != 0:
            raise SSHError(scrub((cp.stderr or cp.stdout).strip()) or "xe failed")
        return cp.stdout

    # -- inventory ---------------------------------------------------
    def authoritative_vm_uuids(self) -> set:
        """UUIDs straight from --minimal, which is comma-separated and so
        cannot be forged through a guest name."""
        raw = self._xe("vm-list params=uuid --minimal", timeout=60).strip()
        return {u for u in (x.strip() for x in raw.split(",")) if UUID_RE.match(u)}

    def list_vms(self, include_templates: bool = False) -> list[dict]:
        known = self.authoritative_vm_uuids()
        out = self._xe(
            "vm-list params=uuid,name-label,name-description,power-state,"
            "memory-static-max,VCPUs-max,is-a-snapshot,is-a-template,"
            "is-control-domain,resident-on,tags --multiple", timeout=90)
        vms = []
        seen: set = set()
        for r in parse_records(out):
            uuid = r.get("uuid")
            if uuid not in known:
                # forged identifier, or a VM that vanished between the two
                # calls - either way not something to act on
                continue
            if uuid in seen:
                # The same UUID cannot legitimately appear twice. A guest
                # whose name or description carries a newline can forge the
                # lines that follow it, so the listing is not trustworthy.
                raise SSHError(
                    f"the xe listing contains {uuid} twice - a guest name or "
                    "description probably contains a newline. Refusing to "
                    "use this inventory; fix the name on the Xen host.")
            seen.add(uuid)
            if r.get("is-control-domain") == "true":
                continue
            if r.get("is-a-snapshot") == "true":
                continue
            if r.get("is-a-template") == "true" and not include_templates:
                continue
            vms.append({
                "uuid": r.get("uuid", ""),
                "name": r.get("name-label", ""),
                "description": r.get("name-description", ""),
                "power_state": r.get("power-state", "unknown"),
                "memory": int(r.get("memory-static-max") or 0),
                "vcpus": int(r.get("VCPUs-max") or 0),
                "template": r.get("is-a-template") == "true",
                "tags": r.get("tags", ""),
            })
        vms.sort(key=lambda v: v["name"].lower())
        return vms

    def _one_record(self, out: str, uuid: str, what: str) -> dict:
        """Exactly one record, for exactly the object that was asked for."""
        recs = parse_records(out)
        if len(recs) != 1:
            raise TamperedOutput(
                f"{what} {uuid} returned {len(recs)} records instead of one - "
                "a free-text field probably contains a newline")
        if recs[0].get("uuid") != uuid:
            raise TamperedOutput(
                f"asked for {what} {uuid} but got "
                f"{recs[0].get('uuid')!r} back")
        return recs[0]

    def vm_detail(self, uuid: str) -> dict:
        out = self._xe(f"vm-param-list uuid={shlex.quote(uuid)}", timeout=45)
        p = self._one_record(out, uuid, "VM")
        platform = parse_map(p.get("platform", ""))
        detail = {
            "uuid": uuid,
            "name": p.get("name-label", ""),
            "description": p.get("name-description", ""),
            "power_state": p.get("power-state", ""),
            "memory": int(p.get("memory-static-max") or 0),
            "vcpus": int(p.get("VCPUs-max") or 0),
            "cores_per_socket": int(platform.get("cores-per-socket") or 1),
            "hvm": bool(p.get("HVM-boot-policy")),
            "firmware": "uefi" if platform.get("device-model") == "qemu-upstream-uefi"
                        or "uefi" in (p.get("HVM-boot-policy") or "").lower() else "bios",
            "snapshots": [s for s in (p.get("snapshots") or "").split(";") if s.strip()],
            "disks": self.vm_disks(uuid),
            "nics": self.vm_nics(uuid),
        }
        return detail

    def vm_disks(self, uuid: str) -> list[dict]:
        out = self._xe(
            f"vbd-list vm-uuid={shlex.quote(uuid)} "
            "params=uuid,vdi-uuid,device,type,mode,bootable,empty,userdevice "
            "--multiple", timeout=45)
        disks = []
        for r in parse_records(out):
            if r.get("type") != "Disk" or r.get("empty") == "true":
                continue
            vdi = r.get("vdi-uuid", "")
            if not vdi or vdi.startswith("<"):
                continue
            info = self.vdi_info(vdi)
            info.update({"device": r.get("device", ""),
                         "bootable": r.get("bootable") == "true",
                         "userdevice": r.get("userdevice", "")})
            disks.append(info)
        disks.sort(key=lambda d: d.get("userdevice", ""))
        return disks

    def vdi_info(self, vdi: str) -> dict:
        out = self._xe(f"vdi-param-list uuid={shlex.quote(vdi)}", timeout=45)
        p = self._one_record(out, vdi, "VDI")
        sr = p.get("sr-uuid", "")
        return {
            "vdi": vdi,
            "label": p.get("name-label", ""),
            "virtual_size": int(p.get("virtual-size") or 0),
            "physical_size": int(p.get("physical-utilisation") or 0),
            "sr": sr,
            "sr_name": p.get("sr-name-label", ""),
            "sm_config": parse_map(p.get("sm-config", "")),
        }

    def vm_nics(self, uuid: str) -> list[dict]:
        out = self._xe(
            f"vif-list vm-uuid={shlex.quote(uuid)} "
            "params=uuid,device,MAC,network-uuid,MTU --multiple", timeout=45)
        nics = []
        for r in parse_records(out):
            net = r.get("network-uuid", "")
            nics.append({
                "uuid": r.get("uuid", ""),
                "device": r.get("device", ""),
                "mac": valid_mac(r.get("MAC") or ""),
                "mtu": int(r.get("MTU") or 1500),
                "network": net,
                "network_name": self.network_name(net),
                "vlan": self.network_vlan(net),
            })
        nics.sort(key=lambda n: n.get("device", ""))
        return nics

    _net_cache: dict = {}

    def network_name(self, uuid: str) -> str:
        if not uuid or uuid.startswith("<"):
            return ""
        if uuid not in self._net_cache:
            try:
                out = self._xe(f"network-param-list uuid={shlex.quote(uuid)}",
                               timeout=30)
                self._net_cache[uuid] = self._one_record(out, uuid, "network")
            except SSHError:
                self._net_cache[uuid] = {}
        return self._net_cache[uuid].get("name-label", "")

    def network_vlan(self, uuid: str) -> int | None:
        if not uuid or uuid.startswith("<"):
            return None
        self.network_name(uuid)
        pifs = (self._net_cache.get(uuid, {}).get("PIF-uuids") or "").split(";")
        pif = pifs[0].strip() if pifs and pifs[0].strip() else ""
        if not pif:
            return None
        try:
            out = self._xe(f"pif-param-get uuid={shlex.quote(pif)} param-name=VLAN",
                           timeout=25).strip()
            v = int(out)
            return v if v >= 0 else None
        except (SSHError, ValueError):
            return None

    def sr_list(self) -> list[dict]:
        out = self._xe("sr-list params=uuid,name-label,type,content-type,"
                       "physical-size,physical-utilisation --multiple", timeout=45)
        return [{
            "uuid": r.get("uuid", ""), "name": r.get("name-label", ""),
            "type": r.get("type", ""), "content": r.get("content-type", ""),
            "size": int(r.get("physical-size") or 0),
            "used": int(r.get("physical-utilisation") or 0),
        } for r in parse_records(out)]

    # -- disk location -----------------------------------------------
    def sr_type(self, sr: str) -> str:
        try:
            return self._xe(f"sr-param-get uuid={shlex.quote(sr)} param-name=type",
                            timeout=25).strip()
        except SSHError:
            return ""

    def vdi_path(self, vdi: str, sr: str) -> tuple[str, str]:
        """Return (path, kind) where kind is 'lvm' or 'file'."""
        st = self.sr_type(sr)
        if st.startswith("lvm"):
            return f"/dev/VG_XenStorage-{sr}/VHD-{vdi}", "lvm"
        return f"/var/run/sr-mount/{sr}/{vdi}.vhd", "file"

    def vhd_parent(self, path: str) -> str | None:
        """Non-empty when the VHD is part of a differencing chain."""
        cp = self.ssh.run(f"vhd-util query -p -n {shlex.quote(path)} 2>&1", timeout=30)
        out = (cp.stdout or "").strip()
        if cp.returncode != 0 or "no parent" in out.lower() or not out:
            return None
        return out if out.startswith("/") else None

    def lv_activate(self, path: str) -> None:
        self.ssh.run(f"lvchange -ay {shlex.quote(path)}", timeout=60)

    def lv_deactivate(self, path: str) -> None:
        self.ssh.run(f"lvchange -an {shlex.quote(path)}", timeout=60)

    def lv_is_active(self, path: str) -> bool:
        cp = self.ssh.run(f"test -e {shlex.quote(path)} && echo yes", timeout=25)
        return "yes" in cp.stdout

    # -- power / snapshots -------------------------------------------
    def shutdown(self, uuid: str, force: bool = False, timeout: int = 600) -> None:
        verb = "vm-shutdown" if not force else "vm-shutdown force=true"
        self._xe(f"{verb} uuid={shlex.quote(uuid)}", timeout=timeout)

    def start(self, uuid: str, timeout: int = 300) -> None:
        self._xe(f"vm-start uuid={shlex.quote(uuid)}", timeout=timeout)

    def power_state(self, uuid: str) -> str:
        try:
            return self._xe(
                f"vm-param-get uuid={shlex.quote(uuid)} param-name=power-state",
                timeout=25).strip()
        except SSHError:
            return "unknown"

    def snapshot(self, uuid: str, label: str, timeout: int = 900) -> str:
        return scrub(self._xe(
            f"vm-snapshot uuid={shlex.quote(uuid)} "
            f"new-name-label={shlex.quote(label)}", timeout=timeout).strip())

    def snapshot_destroy(self, snap_uuid: str) -> None:
        q = shlex.quote(snap_uuid)
        try:
            self._xe(f"snapshot-uninstall uuid={q} force=true", timeout=300)
        except SSHError:
            self._xe(f"vm-uninstall uuid={q} force=true", timeout=300)

    # -- raw disk export over the XAPI HTTP handler -------------------
    def fetch_tls_pin(self) -> str:
        """SHA-256 of the XAPI certificate, read over the SSH session.

        SSH is already authenticated by password or key, so the fingerprint
        it hands back is trustworthy and can anchor the HTTPS channel that
        the disk export runs over.
        """
        cp = self.ssh.run(
            "openssl x509 -in /etc/xensource/xapi-ssl.pem -outform DER "
            "2>/dev/null | sha256sum | cut -d' ' -f1", timeout=30)
        pin = (cp.stdout or "").strip().lower()
        if len(pin) != 64 or not all(c in "0123456789abcdef" for c in pin):
            raise SSHError("could not read the XAPI certificate")
        return pin

    def _tls_ctx(self) -> ssl.SSLContext:
        ctx = ssl.create_default_context()
        if self.verify_tls and not self.tls_pin:
            return ctx
        # A pinned fingerprint is checked by hand right after the handshake;
        # the library-level check cannot be used because XCP-ng ships a
        # self-signed certificate whose name never matches.
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        return ctx

    def _connect_xapi(self, timeout: int) -> http.client.HTTPSConnection:
        # Fail closed: the host password rides on this channel, so it is only
        # ever sent once the peer has been proven - by a pinned fingerprint or
        # by ordinary CA verification. Running without either takes a
        # deliberate insecure_tls on the server entry.
        if not self.tls_pin and not self.verify_tls and not self.insecure_tls:
            raise SSHError(
                "no pinned XAPI certificate for this server - add the server "
                "again so the fingerprint can be read over SSH, enable "
                "verify_tls, or set insecure_tls to accept an unverified "
                "channel on purpose")
        conn = http.client.HTTPSConnection(
            self.ssh.host, 443, timeout=timeout, context=self._tls_ctx())
        try:
            conn.connect()
        except OSError as exc:
            raise SSHError(f"cannot reach the XAPI endpoint: {exc}") from exc
        if self.tls_pin:
            der = conn.sock.getpeercert(binary_form=True)
            got = hashlib.sha256(der or b"").hexdigest()
            if got != self.tls_pin:
                conn.close()
                raise SSHError(
                    "XAPI certificate fingerprint does not match the pinned "
                    f"one ({got[:16]}... vs {self.tls_pin[:16]}...) - refusing "
                    "to send credentials")
        return conn

    def export_raw(self, vdi: str, timeout: int = 180):
        """Open a raw byte stream of a VDI.

        XAPI resolves VHD differencing chains itself, so this works for
        snapshots and for disks whose base image sits further down the
        chain - unlike reading the leaf LV directly.
        """
        if not self.ssh.password:
            raise SSHError(
                "the XAPI HTTP export needs the host password; "
                "add it to the server entry")
        conn = self._connect_xapi(timeout)
        # The credentials go out only after the certificate has been accepted.
        path = f"/export_raw_vdi?vdi={urllib.parse.quote(vdi)}&format=raw"
        token = base64.b64encode(
            f"{self.ssh.user}:{self.ssh.password}".encode()).decode()
        try:
            conn.putrequest("GET", path, skip_accept_encoding=True)
            conn.putheader("Authorization", f"Basic {token}")
            conn.putheader("Host", self.ssh.host)
            conn.endheaders()
            resp = conn.getresponse()
        except OSError as exc:
            conn.close()
            raise SSHError(f"XAPI export failed: {exc}") from exc
        if resp.status != 200:
            detail = scrub(f"{resp.status} {resp.reason}")
            conn.close()
            raise SSHError(f"XAPI export refused ({detail})")
        resp.xapi_conn = conn  # keep the connection alive with the stream
        return resp

    # -- source preparation -------------------------------------------
    def prepare(self, plan: dict, detail: dict, log) -> str:
        """Put the source into the state the chosen mode promises.

        Returns a short note for the job step; `release` undoes whatever was
        created here.
        """
        import time as _time
        uuid = plan["vm_uuid"]
        mode = plan.get("mode", "offline")
        for d in detail.get("disks", []):
            d["export_source"] = d["vdi"]

        if mode == "offline":
            state = self.power_state(uuid)
            if state == "running":
                log("clean shutdown of the source guest")
                self.shutdown(uuid, timeout=900)
                for _ in range(60):
                    if self.power_state(uuid) == "halted":
                        break
                    _time.sleep(2)
                else:
                    raise SSHError("source guest did not reach halted state")
                log("source guest halted", "success")
            else:
                log("source guest was already halted")
            return "halted"

        if mode == "snapshot":
            label = f"xenproxY-{_time.strftime('%Y%m%d-%H%M%S')}"
            log(f"creating snapshot {label}")
            self.snapshot_uuid = self.snapshot(uuid, label)
            log(f"snapshot {self.snapshot_uuid}", "success")
            snap_disks = self.vm_disks(self.snapshot_uuid)
            if not snap_disks:
                raise SSHError("the snapshot exposes no disks")
            by_dev = {d["device"]: d for d in snap_disks}
            for d in detail.get("disks", []):
                src = by_dev.get(d["device"])
                if src:
                    d["export_source"] = src["vdi"]
            return label

        log("reading the volume while the guest runs - nothing is changed on Xen")
        log("the image is crash-consistent; the guest will replay its journal",
            "warning")
        return "no change on Xen"

    def release(self, ok: bool, log) -> None:
        if not self.snapshot_uuid:
            return
        try:
            self.snapshot_destroy(self.snapshot_uuid)
            log("snapshot removed from Xen", "success")
        except SSHError as exc:
            log(f"could not remove snapshot {self.snapshot_uuid}: {exc}",
                "warning")
        self.snapshot_uuid = ""

    def open_disk(self, disk: dict, log=None) -> XapiStream:
        vdi = disk.get("export_source") or disk["vdi"]
        resp = self.export_raw(vdi, timeout=300)
        conn = getattr(resp, "xapi_conn", None)
        stream = XapiStream(resp, conn, disk.get("virtual_size") or 0)
        stream.name = f"VDI {vdi[:8]}"
        return stream

    def pinned(self) -> bool:
        return bool(self.tls_pin)

    def export_available(self, vdi: str) -> tuple[bool, str]:
        """Cheap probe: open the stream, read a little, close again."""
        try:
            resp = self.export_raw(vdi, timeout=60)
        except SSHError as exc:
            return False, str(exc)
        try:
            data = resp.read(512)
            if len(data) < 512:
                return False, "short read from XAPI"
            return True, "ok"
        finally:
            resp.close()
            getattr(resp, "xapi_conn", None) and resp.xapi_conn.close()
