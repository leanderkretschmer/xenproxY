# xenproxY

Migrate XenServer / XCP-ng and Incus guests to Proxmox VE — from an interactive
terminal UI that runs on the Proxmox node itself, with the keyboard or the
mouse.

![Browsing the guests on a Xen host](docs/screens/guests.svg)

## Install

```sh
curl -fsSL https://raw.githubusercontent.com/leanderkretschmer/xenproxY/main/install.sh | bash
```

Run it on the **Proxmox VE node** that will receive the guests. The installer
needs root, drops the package into `/opt/xenproxy` and puts a launcher at
`/usr/local/bin/xenproxY`. Nothing is installed on the source side.

Requirements: Python 3.9+ (Proxmox ships it), `curl`, and network access from
the Proxmox node to the source host — port 22 for both kinds, plus 443 for
XenServer/XCP-ng.

To pin a version: `XENPROXY_REF=v0.2.0 bash install.sh`.

## First run

```sh
xenproxY
```

With no server configured yet it opens the setup dialog. Pick the kind of
source, then enter host, user and credentials. The SSH host key is shown for
confirmation first, and only once you accept it does anything leave the
machine; then the connection is checked **before** the entry is stored.

![First run: adding a source server](docs/screens/setup.svg)

For a Xen host the password is required even if you later switch SSH to a key,
because the disk export runs through the XAPI HTTPS endpoint. An Incus host
needs no password at all when SSH uses a key: the disks come down the SSH
session itself.

Once saved the host shows up in the server list, where `t` re-checks the
connection and **Open** lists its guests.

![The configured source servers](docs/screens/servers.svg)

Configuration lives in `/etc/xenproxy/config.json`, mode `0600`, with the
accepted host keys next to it in `/etc/xenproxy/known_hosts`.

## Finding your way around

Three rules cover the whole interface, and the footer always names the one
that applies right now:

| | |
|---|---|
| `tab` / `shift-tab` | move between the panes of a screen: **menu → content → actions** |
| `↑` `↓` `←` `→` | move inside whatever has focus |
| `⏎` | run the highlighted action — always drawn as a button |

Every screen carries its actions as a row of buttons above the footer, each
with its own shortcut on the face, so there is no key list to memorise. `1`-`4`
jump straight to a section, `esc` goes back, and `?` opens this:

![Keyboard and mouse](docs/screens/help.svg)

### Mouse

The pointer reaches everything the keyboard does. Click a menu entry, a list
row, a form field, a radio option, a switch or a button; click a row that is
already selected to run its action; roll the wheel to scroll the list or log
under the pointer. Breadcrumb steps in the migration wizard are clickable too,
so a value can be corrected without walking back through the pages.

While the terminal reports mouse events it stops doing its own text selection,
so `shift-M` turns reporting off and on at any time; the same switch sits in
Settings and is remembered.

## What it does

Navigate the guests on the source host, inspect disks and NICs, then press `m`
to open the migration wizard:

| Step | Choice |
|---|---|
| **Mode** | `offline`, `snapshot` or `live` |
| **Target** | VMID, name, storage pool |
| **Network** | keep MAC/VLAN, new settings, defer, or no NIC |
| **Hardware** | cores, sockets, memory, firmware, machine type, CPU |
| **Options** | start afterwards, adapt guest, verify, start on boot |
| **Review** | summary plus warnings, `⏎` starts |

![Wizard step one: the copy mode](docs/screens/wizard-mode.svg)

The last step restates the whole plan and calls out anything worth a second
look — a source that is about to be shut down, a VMID that is taken, a MAC
that would collide with the still-running source.

![Wizard review step](docs/screens/wizard-review.svg)

The Jobs screen shows throughput, per-step state and a live log; finished runs
are kept in `/var/lib/xenproxy/jobs`.

![A migration in flight](docs/screens/job.svg)

### Copy modes

| Mode | Downtime | Change on the source | Consistency |
|---|---|---|---|
| `offline` | yes, for the copy | power state only | full |
| `snapshot` | none | temporary objects, removed afterwards | full, at snapshot time |
| `live` | none | **none** | crash-consistent |

`live` reads the volume while the guest runs, so the guest replays its journal
on first boot. Use it when the source must not be touched at all; use `offline`
when the data matters more than the downtime.

On Incus, `snapshot` mode needs a little room: `incus export` always exports an
instance as it stands, so the snapshot is copied into a throwaway instance,
that is what gets exported, and both are removed afterwards.

## Sources

### XenServer / XCP-ng

