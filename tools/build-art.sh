#!/usr/bin/env bash
# Rebuild everything under xenproxy/art/ from the images in docs/logos/.
#
# The palette is set per size on purpose. Shrinking an illustration to
# thirty cells and then to sixteen needs fewer colours each time, or the
# vote spends them on gradients and the mark turns to confetti - but too
# few and it drops a brand colour instead, which is why these numbers are
# written down rather than derived.
set -euo pipefail
cd "$(dirname "$0")/.."

art() { python3 tools/img2art.py "docs/logos/$1" "$2" --widths "$3" --colours "$4"; }

# The counts are the number of inks the logo is actually drawn with, read
# off the full-size image. A flat mark needs exactly its own colours; the
# illustration needs enough to keep the rocket apart from the sky, and the
# despeckle pass clears up what is left.
art proxmox.webp proxmox 48,36,24,16 2

art xcp-ng.png   xcp-ng  48 8
art xcp-ng.png   xcp-ng  36 8
art xcp-ng.png   xcp-ng  24 7
art xcp-ng.png   xcp-ng  16 6

art incus.png    incus   48,36,24,16 5

art xenproxy-mark.png xenproxy 48,36,24,16,12 2

# The name is coverage only; its colour comes from the theme at draw time.
# Below seven rows the counters of the letters close up and the name stops
# being readable, so it is not shipped smaller than that: a terminal too
# narrow for it gets the name as text.
python3 tools/img2art.py docs/logos/wordmark.png wordmark \
    --widths 140,104,80 --mask --cover 0.45
