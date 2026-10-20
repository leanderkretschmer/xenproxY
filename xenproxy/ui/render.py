"""Pure render primitives: every function returns a list of strings.

Naming follows the termcn component vocabulary (Box, Panel, Divider,
Columns, Badge, Progress Bar, Key Value, Table, Spinner).
"""
from __future__ import annotations

from ..theme import RESET, Theme
from .term import pad, truncate, vlen


def panel(th: Theme, body: list[str], width: int, *, title: str = "",
          subtitle: str = "", focused: bool = False,
          token: str | None = None, height: int | None = None) -> list[str]:
    """A titled bordered panel. `focused` lifts the border to `primary`."""
    tl, h, tr, vl, bl, br, vr, hb = th.border_chars
    btok = token or ("primary" if focused else "border")
    inner = max(1, width - 2)

    if height is not None:
        body = body[: max(0, height - 2)]
        body = body + [""] * max(0, (height - 2) - len(body))

    head = ""
    if title:
        t = f" {title} "
        head = th.paint(t, "primary" if focused else "foreground", bold=focused)
    if subtitle:
        st = f" {subtitle} "
        room = inner - vlen(head) - 2
        if room > vlen(st):
            head = head + th.paint(h * (room - vlen(st)), btok) + th.paint(st, "muted")

    used = vlen(head)
    top = th.paint(tl + h, btok) + head + th.paint(h * max(0, inner - used - 1) + tr, btok)
    out = [top]
    for row in body:
        out.append(th.paint(vl, btok) + pad(truncate(row, inner), inner) + th.paint(vr, btok))
    out.append(th.paint(bl + hb * inner + br, btok))
    return out


def divider(th: Theme, width: int, label: str = "", token: str = "border") -> str:
    if not label:
        return th.paint("─" * width if th.uni else "-" * width, token)
    ch = "─" if th.uni else "-"
    lab = th.paint(f" {label} ", "muted")
    left = 2
    right = max(0, width - left - vlen(lab))
    return th.paint(ch * left, token) + lab + th.paint(ch * right, token)


def columns(blocks: list[list[str]], widths: list[int], gap: int = 1) -> list[str]:
    """Place blocks side by side, padding each to its width."""
    rows = max((len(b) for b in blocks), default=0)
    out = []
    for i in range(rows):
        parts = []
        for b, w in zip(blocks, widths):
            parts.append(pad(b[i] if i < len(b) else "", w))
        out.append((" " * gap).join(parts))
    return out


def stack(blocks: list[list[str]], gap: int = 0) -> list[str]:
    out: list[str] = []
    for i, b in enumerate(blocks):
        if i and gap:
            out.extend([""] * gap)
        out.extend(b)
    return out


def badge(th: Theme, text: str, token: str = "primary", *, solid: bool = False) -> str:
    if solid:
        return f"{th.bg(token)}{th.fg('surface')} {text} {RESET}"
    return th.paint(f"[{text}]", token, bold=True)


def tag(th: Theme, text: str, token: str = "muted") -> str:
    return th.paint(f"{th.g['bullet']} {text}", token)


def progress_bar(th: Theme, frac: float, width: int, *, token: str = "primary",
                 show_pct: bool = True) -> str:
    frac = max(0.0, min(1.0, frac))
    label = f" {frac * 100:5.1f}%" if show_pct else ""
    bw = max(1, width - vlen(label))
    full = int(bw * frac)
    rest = bw - full
    bar = th.paint(th.g["bar_full"] * full, token)
    bar += th.paint(th.g["bar_empty"] * rest, "border")
    return bar + th.paint(label, "muted")


def multi_progress(th: Theme, items: list[tuple[str, float, str]], width: int,
                   label_width: int = 18) -> list[str]:
    out = []
    for name, frac, token in items:
        lbl = pad(truncate(name, label_width), label_width)
        out.append(th.paint(lbl, "foreground") + " " +
                   progress_bar(th, frac, max(10, width - label_width - 1), token=token))
    return out


