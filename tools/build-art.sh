#!/usr/bin/env bash
# Rebuild everything under xenproxy/art/ from the images in docs/logos/.
#
# The palette is set per size on purpose. Shrinking an illustration to
# thirty cells and then to sixteen needs fewer colours each time, or the
# quantiser spends them on gradients and the mark turns to confetti - but
# too few and it drops a brand colour instead, which is why these numbers
# are written down rather than derived.
set -euo pipefail
cd "$(dirname "$0")/.."

art() { python3 tools/img2art.py "docs/logos/$1" "$2" --widths "$3" --colours "$4"; }

art proxmox.webp proxmox 36 4
art proxmox.webp proxmox 24 3
art proxmox.webp proxmox 16 3

art xcp-ng.png   xcp-ng  36 7
art xcp-ng.png   xcp-ng  24 6
art xcp-ng.png   xcp-ng  16 5

art incus.png    incus   36 6
art incus.png    incus   24 7
art incus.png    incus   16 6

art xenproxy-mark.png xenproxy 36,24,16,12 5

# The name is coverage only; its colour comes from the theme at draw time.
python3 tools/img2art.py docs/logos/wordmark.png wordmark \
    --widths 140,104,80,60,44 --colours 6 --mask
