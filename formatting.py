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
# The summary line with global transfer rates and totals.
DOWN_RE = re.compile(r"Down:\s*([\d.]+\s*\S+/s)", re.IGNORECASE)
UP_RE = re.compile(r"Up:\s*([\d.]+\s*\S+/s)", re.IGNORECASE)
SHARED_RE = re.compile(r"Shared:\s*(\S+)", re.IGNORECASE)
DLED_RE = re.compile(r"Downloaded:\s*(\S+)", re.IGNORECASE)
ULED_RE = re.compile(r"Uploaded:\s*(\S+)", re.IGNORECASE)

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
    # tail = [%, Done, Size, lSeen, Old, Active, Rate, Prio]
    done, size, active, rate = tail[1], tail[2], tail[5], tail[6]
    name_tokens = tokens[LEAD_COLS:-TAIL_COLS] if len(tokens) > LEAD_COLS + TAIL_COLS \
        else tokens[:-TAIL_COLS]
    name = " ".join(name_tokens) or "(unknown)"
    return {
        "num": num,
        "state": state,
        "pct": pct,
        "done": done,
        "size": size,
        "active": active,
        "rate": rate,
        "name": name,
    }


# --- Search results ---------------------------------------------------------
#
# `vr` prints the results of the last search as a fixed-width table, one result
# per line:
#
#   [ Num ]      Size     Avail Status        Names          Tags   MD4
#   [     2] 12557510216     1      Doraemon y el viaje ...mkv      urn:ed2k:7308...
#
# The first bracketed number is the result index that `d <num>` downloads. Then
# come the size in raw bytes and the availability (number of sources). The file
# name (middle-truncated by mldonkey with "....") sits in the middle, and the
# ed2k hash (urn:ed2k:...) closes the row. We key off that layout and pull the
# name out from between the availability column and the hash.

# A result row: "[     2] 12557510216     1   Some Name ...mkv   urn:ed2k:HASH"
RESULT_RE = re.compile(r"^\s*\[\s*(\d+)\s*\]\s+(\d+)\s+(\d+)\s+(.*)$")
# The ed2k hash that closes each row.
URN_RE = re.compile(r"urn:ed2k:[0-9A-Fa-f]+", re.IGNORECASE)


def _human_size(token: str) -> str:
    """Pretty-print a size token; expand a bare byte count, pass units through."""
    raw = token.strip()
    if re.fullmatch(r"\d+", raw):
        size = float(raw)
        for unit in ("B", "KB", "MB", "GB", "TB"):
            if size < 1024 or unit == "TB":
                return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
            size /= 1024
    return raw


def parse_search_results(raw: str) -> list[dict]:
    """Parse `vr` table output into [{num, name, size, avail, md4}] result dicts."""
    text = strip_ansi(raw)
    results: list[dict] = []
    for line in text.splitlines():
        m = RESULT_RE.match(line)
        if not m:
            continue
        rest = m.group(4)
        um = URN_RE.search(rest)
        md4 = um.group(0) if um else ""
        # Whatever sits between the availability column and the hash is the name
        # (the Status/Tags columns are usually empty). Collapse mldonkey's "...."
        # middle-truncation marker into a single ellipsis.
        name = re.sub(r"\.{3,}", "…", URN_RE.sub("", rest).strip())
        results.append({
            "num": int(m.group(1)),
            "name": name or "(no name)",
            "size": _human_size(m.group(2)),
            "avail": m.group(3),
            "md4": md4,
        })
    return results


def format_result_line(idx: int, r: dict) -> str:
    """Render one search result as a couple of HTML lines for the message body."""
    meta = []
    if r.get("size"):
        meta.append(f"📦 {html.escape(r['size'])}")
    if r.get("avail"):
        meta.append(f"👥 {html.escape(r['avail'])}")
    head = f"<b>{idx}.</b> <code>{html.escape(r['name'][:80] or '(no name)')}</code>"
    if meta:
        head += f"\n     <i>{'  ·  '.join(meta)}</i>"
    return head


def _clean_token(value: str) -> str:
    """Drop mldonkey's placeholder ('-', '0', '0.0') values."""
    return "" if value.strip() in ("", "-", "0", "0.0") else value.strip()


def render_downloads(raw: str, limit: int = 25) -> str:
    text = strip_ansi(raw)
    stats = {}
    rows = []
    raw_row_lines = 0  # bracketed rows we saw, parsed or not
    for line in text.splitlines():
        if "down" not in stats:
            m = DOWN_RE.search(line)
            if m:
                stats["down"] = m.group(1)
                for key, rx in (("up", UP_RE), ("shared", SHARED_RE),
                                ("dled", DLED_RE), ("uled", ULED_RE)):
                    hit = rx.search(line)
                    if hit:
                        stats[key] = hit.group(1)
        rm = ROW_RE.match(line)
        if rm:
            raw_row_lines += 1
            parsed = _parse_row(rm.group(1), int(rm.group(2)), rm.group(3))
            if parsed:
                rows.append(parsed)

    # Global header lines (rates + lifetime totals), shown in both states.
    header = []
    if stats.get("down"):
        header.append(
            f"⬇ {html.escape(stats['down'])} · ⬆ {html.escape(stats.get('up', '?'))}"
        )
    if stats.get("dled") or stats.get("uled"):
        bits = []
        if stats.get("dled"):
            bits.append(f"Σ↓ {html.escape(stats['dled'])}")
        if stats.get("uled"):
            bits.append(f"Σ↑ {html.escape(stats['uled'])}")
        if stats.get("shared"):
            bits.append(f"🔁 {html.escape(stats['shared'])}")
        header.append(" · ".join(bits))

    if not rows:
        # If mldonkey printed bracketed rows we couldn't parse, show the raw
        # table rather than wrongly claiming there's nothing downloading.
        if raw_row_lines:
            body = html.escape(text.strip())[:3500]
            return "📥 <b>Downloads</b>\n<pre>" + body + "</pre>"
        lines = ["📥 <b>No active downloads.</b>"]
        lines.extend(header)
        return "\n".join(lines)

    lines = [f"📥 <b>Downloads ({len(rows)})</b>"]
    lines.extend(header)
    lines.append("")
    for r in rows[:limit]:
        lines.append(
            f"{_icon(r['state'], r['pct'])} <b>#{r['num']}</b>  "
            f"{progress_bar(r['pct'])} {r['pct']:.0f}%"
        )
        lines.append(f"   <code>{html.escape(r['name'][:70])}</code>")
        meta = [f"{html.escape(r['done'])} / {html.escape(r['size'])}"]
        rate = _clean_token(r.get("rate", ""))
        if rate:
            meta.append(f"⚡ {html.escape(rate)}")
        active = _clean_token(r.get("active", ""))
        if active:
            meta.append(f"👥 {html.escape(active)}")
        lines.append(f"   <i>{'  ·  '.join(meta)}</i>")
    if len(rows) > limit:
        lines.append(f"\n…and {len(rows) - limit} more.")
    return "\n".join(lines)


# --- Cancel -----------------------------------------------------------------

# mldonkey's cancel summary carries the file name on a "Probable name:" line.
PROBABLE_RE = re.compile(r"^\s*Probable name:\s*(.+?)\s*$", re.IGNORECASE | re.MULTILINE)


def parse_cancel_name(raw: str) -> str | None:
    """Pull the file name out of mldonkey's cancel confirmation, if present."""
    m = PROBABLE_RE.search(strip_ansi(raw))
    return m.group(1).strip() if m else None
