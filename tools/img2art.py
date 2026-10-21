#!/usr/bin/env python3
"""Turn a logo image into the cell art the UI ships.

One character per terminal cell, one colour per cell, and at draw time the
colour is painted as a background under a space - so nothing about the
picture depends on how a font draws a glyph. A cell is about twice as tall
as it is wide; the image is sampled on that grid, and the mark keeps its
proportions on screen.

Each cell is decided by a vote. The source is shrunk to a few samples per
cell, every sample is matched to the nearest ink in the logo's palette, and
the cell takes the ink most of its samples agree on. Averaging the samples
and matching the mean instead invents colours the logo does not have
wherever two inks meet - a pink seam between red and white, say - and the
edges come out smeared rather than drawn.

The output is a small text file: a palette of hex colours, then one line
per cell row, one character per cell - a base-36 palette index, or `.` for
nothing. The runtime needs no image library to read it.

    python3 tools/img2art.py proxmox.webp proxmox --widths 16,24,36
"""
from __future__ import annotations

import argparse
import os
import sys

from PIL import Image, ImageFilter

DIGITS = "0123456789abcdefghijklmnopqrstuvwxyz"
OUT_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                       "xenproxy", "art")

CELL_ASPECT = 2.0       # height of a terminal cell over its width
SUPER = 4               # samples per cell edge: 16 votes per cell
ALPHA_ON = 128          # a sample this opaque counts as drawn
COVER = 0.4             # share of drawn samples for the cell to be drawn