Disks are pulled as a raw stream from the XAPI endpoint `export_raw_vdi`
straight onto the freshly allocated Proxmox volume — one pass, no staging file,
no intermediate conversion. XAPI resolves VHD differencing chains itself, so
snapshots and chained base images come out whole.

The inventory comes from `xe`, whose output is text; see **Security** below for
what that implies and how it is handled.

### Incus

![Guests on an Incus host](docs/screens/incus.svg)

Virtual machines are listed with their memory, CPU, firmware and NIC; the root
volume is pulled with `incus --quiet export <name> - --compression none`, whose
uncompressed tar stream is unpacked while it arrives. The member
`backup/virtual-machine.img` inside it is the raw disk, and its tar header
carries the authoritative byte count the target volume is sized from.

`--quiet` is not cosmetic: without it `incus` writes its progress bar to the
same stdout as the tarball.

Two things to know:

- **Containers are not listed.** They have no block device to hand to KVM.
- **Extra block devices are separate Incus volumes** and are not part of the
  instance export. The wizard says so when it sees any; copy them with
  `incus storage volume export`.

Incus runs its VMs on OVMF, so migrated guests are created with `bios: ovmf`
and get an EFI disk on the target.

With *verify* on (default), the checksum of the received stream is compared
against a read-back of the written volume, whichever source it came from.

## Guest adaptation

Two things routinely break a guest that has just changed hypervisor, and both
are handled when *adapt guest* is on:

- **The NIC is renamed.** Under Xen a paravirtual NIC is called `eth0` because
  it has no PCI path; under KVM a virtio NIC becomes `ens18`, and a static
  `/etc/network/interfaces` stops matching. xenproxY mounts the migrated root
  volume and pins `net.ifnames=0 biosdevname=0` in `/etc/default/grub` and in
  the existing `grub.cfg` entries, keeping backups as `*.pre-xenproxy`.
- **A UEFI guest loses its firmware boot entry.** That entry lives in the
  source hypervisor's NVRAM and names the guest's loader; the target gets an
  empty one and falls back to `\EFI\BOOT\BOOTX64.EFI`. When the ESP has no
  such file, the first loader found under `EFI/` is copied into that path (and
  `grubx64.efi` alongside it when the loader is shim), so the guest starts
  instead of looking for a PXE server.

It also warns when `/etc/fstab` names devices directly or the initramfs
carries no virtio modules.

Themes, defaults, the mouse switch and the update check live under Settings —
`tokyo-night`, `dracula`, `nord`, `gruvbox`, `default` and `high-contrast`,
each with rounded, single, double or plain ASCII borders.

![Settings](docs/screens/settings.svg)

## Command line

Everything the TUI does is scriptable:

```sh
xenproxY servers                           # configured source hosts
xenproxY add --name xen1 --host 10.0.0.5   # shows the host key, then prompts
printf '%s\n' "$PW" | xenproxY add --name xen1 --host 10.0.0.5 \
    --host-key-fingerprint SHA256:jCTxF+T3ZM3IL50Cb3ouMuUBCn82v7 --password-stdin
xenproxY add --kind incus --name incus1 --host 10.0.0.40 \
    --key /etc/xenproxy/keys/incus1_ed25519 --accept-host-key --password-stdin </dev/null
xenproxY test --server incus1
xenproxY list --server incus1              # guests on that host
xenproxY show db-replica --server incus1
xenproxY migrate db-replica --server incus1 \
    --mode offline --storage local-zfs --vmid 210 \
    --net keep --bridge vmbr0 --vlan 1051
xenproxY migrate some-vm --dry-run         # print the plan, change nothing
xenproxY jobs
xenproxY update --check
```

## Updates

On start xenproxY asks GitHub for the newest published version and offers to
update when one exists; that check stays quiet otherwise, so a node without
internet is not nagged at every launch.

**Check for updates** in Settings always says what it found — a newer version,
that you are on the newest one, that nothing is published yet, or why GitHub
could not be asked (an unauthenticated API allows 60 requests an hour per
address, which a node behind a busy NAT does run into). The answer also stays
on the Settings screen rather than only in a toast.

**Reinstall** re-runs the installer against the current `main` branch. That is
the way to move to a newer commit when no release is tagged for it, and it is
the same thing as running the one-liner by hand:

```sh
curl -fsSL https://raw.githubusercontent.com/leanderkretschmer/xenproxY/main/install.sh | bash
xenproxY update --check          # report only
xenproxY update --force --yes    # reinstall from main without asking
```

Either way `/opt/xenproxy` is replaced and `/etc/xenproxy` is left alone, so
servers, host keys and settings survive.

