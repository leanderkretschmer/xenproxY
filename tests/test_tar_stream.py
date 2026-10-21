#!/usr/bin/env python3
"""The tar reader, against the shapes a real export can take.

A tar size field is eleven octal digits, so it stops just short of 8 GiB.
Anything larger is carried either as a GNU base-256 field or as a PAX
extended record in front of the entry - and Go, which Incus is written in,
chooses PAX. A disk bigger than 8 GiB therefore looked like a disk of no
size at all until this was handled, which is the whole reason these cases
exist.

    python3 tests/test_tar_stream.py
"""
import io
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from xenproxy.incus import TarMember
from xenproxy.sshclient import SSHError

BIG = 64 * (1 << 30)          # the size that first showed the bug
SMALL = 4 * (1 << 30)

def block(fields: bytes) -> bytes:
    return fields.ljust(512, b"\0")

def ustar_header(name, size, typeflag=b"0", octal=True):
    h = bytearray(b"\0" * 512)
    h[0:len(name)] = name.encode()
    h[100:108] = b"0000644\0"
    h[108:116] = b"0000000\0"
    h[116:124] = b"0000000\0"
    if octal:
        h[124:136] = ("%011o\0" % size).encode()
    else:
        # GNU base-256: top bit set, the rest big-endian
        raw = size.to_bytes(11, "big")
        h[124] = 0x80
        h[125:136] = raw
    h[136:148] = b"00000000000\0"
    h[156:157] = typeflag
    h[257:263] = b"ustar\0"
    h[263:265] = b"00"
    chk = sum(h[:148]) + 32 * 8 + sum(h[156:])
    h[148:156] = ("%06o\0 " % chk).encode()
    return bytes(h)

def pax_record(key, value):
    body = f"{key}={value}\n".encode()
    for n in range(len(body) + 3, len(body) + 8):
        if len(str(n)) + 1 + len(body) == n:
            return str(n).encode() + b" " + body
    raise AssertionError

def pad(data):
    rest = (-len(data)) % 512
    return data + b"\0" * rest

def stream_pax(size):
    """How Go writes it: a PAX record with the real size, field left at 0."""
    rec = pax_record("size", size) + pax_record("path", "backup/virtual-machine.img")
    out = ustar_header("backup/index.yaml", 20) + pad(b"name: haos\n")
    out += ustar_header("PaxHeaders/0/virtual-machine.img", len(rec), b"x")
    out += pad(rec)
    out += ustar_header("backup/virtual-machine.img", 0)
    return out

def stream_gnu(size):
    """How GNU tar writes it: base-256 in the size field."""
    out = ustar_header("backup/index.yaml", 20) + pad(b"name: haos\n")
    out += ustar_header("backup/virtual-machine.img", size, octal=False)
    return out

def stream_small(size):
    out = ustar_header("backup/index.yaml", 20) + pad(b"name: haos\n")
    out += ustar_header("backup/virtual-machine.img", size)
    return out

class FakeProc:
    """Stands in for the ssh process TarMember is given."""
    def __init__(self, data):
        self.stdout = io.BufferedReader(io.BytesIO(data))
        self.stderr = None
    def poll(self): return 0
    def terminate(self): pass
    def wait(self, timeout=None): return 0
    def kill(self): pass

def check(label, data, expect):
    try:
        m = TarMember(FakeProc(data), "virtual-machine.img")
        ok = m.size == expect
        print(f"  {'ok  ' if ok else 'FAIL'} {label:<32} {m.size:>15,} "
              f"(want {expect:,})")
        m.close()
        return ok
    except SSHError as exc:
        print(f"  FAIL {label:<32} {exc}")
        return False

def stream_longname(size):
    """A GNU long-name record in front of the entry."""
    long = b"backup/some/deeply/nested/virtual-machine.img\0"
    out = ustar_header("backup/index.yaml", 20) + pad(b"name: haos\n")
    out += ustar_header("././@LongLink", len(long), b"L")
    out += pad(long)
    out += ustar_header("short", size, octal=False)
    return out

