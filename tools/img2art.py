#!/usr/bin/env python3
"""Turn a logo image into the colour cell art the UI ships.

A terminal cell is about twice as tall as it is wide, and U+2580 splits one
into an upper and a lower half. Two stacked pixels therefore fit in one
cell and come out square, which is why the grids below are W x 2H.

The output is a small text file: a palette of hex colours, then one line
per pixel row, one character per pixel - a base-36 palette index, or `.`
for transparent. The runtime needs no image library to read it.

    python3 tools/img2art.py proxmox.webp proxmox --widths 16,24,36
"""
from __future__ import annotations

import argparse
import os
import sys

from PIL import Image

DIGITS = "0123456789abcdefghijklmnopqrstuvwxyz"
OUT_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                       "xenproxy", "art")


def trim(im: Image.Image) -> Image.Image:
    """Crop to what is actually drawn, so the mark fills its grid."""
    alpha = im.getchannel("A")
    box = alpha.getbbox()
    return im.crop(box) if box else im


def quantise(im: Image.Image, colours: int):
    """(palette, index grid) with transparent pixels marked as None."""
    rgb = Image.new("RGB", im.size, (0, 0, 0))
    rgb.paste(im.convert("RGB"), (0, 0))
    pal_img = rgb.convert("P", palette=Image.ADAPTIVE, colors=colours)
    raw = pal_img.getpalette() or []
    palette = [tuple(raw[i * 3:i * 3 + 3]) for i in range(colours)]
    alpha = im.getchannel("A").load()
    idx = pal_img.load()
    w, h = im.size
    grid = [[None if alpha[x, y] < 128 else idx[x, y] for x in range(w)]
            for y in range(h)]
    return palette, grid


def render(path: str, name: str, width: int, colours: int) -> str:
    im = trim(Image.open(path).convert("RGBA"))
    # square pixels: the grid is as tall as it is wide, and the renderer
    # folds every two rows into one cell
    side = width
    scale = min(side / im.width, side / im.height)
    tw, th = max(1, round(im.width * scale)), max(1, round(im.height * scale))
    im = im.resize((tw, th), Image.LANCZOS)
    if th % 2:
        th += 1
        im = im.crop((0, 0, tw, th))        # keeps the pairing even
    canvas = Image.new("RGBA", (side, th), (0, 0, 0, 0))
    canvas.paste(im, ((side - tw) // 2, 0))

    palette, grid = quantise(canvas, colours)
    used = sorted({v for row in grid for v in row if v is not None})
    remap = {old: i for i, old in enumerate(used)}
    lines = [f"# {name} {side}x{th // 2} cells"]
    lines.append("palette " + " ".join(
        "%02x%02x%02x" % palette[o] for o in used))
    for row in grid:
        lines.append("".join("." if v is None else DIGITS[remap[v]]
                             for v in row))
    return "\n".join(lines) + "\n"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("image")
    ap.add_argument("name")
    ap.add_argument("--widths", default="16,24,36")
    ap.add_argument("--colours", type=int, default=12)
    args = ap.parse_args()
    os.makedirs(OUT_DIR, exist_ok=True)
    for w in [int(x) for x in args.widths.split(",")]:
        text = render(args.image, args.name, w, args.colours)
        out = os.path.join(OUT_DIR, f"{args.name}-{w}.art")
        with open(out, "w", encoding="utf-8") as fh:
            fh.write(text)
        rows = len(text.splitlines()) - 2
        print(f"  {os.path.basename(out):<20} {w}x{rows // 2} cells, "
              f"{len(text)} bytes")
    return 0


if __name__ == "__main__":
    sys.exit(main())