## Security

xenproxY runs as root on the hypervisor and handles images authored
elsewhere, so a few things are deliberate:

- **The SSH host key is confirmed before anything is sent.** On first
  contact the host keys are fetched with `ssh-keyscan`, which authenticates
  nothing, and shown for comparison; only after you accept them are they
  written to `/etc/xenproxy/known_hosts` and only then does a password
  travel. Afterwards `StrictHostKeyChecking=yes` applies against that file,
  so a changed key stops the run instead of being absorbed. This is the
  first link in the chain: trusting whatever answers on first contact
  would hand a man in the middle both the host password and the
  certificate fingerprint below. Non-interactive use passes
  `--host-key-fingerprint SHA256:...`.
- **The XAPI certificate is pinned, and the channel fails closed.** When a
  Xen server is added, its SHA-256 fingerprint is read from
  `/etc/xensource/xapi-ssl.pem` over that now-verified SSH session and
  stored alongside it. Every disk export verifies that fingerprint
  right after the TLS handshake and aborts *before* the credentials go
  out if it does not match. XCP-ng ships a self-signed certificate, so
  ordinary CA verification is not an option, and trusting it blindly
  would put the host password on the wire for anyone in the path. If
  neither a pin nor `verify_tls` is available the export refuses to run;
  `insecure_tls` on the server entry is the deliberate way to override
  that. An Incus source needs none of this: its disks never leave the
  authenticated SSH session.
- **Remote output is never trusted to be well formed.** `xe` prints a text
  format, so a guest name carrying a newline could otherwise forge whole
  records: records are only opened at a syntactically valid UUID, the
  listing is cross-checked against `xe ... --minimal`, and a repeated key
  or UUID stops the run rather than being silently resolved. Incus answers
  in JSON, where the structure is decided by the encoder, and instance
  names are matched against the hostname grammar before they are used.
- **The migrated image is treated as untrusted.** It is mounted
  `nosuid,nodev,noexec`, and every file the guest adaptation touches is
  opened component by component with `O_NOFOLLOW` and `O_NONBLOCK`, then
  checked to be a regular file. A symlink inside the image — say
  `/etc/default/grub` pointing at `/etc/shadow` — would otherwise
  resolve against the *host* root and let a prepared image overwrite
  host files, and a FIFO in the same place would hang the migration
  until something wrote to it. Both are refused and reported by name.
- **Values coming from the source are scrubbed of control characters**
  before they reach the terminal, so a guest named with an embedded escape
  sequence cannot repaint the screen or drive the emulator. Parsed
  fields are cleaned at the parser, and job log lines and notifications
  are cleaned again where they are rendered, because remote error text
  reaches the screen that way too.
- **The SSH askpass helper** lives in a private `0700` directory, is
  re-verified as a root-owned regular file before every reuse and is
  removed at exit — it is a small executable that prints a secret.
- **Values that end up in a `qm` property string are validated**, not
  just interpolated. A MAC has to match a MAC exactly, a bridge name has
  to look like a bridge name, a VLAN tag has to be an integer in range —
  otherwise it is dropped or the migration stops. Otherwise a crafted
  NIC definition could smuggle extra options into `net0`.
- **A disk stream is capped at the size it declared.** Anything beyond
  that is an error rather than something that keeps being written, and a
  tar member claiming an implausible length is refused before a volume is
  allocated for it.
- Credentials live in `/etc/xenproxy/config.json`, mode `0600`, owned by
  root. There is deliberately no `--password` flag, because argv is
  readable from the process list: the password is prompted for, or read
  with `--password-stdin`, or taken from `XENPROXY_PASSWORD`.

## Notes

- The source guest is never deleted. After a successful migration it stays on
  the source host — stopped in `offline` mode, untouched otherwise — so you
  keep a fallback.
- Guest and source share a MAC and IP. Do not run both on the same VLAN; the
  wizard warns when a plan would do that.
- `qemu-guest-agent` is not installed automatically. Install it inside the
  guest and set `agent: 1` afterwards.
- Only the first disk is adapted for KVM; extra data disks are copied as-is.

## Development

The images above are rendered straight from the real UI code, so they cannot
drift away from what the tool draws:

```sh
python3 tools/make_screenshots.py    # -> docs/screens/*.svg
```

`tools/ansi2svg.py` turns the renderer's ANSI output into SVG. Box-drawing
and shading characters are emitted as vectors rather than glyphs, so borders
connect and progress bars stay solid at any zoom level.

## Licence

MIT — see [LICENSE](LICENSE).