def stream_pax_path(size):
    """PAX carries both the path and the size."""
    rec = pax_record("size", size) + pax_record("path", "backup/virtual-machine.img")
    out = ustar_header("PaxHeaders/0/x", len(rec), b"x") + pad(rec)
    out += ustar_header("irgendwas-anderes", 0)
    return out

def stream_missing():
    out = ustar_header("backup/index.yaml", 20) + pad(b"name: haos\n")
    out += ustar_header("backup/container", 10) + pad(b"rootfs\n")
    return out + b"\0" * 1024

def expect_error(label, data, needle):
    try:
        m = TarMember(FakeProc(data), "virtual-machine.img")
        print(f"  FAIL {label:<32} should have failed, got {m.size}")
        return False
    except SSHError as exc:
        ok = needle in str(exc)
        print(f"  {'ok  ' if ok else 'FAIL'} {label:<32} {str(exc)[:56]}")
        return ok

def stream_with_payload(payload):
    """A PAX-sized entry whose bytes are actually there, to read back."""
    rec = pax_record("size", len(payload)) + pax_record(
        "path", "backup/virtual-machine.img")
    out = ustar_header("backup/index.yaml", 11) + pad(b"name: haos\n")
    out += ustar_header("PaxHeaders/0/vm", len(rec), b"x") + pad(rec)
    out += ustar_header("placeholder", 0) + pad(payload)
    return out


def stream_as_incus_writes_it(size):
    """The layout a real export has, taken from one.

    Two things in here are easy to get wrong. The disk's own metadata sits
    in a *directory* called backup/virtual-machine, one character away from
    the file; and the PAX header that carries the real size is itself named
    after the file it describes, so a reader matching on the name alone
    picks the wrong entry and finds twenty bytes of text.
    """
    out = ustar_header("backup/index.yaml", 12) + pad(b"name: big\n")
    out += ustar_header("backup/virtual-machine", 0, b"5")
    out += ustar_header("backup/virtual-machine/backup.yaml", 10) + pad(b"name: big\n")
    out += ustar_header("backup/virtual-machine/templates", 0, b"5")
    rec = pax_record("size", size)
    out += ustar_header("backup/PaxHeaders.0/virtual-machine.img", len(rec), b"x")
    out += pad(rec)
    out += ustar_header("backup/virtual-machine.img", 0)
    return out


def check_payload(label, payload):
    data = stream_with_payload(payload)
    m = TarMember(FakeProc(data), "virtual-machine.img")
    buf = bytearray(4096)
    view = memoryview(buf)
    got = bytearray()
    while True:
        n = m.readinto(view)
        if not n:
            break
        got += view[:n]
    over = m.readinto(memoryview(bytearray(1)))
    m.close()
    ok = bytes(got) == payload and m.size == len(payload) and not over
    print(f"  {'ok  ' if ok else 'FAIL'} {label:<32} read {len(got):>15,} "
          f"bytes, stops at the end")
    return ok


results = [
    check("small, plain octal", stream_small(SMALL), SMALL),
    check("large, PAX record", stream_pax(BIG), BIG),
    check("large, GNU base-256", stream_gnu(BIG), BIG),
    check("GNU long name", stream_longname(BIG), BIG),
    check("PAX sets path and size", stream_pax_path(BIG), BIG),
    expect_error("a container, not a VM", stream_missing(), "contains no"),
    expect_error("implausible size",
                 stream_gnu(1 << 50), "ceiling"),
    check_payload("payload read back whole", bytes(range(256)) * 37),
    check("the layout a real export has",
          stream_as_incus_writes_it(12 * (1 << 30)), 12 * (1 << 30)),
]
print("\nall good" if all(results) else "\nFAILURES")
sys.exit(0 if all(results) else 1)