def resize_premultiplied(im: Image.Image, size) -> Image.Image:
    """Scale without letting the colour behind transparency bleed in.

    A fully transparent pixel still carries a colour, and in these files it
    is white, black or a leftover green. Resampling mixes that into the rim
    of every shape unless the channels are weighted by their own alpha
    first - which is exactly where the pale halo around a logo comes from.
    Box filtering is the plain area average: no ringing, so no overshoot
    for the vote to mistake for a third ink.
    """
    r, g, b, a = im.split()
    black = Image.new("L", im.size, 0)
    pre = [Image.composite(ch, black, a).resize(size, Image.BOX)
           for ch in (r, g, b)]
    small_a = a.resize(size, Image.BOX)
    ap = small_a.load()
    out = []
    for ch in pre:
        px = ch.load()
        plane = Image.new("L", size)
        pp = plane.load()
        for y in range(size[1]):
            for x in range(size[0]):
                alpha = ap[x, y]
                pp[x, y] = min(255, px[x, y] * 255 // alpha) if alpha else 0
        out.append(plane)
    return Image.merge("RGBA", (*out, small_a))


def seal_gaps(im: Image.Image, colour, radius: int):
    """Give the narrow separations inside a mark a colour of their own.

    A logo drawn for paper separates its shapes with the page showing
    through, and those separations run out to the edge rather than being
    enclosed. On a dark terminal they come out as cracks across the shape.
    Closing the alpha channel finds exactly the gaps narrower than the
    radius, and they get painted the colour the logo was drawn against.
    """
    k = radius * 2 + 1
    a = im.getchannel("A")
    closed = a.filter(ImageFilter.MaxFilter(k)).filter(ImageFilter.MinFilter(k))
    src, dst = a.load(), closed.load()
    out = im.copy()
    px = out.load()
    w, h = im.size
    sealed = 0
    for y in range(h):
        for x in range(w):
            if src[x, y] < ALPHA_ON <= dst[x, y]:
                px[x, y] = (*colour, 255)
                sealed += 1
    return out, sealed


def trim(im: Image.Image) -> Image.Image:
    """Crop to what is actually drawn, so the mark fills its grid."""
    box = im.getchannel("A").getbbox()
    return im.crop(box) if box else im


def source_palette(im: Image.Image, colours: int, merge: int = 44):
    """The colours the logo is actually drawn in.

    Taken from the full-size image, where a flat logo still has its five
    flat colours. Reading them off the shrunk one instead picks up the
    blends along every edge, and a palette of those turns Incus orange
    into brick and its greys into beige.
    """
    counts: dict = {}
    px = im.load()
    for y in range(im.height):
        for x in range(im.width):
            r, g, b, a = px[x, y]
            if a > 200:
                counts[(r, g, b)] = counts.get((r, g, b), 0) + 1
    ranked = sorted(counts.items(), key=lambda kv: -kv[1])
    chosen: list = []
    for col, _n in ranked:
        if len(chosen) >= colours:
            break
        # near-duplicates are the same ink with a different anti-alias
        if all(sum((col[i] - c[i]) ** 2 for i in range(3)) > merge * merge
               for c in chosen):
            chosen.append(col)
    return chosen or [(255, 255, 255)]


def sample(im: Image.Image, palette, cols: int, rows: int, cover: float,
           mask: bool = False):
    """Index grid, one entry per cell, decided by the vote described above.

    A cell is drawn when at least `cover` of its samples are; below that
    it is the rim of a shape and stays empty rather than becoming a
    smudge. For a mask the inks do not matter, only the coverage.
    """
    big = resize_premultiplied(im, (cols * SUPER, rows * SUPER))
    px = big.load()
    need = cover * SUPER * SUPER
    cache: dict = {}
    grid = []
    for cy in range(rows):
        row = []
        for cx in range(cols):
            tally: dict = {}
            drawn = 0
            for sy in range(SUPER):
                for sx in range(SUPER):
                    r, g, b, a = px[cx * SUPER + sx, cy * SUPER + sy]
                    if a < ALPHA_ON:
                        continue
                    drawn += 1
                    if mask:
                        continue
                    c = (r, g, b)
                    i = cache.get(c)
                    if i is None:
                        i = min(range(len(palette)),
                                key=lambda k: sum((c[j] - palette[k][j]) ** 2
                                                  for j in range(3)))
                        cache[c] = i
                    tally[i] = tally.get(i, 0) + 1
            if drawn < need:
                row.append(None)
            elif mask:
                row.append(0)
            else:
                row.append(max(tally.items(), key=lambda kv: kv[1])[0])
        grid.append(row)
    return grid


def despeckle(grid, rounds: int = 3):
    """Let the majority of a cell's neighbours decide what it is.

    Shrinking a detailed illustration to thirty-odd cells leaves a cell
    here and there whose colour matches nothing around it. Flattening
    those into whatever surrounds them is what the eye does anyway at this
    size, and it is the difference between a mark and a smudge.
    """
    h = len(grid)
    for _ in range(rounds):
        changed = 0
        out = [row[:] for row in grid]
        for y in range(h):
            for x in range(len(grid[y])):
                here = grid[y][x]
                tally: dict = {}
                same = 0
                for dy in (-1, 0, 1):
                    for dx in (-1, 0, 1):
                        if dy == dx == 0:
                            continue
                        yy, xx = y + dy, x + dx
                        if 0 <= yy < h and 0 <= xx < len(grid[yy]):
                            v = grid[yy][xx]
                            tally[v] = tally.get(v, 0) + 1
                            if v == here:
                                same += 1
                if same > 1 or not tally:
                    continue
                best, count = max(tally.items(), key=lambda kv: kv[1])
                # a cell with no neighbour of its own colour is noise
                # already; one with a single such neighbour is the end
                # of something and needs a clear majority against it
                if count >= (3 if same == 0 else 5) and best != here:
                    out[y][x] = best
                    changed += 1
        grid = out
        if not changed:
            break
    return grid


def render(path: str, name: str, width: int, colours: int,
           hole_fill=None, seal_radius: int = 12, mask: bool = False,
           clean: bool = True, cover: float = COVER) -> str:
    im = Image.open(path).convert("RGBA")
    original = im
    if hole_fill:
        im, sealed = seal_gaps(im, hole_fill, seal_radius)
        if sealed:
            print(f"    sealed {sealed} pixels of separation")
    im = trim(im)
    # the box is `width` cells wide and as tall as that is on screen
    scale = min(width / im.width, width / im.height)
    cols = max(1, round(im.width * scale))
    rows = max(1, round(im.height * scale / CELL_ASPECT))
    palette = [(255, 255, 255)] if mask else source_palette(original, colours)
    grid = sample(im, palette, cols, rows, cover, mask)
    if clean and not mask:
        grid = despeckle(grid)

    used = sorted({v for row in grid for v in row if v is not None})
    remap = {old: i for i, old in enumerate(used)}
    left = (width - cols) // 2
    lines = [f"# {name} {width}x{rows} cells"]
    lines.append("palette " + " ".join(
        "%02x%02x%02x" % palette[o] for o in used))
    for row in grid:
        body = "".join("." if v is None else DIGITS[remap[v]] for v in row)
        lines.append("." * left + body + "." * (width - cols - left))
    return "\n".join(lines) + "\n"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("image")
    ap.add_argument("name")
    ap.add_argument("--widths", default="16,24,36")
    ap.add_argument("--colours", type=int, default=12)
    ap.add_argument("--cover", type=float, default=COVER,
                    help="share of a cell that must be drawn for the cell "
                         "to be - lower makes thin strokes survive")
    ap.add_argument("--no-clean", action="store_true",
                    help="keep every stray cell the downscale produced")
    ap.add_argument("--mask", action="store_true",
                    help="ship coverage only; the colour is applied when the "
                         "art is drawn, so it follows the theme")
    ap.add_argument("--seal-radius", type=int, default=12)
    ap.add_argument("--fill-holes", default="",
                    help="hex colour for transparent gaps inside the mark, "
                         "e.g. ffffff - they are cracks on a dark terminal")
    args = ap.parse_args()
    hole = None
    if args.fill_holes:
        h = args.fill_holes.lstrip("#")
        hole = (int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16))
    os.makedirs(OUT_DIR, exist_ok=True)
    for w in [int(x) for x in args.widths.split(",")]:
        text = render(args.image, args.name, w, args.colours, hole,
                      args.seal_radius, args.mask, not args.no_clean,
                      args.cover)
        out = os.path.join(OUT_DIR, f"{args.name}-{w}.art")
        with open(out, "w", encoding="utf-8") as fh:
            fh.write(text)
        rows = len(text.splitlines()) - 2
        print(f"  {os.path.basename(out):<20} {w}x{rows} cells, "
              f"{len(text)} bytes")
    return 0


if __name__ == "__main__":
    sys.exit(main())