def key_value(th: Theme, pairs: list[tuple[str, str]], width: int,
              key_width: int | None = None) -> list[str]:
    if not pairs:
        return []
    kw = key_width or min(24, max(vlen(k) for k, _ in pairs) + 2)
    out = []
    for k, v in pairs:
        out.append(th.paint(pad(truncate(k, kw), kw), "muted") +
                   truncate(str(v), max(1, width - kw)))
    return out


def table(th: Theme, headers: list[str], rows: list[list[str]], width: int, *,
          selected: int = -1, aligns: list[str] | None = None,
          max_rows: int | None = None, offset: int = 0) -> list[str]:
    n = len(headers)
    aligns = aligns or ["left"] * n
    natural = [vlen(h) for h in headers]
    for r in rows:
        for i in range(n):
            natural[i] = max(natural[i], vlen(str(r[i])) if i < len(r) else 0)
    gap = 2
    avail = width - gap * (n - 1)
    total = sum(natural)
    if total > avail:
        widest = natural.index(max(natural))
        natural[widest] -= total - avail
        natural[widest] = max(6, natural[widest])
    else:
        natural[0] += avail - total

    head = (" " * gap).join(
        th.paint(pad(truncate(h, w), w, a), "muted", bold=True)
        for h, w, a in zip(headers, natural, aligns))
    out = [head, divider(th, width)]

    view = rows[offset: offset + max_rows] if max_rows else rows
    for idx, r in enumerate(view, start=offset):
        cells = [pad(truncate(str(r[i]) if i < len(r) else "", w), w, a)
                 for i, (w, a) in enumerate(zip(natural, aligns))]
        line = (" " * gap).join(cells)
        if idx == selected:
            out.append(f"{th.bg('surface')}{th.fg('primary')}{pad(line, width)}{RESET}")
        else:
            out.append(th.paint(line, "foreground"))
    return out


def alert(th: Theme, text: str, width: int, kind: str = "info",
          title: str = "") -> list[str]:
    icons = {"info": th.g["bullet"], "success": th.g["check"],
             "warning": th.g["warn"], "danger": th.g["cross"]}
    token = {"info": "info", "success": "success",
             "warning": "warning", "danger": "danger"}.get(kind, "info")
    head = f"{icons.get(kind, '*')} {title or kind.upper()}"
    body = [th.paint(head, token, bold=True)]
    body += wrap(text, width - 4)
    return panel(th, body, width, token=token)


def status_message(th: Theme, text: str, kind: str = "info") -> str:
    icons = {"info": th.g["bullet"], "success": th.g["check"],
             "warning": th.g["warn"], "danger": th.g["cross"],
             "pending": th.g["ring"]}
    token = {"info": "info", "success": "success", "warning": "warning",
             "danger": "danger", "pending": "muted"}.get(kind, "info")
    return th.paint(f"{icons.get(kind, '*')} {text}", token)


def wrap(text: str, width: int) -> list[str]:
    out: list[str] = []
    for para in str(text).split("\n"):
        if not para:
            out.append("")
            continue
        line = ""
        for word in para.split(" "):
            if line and vlen(line) + 1 + vlen(word) > width:
                out.append(line)
                line = word
            else:
                line = f"{line} {word}" if line else word
        if line:
            out.append(line)
    return out


def spinner_frame(th: Theme, tick: int, token: str = "primary") -> str:
    frames = th.g["spinner"]
    return th.paint(frames[tick % len(frames)], token)


def help_bar(th: Theme, items: list[tuple[str, str]], width: int) -> str:
    parts = []
    for key, desc in items:
        parts.append(th.paint(key, "accent", bold=True) + " " + th.paint(desc, "muted"))
    sep = th.paint(f"  {th.g['sep']}  ", "border")
    return truncate(sep.join(parts), width)


def breadcrumb(th: Theme, parts: list[str], width: int) -> str:
    sep = th.paint(f" {th.g['tri_right']} ", "border")
    chunks = [th.paint(p, "muted") for p in parts[:-1]]
    if parts:
        chunks.append(th.paint(parts[-1], "foreground", bold=True))
    return truncate(sep.join(chunks), width)
