"""Turn mldonkey's `vd` console output into something nice for Telegram.

Real-world `vd` output from the console looks like:

    Down: 0.1 KB/s ( 24 + 66 ) | Up: 184.1 KB/s ( ... ) | Shared: ... | Downloaded: 9.69G | Uploaded: 248.10G
      Num   Rele Comm User   Group   File   %   Done   Size lSeen Old Active Rate Prio
    [D1037] -    0    admin  mldonkey Some.File.mkv  15.3  259.8mb  1.7gb  -  1:-  0/1  -  0
    Downloaded 0 files

Each download row starts with "[<state><num>]". The columns after it are, in
order: Rele, Comm, User, Group, File, %, Done, Size, lSeen, Old, Active, Rate,
Prio. The filename may contain spaces, so we peel off the 4 fixed leading
columns and the 8 fixed trailing columns and treat whatever remains as the name.
"""

import html
import re

from mldonkey import strip_ansi

# A download row: "[D1037] ..." — optional state letter, then the index.
ROW_RE = re.compile(r"^\s*\[\s*([A-Za-z]?)\s*(\d+)\s*\]\s+(.*)$")
# The summary line with global transfer rates.
DOWN_RE = re.compile(r"Down:\s*([\d.]+\s*\S+/s)", re.IGNORECASE)
UP_RE = re.compile(r"Up:\s*([\d.]+\s*\S+/s)", re.IGNORECASE)

# Number of fixed columns before and after the (possibly multi-word) filename.
LEAD_COLS = 4   # Rele Comm User Group
TAIL_COLS = 8   # % Done Size lSeen Old Active Rate Prio

BLOCK = "▰"
EMPTY = "▱"

STATE_ICON = {
    "D": "⬇️",   # downloading
    "P": "⏸️",   # paused
    "Q": "⏳",   # queued
}


def progress_bar(pct: float, width: int = 10) -> str:
    pct = max(0.0, min(100.0, pct))
    filled = int(round(pct / 100 * width))
    return BLOCK * filled + EMPTY * (width - filled)


def _icon(state: str, pct: float) -> str:
    if pct >= 100:
        return "✅"
    return STATE_ICON.get(state.upper(), "⬇️")


def _parse_row(state: str, num: int, rest: str):
    tokens = rest.split()
    if len(tokens) < TAIL_COLS + 1:
        return None
    tail = tokens[-TAIL_COLS:]
    try:
        pct = float(tail[0])
    except ValueError:
        return None
    done, size = tail[1], tail[2]
    name_tokens = tokens[LEAD_COLS:-TAIL_COLS] if len(tokens) > LEAD_COLS + TAIL_COLS \
        else tokens[:-TAIL_COLS]
    name = " ".join(name_tokens) or "(unknown)"
    return {
        "num": num,
        "state": state,
        "pct": pct,
        "done": done,
        "size": size,
        "name": name,
    }


def render_downloads(raw: str, limit: int = 25) -> str:
    text = strip_ansi(raw)
    down = up = None
    rows = []
    for line in text.splitlines():
        if down is None:
            m = DOWN_RE.search(line)
            if m:
                down = m.group(1)
                u = UP_RE.search(line)
                up = u.group(1) if u else None
        rm = ROW_RE.match(line)
        if rm:
            parsed = _parse_row(rm.group(1), int(rm.group(2)), rm.group(3))
            if parsed:
                rows.append(parsed)

    if not rows:
        body = html.escape(text.strip()) or "No active downloads."
        header = "📥 <b>Downloads</b>"
        if down:
            header += f"\n⬇ {html.escape(down)}" + (f" · ⬆ {html.escape(up)}" if up else "")
        return f"{header}\n<pre>{body}</pre>"

    lines = [f"📥 <b>Downloads ({len(rows)})</b>"]
    if down:
        lines.append(f"⬇ {html.escape(down)}" + (f" · ⬆ {html.escape(up)}" if up else ""))
    lines.append("")
    for r in rows[:limit]:
        lines.append(
            f"{_icon(r['state'], r['pct'])} <b>#{r['num']}</b>  "
            f"{progress_bar(r['pct'])} {r['pct']:.0f}%"
        )
        lines.append(f"   <code>{html.escape(r['name'][:70])}</code>")
        lines.append(f"   <i>{html.escape(r['done'])} / {html.escape(r['size'])}</i>")
    if len(rows) > limit:
        lines.append(f"\n…and {len(rows) - limit} more.")
    return "\n".join(lines)
