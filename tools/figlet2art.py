#!/usr/bin/env python3
"""Render a word from a FIGlet font into a plain art file.

Only the full-width layout is implemented - no smushing - because the
wordmark is rendered once at build time and shipped, so the runtime needs
neither a font file nor this code.

    python3 tools/figlet2art.py X992.flf xenproxY wordmark-full

`--drop` removes characters from the result. The decorative faces carry
loose accents and apostrophes that read as grit at terminal size rather
than as part of a letter, and taking them out leaves the shape intact.
"""
from __future__ import annotations

import argparse
import os
import sys

OUT_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                       "xenproxy", "art")


def load(path: str) -> tuple[dict, int]:
    with open(path, encoding="utf-8", errors="replace") as fh:
        raw = fh.read().splitlines()
    head = raw[0].split()
    if not head or not head[0].startswith("flf2a"):
        raise SystemExit(f"{path}: not a FIGlet font")
    hard = head[0][-1]
    height, comments = int(head[1]), int(head[5])
    body = raw[1 + comments:]
    chars: dict = {}
    for i, code in enumerate(range(32, 127)):
        block = body[i * height:(i + 1) * height]
        if len(block) < height:
            break
        rows = []
        for line in block:
            if line:
                line = line.rstrip(line[-1])     # one or two endmarks
            rows.append(line.replace(hard, " "))
        chars[chr(code)] = rows
    return chars, height


def render(chars: dict, height: int, text: str) -> list[str]:
    out = [""] * height
    for ch in text:
        glyph = chars.get(ch) or chars.get(" ")
        for r in range(height):
            out[r] += glyph[r]
    while out and not out[0].strip():
        out.pop(0)
    while out and not out[-1].strip():
        out.pop()
    return [l.rstrip() for l in out]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("font")
    ap.add_argument("text")
    ap.add_argument("name")
    ap.add_argument("--drop", default="",
                    help="characters to blank out of the finished art")
    args = ap.parse_args()
    chars, height = load(args.font)
    art = render(chars, height, args.text)
    if args.drop:
        drop = set(args.drop)
        art = ["".join(" " if c in drop else c for c in line).rstrip()
               for line in art]
        while art and not art[0].strip():
            art.pop(0)
        while art and not art[-1].strip():
            art.pop()
    os.makedirs(OUT_DIR, exist_ok=True)
    out = os.path.join(OUT_DIR, f"{args.name}.txt")
    with open(out, "w", encoding="utf-8") as fh:
        fh.write("\n".join(art) + "\n")
    ink = sum(1 for line in art for c in line if c != " ")
    print(f"  {os.path.basename(out):<22} "
          f"{max(len(l) for l in art)}x{len(art)}  {ink} marks")
    return 0


if __name__ == "__main__":
    sys.exit(main())
