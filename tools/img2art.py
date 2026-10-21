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

from PIL import Image, ImageFilter

DIGITS = "0123456789abcdefghijklmnopqrstuvwxyz"
OUT_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                       "xenproxy", "art")


# Edge pixels that are mostly there are kept. Dropping everything below
# half opacity eats the anti-aliased rim of a shape and leaves the result
# looking nibbled.
ALPHA_FLOOR = 96


def resize_premultiplied(im: Image.Image, size) -> Image.Image:
    """Scale without letting the colour behind transparency bleed in.

    A fully transparent pixel still carries a colour, and in these files it
    is white, black or a leftover green. Resampling mixes that into the rim
    of every shape unless the channels are weighted by their own alpha
    first - which is exactly where the pale halo around a logo comes from.
    """
    r, g, b, a = im.split()
    af = a.point(lambda v: v)
    pre = [Image.composite(ch, Image.new("L", im.size, 0), af)
           for ch in (r, g, b)]
    pre = [ch.resize(size, Image.LANCZOS) for ch in pre]
    small_a = a.resize(size, Image.LANCZOS)
    out = []
    ap = small_a.load()
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
            if src[x, y] < ALPHA_FLOOR <= dst[x, y]:
                px[x, y] = (*colour, 255)
                sealed += 1
    return out, sealed


def fill_holes(im: Image.Image, colour) -> Image.Image:
    """Close the transparent gaps *inside* a mark.

    A logo drawn for paper separates its shapes with the page showing
    through. On a dark terminal that page is not there, so the separations
    come out as cracks running across the shape. Anything transparent that
    cannot be reached from the edge of the image is interior, and gets the
    colour the logo was drawn against.
    """
    w, h = im.size
    alpha = im.getchannel("A").load()
    outside = bytearray(w * h)
    stack = []
    for x in range(w):
        for y in (0, h - 1):
            if alpha[x, y] < ALPHA_FLOOR:
                stack.append((x, y))
    for y in range(h):
        for x in (0, w - 1):
            if alpha[x, y] < ALPHA_FLOOR:
                stack.append((x, y))
    while stack:
        x, y = stack.pop()
        i = y * w + x
        if outside[i]:
            continue
        outside[i] = 1
        for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1)):
            nx, ny = x + dx, y + dy
            if 0 <= nx < w and 0 <= ny < h and not outside[ny * w + nx] \
                    and alpha[nx, ny] < ALPHA_FLOOR:
                stack.append((nx, ny))
    out = im.copy()
    px = out.load()
    filled = 0
    for y in range(h):
        for x in range(w):
            if alpha[x, y] < ALPHA_FLOOR and not outside[y * w + x]:
                px[x, y] = (*colour, 255)
                filled += 1
    return out, filled


def trim(im: Image.Image) -> Image.Image:
    """Crop to what is actually drawn, so the mark fills its grid."""
    alpha = im.getchannel("A")
    box = alpha.getbbox()
    return im.crop(box) if box else im


def as_mask(im: Image.Image, levels: int):
    """Coverage only, as a grey ramp.

    Used for the wordmark: the shape is shipped, the colour is decided when
    it is drawn, so it follows the theme instead of being baked in.
    """
    alpha = im.getchannel("A").load()
    w, h = im.size
    palette = [(0, 0, 0)] + [
        (round(255 * (i + 1) / levels),) * 3 for i in range(levels)]
    grid = []
    for y in range(h):
        row = []
        for x in range(w):
            a = alpha[x, y]
            row.append(None if a < ALPHA_FLOOR
                       else min(levels, 1 + a * levels // 256))
            if row[-1] == 0:
                row[-1] = 1
        grid.append(row)
    return palette, grid


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


def map_to(im: Image.Image, palette):
    """Nearest palette entry per pixel, with transparency kept apart."""
    alpha = im.getchannel("A").load()
    px = im.load()
    cache: dict = {}
    grid = []
    for y in range(im.height):
        row = []
        for x in range(im.width):
            if alpha[x, y] < ALPHA_FLOOR:
                row.append(None)
                continue
            c = px[x, y][:3]
            hit = cache.get(c)
            if hit is None:
                hit = min(range(len(palette)),
                          key=lambda i: sum((c[j] - palette[i][j]) ** 2
                                            for j in range(3)))
                cache[c] = hit
            row.append(hit)
        grid.append(row)
    return grid


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
    grid = [[None if alpha[x, y] < ALPHA_FLOOR else idx[x, y]
             for x in range(w)] for y in range(h)]
    return palette, grid


def despeckle(grid, rounds: int = 3):
    """Let the majority of a pixel's neighbours decide what it is.

    Shrinking a detailed illustration to thirty-odd pixels turns its
    gradients into confetti: a pixel here and there whose colour matches
    nothing around it. Flattening those into whatever surrounds them is
    what the eye does anyway at this size, and it is the difference
    between a mark and a smudge.
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
                if count >= 5 and best != here:
                    out[y][x] = best
                    changed += 1
        grid = out
        if not changed:
            break
    return grid


def render(path: str, name: str, width: int, colours: int,
           hole_fill=None, seal_radius: int = 12, mask: bool = False,
           clean: bool = True, adaptive: bool = False) -> str:
    im = Image.open(path).convert("RGBA")
    original = im
    if hole_fill:
        im, sealed = seal_gaps(im, hole_fill, seal_radius)
        if sealed:
            print(f"    sealed {sealed} pixels of separation")
    im = trim(im)
    # square pixels: the grid is as tall as it is wide, and the renderer
    # folds every two rows into one cell
    side = width
    scale = min(side / im.width, side / im.height)
    tw, th = max(1, round(im.width * scale)), max(1, round(im.height * scale))
    im = resize_premultiplied(im, (tw, th))
    if th % 2:
        th += 1
        im = im.crop((0, 0, tw, th))        # keeps the pairing even
    canvas = Image.new("RGBA", (side, th), (0, 0, 0, 0))
    canvas.paste(im, ((side - tw) // 2, 0))

    if mask:
        palette, grid = as_mask(canvas, colours)
    elif adaptive:
        palette, grid = quantise(canvas, colours)
    else:
        palette = source_palette(original, colours)
        grid = map_to(canvas, palette)
    if clean:
        grid = despeckle(grid)
    palette = list(palette)
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
    ap.add_argument("--adaptive", action="store_true",
                    help="let the quantiser pick colours from the shrunk "
                         "image instead of reading them off the original")
    ap.add_argument("--no-clean", action="store_true",
                    help="keep every stray pixel the downscale produced")
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
                      args.adaptive)
        out = os.path.join(OUT_DIR, f"{args.name}-{w}.art")
        with open(out, "w", encoding="utf-8") as fh:
            fh.write(text)
        rows = len(text.splitlines()) - 2
        print(f"  {os.path.basename(out):<20} {w}x{rows // 2} cells, "
              f"{len(text)} bytes")
    return 0


if __name__ == "__main__":
    sys.exit(main())
