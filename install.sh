#!/usr/bin/env bash
# xenproxY installer
#   curl -fsSL https://raw.githubusercontent.com/leanderkretschmer/xenproxY/main/install.sh | bash
set -euo pipefail

REPO="${XENPROXY_REPO:-leanderkretschmer/xenproxY}"
REF="${XENPROXY_REF:-}"
PREFIX="${XENPROXY_PREFIX:-/opt/xenproxy}"
BINDIR="${XENPROXY_BINDIR:-/usr/local/bin}"
CONFDIR="/etc/xenproxy"
STATEDIR="/var/lib/xenproxy"

c_reset=$'\e[0m'; c_dim=$'\e[2m'; c_b=$'\e[1m'
c_pri=$'\e[38;5;75m'; c_ok=$'\e[38;5;114m'; c_warn=$'\e[38;5;179m'; c_err=$'\e[38;5;203m'

say()  { printf '%s\n' "  $*"; }
ok()   { printf '%s\n' "  ${c_ok}✓${c_reset} $*"; }
warn() { printf '%s\n' "  ${c_warn}!${c_reset} $*"; }
die()  { printf '%s\n' "  ${c_err}✗${c_reset} $*" >&2; exit 1; }

banner() {
  printf '\n%s\n' "  ${c_pri}${c_b}xenproxY${c_reset} ${c_dim}- XenServer/XCP-ng to Proxmox VE${c_reset}"
  printf '%s\n\n' "  ${c_dim}──────────────────────────────────────────────${c_reset}"
}

banner
[ "$(id -u)" -eq 0 ] || die "run this as root"

command -v python3 >/dev/null 2>&1 || die "python3 is required"
PYV=$(python3 -c 'import sys;print("%d.%d"%sys.version_info[:2])')
python3 -c 'import sys;sys.exit(0 if sys.version_info>=(3,9) else 1)' \
  || die "python3 >= 3.9 is required (found $PYV)"
python3 -c 'import curses' 2>/dev/null || warn "python3 curses is missing - the TUI falls back to plain ANSI"
ok "python3 $PYV"

command -v curl >/dev/null 2>&1 || die "curl is required"
command -v tar  >/dev/null 2>&1 || die "tar is required"

if [ -x /usr/sbin/qm ] && [ -d /etc/pve ]; then
  ok "Proxmox VE node detected: $(hostname)"
else
  warn "this does not look like a Proxmox VE node - migrations will refuse to run"
fi

if [ -z "$REF" ]; then
  REF=$(curl -fsSL "https://api.github.com/repos/${REPO}/releases/latest" 2>/dev/null \
        | sed -n 's/.*"tag_name"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p' | head -1 || true)
  [ -n "$REF" ] || REF="main"
fi
say "installing ${c_b}${REF}${c_reset} from ${REPO}"

TMP=$(mktemp -d /tmp/xenproxy-install.XXXXXX)
trap 'rm -rf "$TMP"' EXIT
cd "$TMP"

URL="https://codeload.github.com/${REPO}/tar.gz/${REF}"
curl -fsSL "$URL" -o src.tar.gz || die "download failed: $URL"
mkdir -p src && tar -xzf src.tar.gz -C src --strip-components=1
[ -d src/xenproxy ] || die "the archive carries no xenproxy package"
[ -f src/xenproxy/cli.py ] || die "the archive looks incomplete"

VERSION=$(sed -n 's/^__version__ *= *"\([^"]*\)".*/\1/p' src/xenproxy/__init__.py | head -1)
OLD=""
[ -f "$PREFIX/xenproxy/__init__.py" ] && \
  OLD=$(sed -n 's/^__version__ *= *"\([^"]*\)".*/\1/p' "$PREFIX/xenproxy/__init__.py" | head -1)

install -d -m 0755 "$PREFIX"
rm -rf "$PREFIX/xenproxy"
cp -a src/xenproxy "$PREFIX/xenproxy"
cp -a src/install.sh "$PREFIX/install.sh" 2>/dev/null || true
[ -f src/README.md ] && cp -a src/README.md "$PREFIX/README.md"
find "$PREFIX" -name '__pycache__' -type d -exec rm -rf {} + 2>/dev/null || true
chmod -R a+rX "$PREFIX"

install -d -m 0700 "$CONFDIR" "$CONFDIR/keys"
install -d -m 0755 "$STATEDIR" "$STATEDIR/jobs"

cat > "$BINDIR/xenproxY" <<LAUNCH
#!/bin/sh
# xenproxY launcher
cd /
PYTHONPATH="$PREFIX\${PYTHONPATH:+:\$PYTHONPATH}"
export PYTHONPATH
exec python3 -s -m xenproxy "\$@"
LAUNCH
chmod 0755 "$BINDIR/xenproxY"
ln -sf "$BINDIR/xenproxY" "$BINDIR/xenproxy"

if [ -n "$OLD" ] && [ "$OLD" != "$VERSION" ]; then
  ok "updated ${OLD} → ${VERSION}"
elif [ -n "$OLD" ]; then
  ok "reinstalled ${VERSION}"
else
  ok "installed ${VERSION}"
fi
say "${c_dim}binary${c_reset}  $BINDIR/xenproxY"
say "${c_dim}config${c_reset}  $CONFDIR/config.json"

printf '\n'
if [ -f "$CONFDIR/config.json" ]; then
  say "run ${c_b}xenproxY${c_reset} to continue"
else
  say "run ${c_b}xenproxY${c_reset} and add your first Xen server"
fi
printf '\n'
