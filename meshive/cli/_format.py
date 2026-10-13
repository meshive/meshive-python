"""CLI output formatting helpers — color / currency / relative time / table alignment.

ANSI escapes only, no external dependencies. Color turns off automatically when output is not a tty or
NO_COLOR (convention) / MESHIVE_NO_COLOR is set → nothing breaks in pipes, redirects or
--json. Alignment widths are computed from the plain text *before* coloring, so
ANSI codes don't throw off column alignment.
"""
from __future__ import annotations

import math
import os
import re
import sys
from datetime import date, datetime, timezone
from typing import TextIO

from ..formatting import format_hourly, format_usd

_RESET = "\033[0m"
_COLORS = {
    "green": "\033[32m",
    "gray": "\033[90m",
    "yellow": "\033[33m",
    "red": "\033[31m",
    "cyan": "\033[36m",
    "dim": "\033[2m",
}

# Status → color. Unknown statuses fall back to cyan.
_STATUS_COLOR = {
    "running": "green",
    "active": "green",
    "ready": "green",
    "online": "green",          # machine state.name
    "stopped": "gray",
    "terminated": "gray",
    "revoked": "gray",
    "offline": "gray",          # machine state.name
    "paused": "yellow",
    "waiting": "yellow",
    "maintenance": "yellow",    # machine state.name
    "provisioning": "cyan",
    "pending": "cyan",
    "start_up": "cyan",         # machine setup stage
    "re_verifying": "cyan",     # machine setup stage
    "succeeded": "green",       # task terminal (success)
    "queued": "cyan",           # task waiting/preparing stage
    "scheduling": "cyan",
    "pulling": "cyan",
    "fetching": "cyan",
    "scaling": "yellow",        # serving group status
    "draining": "yellow",
    "expired": "gray",
    "uploading": "cyan",        # asset version
    "frozen": "yellow",         # asset (admin freeze)
    "source_missing": "red",    # asset (user S3 source lost)
    "deleted": "gray",
    "purged": "gray",
    "merged": "gray",
    "timed_out": "red",         # task terminal (treated as failure)
    "error": "red",
    "failed": "red",
    "node_not_ready": "red",    # machine state.name
    "agent_not_ready": "red",   # machine state.name
    "delete_machine": "red",    # machine state.name
}

_STATUS_ICON = "●"  # ●

# C0/C1 control characters (including tab and newline) + Unicode bidi/format control characters.
# Escape sequences mixed into server-provided strings (alias/name, ...) could manipulate the terminal,
# and an RTL override (U+202E) could reorder the output for spoofing (Trojan Source style),
# so they are stripped before printing.
_CONTROL_CHARS = re.compile(
    "[\x00-\x1f\x7f-\x9f"
    "\u200e\u200f"        # LRM/RLM
    "\u202a-\u202e"       # LRE/RLE/PDF/LRO/RLO
    "\u2066-\u2069"       # LRI/RLI/FSI/PDI
    "]"
)


def clean(text: str) -> str:
    """Strip control characters from server-provided strings (defense against terminal escape injection)."""
    return _CONTROL_CHARS.sub("", text)


def color_enabled(stream: TextIO | None = None) -> bool:
    stream = stream if stream is not None else sys.stdout
    if os.getenv("NO_COLOR") is not None or os.getenv("MESHIVE_NO_COLOR") is not None:
        return False
    return bool(getattr(stream, "isatty", lambda: False)())


def paint(text: str, color: str | None, enabled: bool) -> str:
    if not enabled or not color or color not in _COLORS:
        return text
    return f"{_COLORS[color]}{text}{_RESET}"


def status_color(status: str) -> str:
    return _STATUS_COLOR.get(status.lower(), "cyan")


def status_cell(status: str) -> str:
    """Icon + status text (the caller paints the color). Plain text, for width calculation."""
    return f"{_STATUS_ICON} {status}" if status else f"{_STATUS_ICON} -"


def money(value: str | float | None) -> str:
    """General amount → '$2.10' (2 decimals). Balances, daily/monthly totals, accumulated cost, refunds, etc. Same as the web console."""
    return format_usd(value)


def money_hourly(value: str | float | None) -> str:
    """Hourly rate → '$0.068' (**always 3 decimals**). Same as the web console.

    If screens round differently ($0.07 vs $0.065) users can't trust what they're billed — that's why the console fixes
    $/hr to 3 decimals for pods, storage, serving, tasks, GPUs and estimate breakdowns, and the CLI prints the same values.
    (Host earnings earn/hr and current/hr use 2 decimals in the console, so they use `money` — mirroring the console.)
    """
    return format_hourly(value)


def yes_no(value: bool) -> str:
    return "yes" if value else "no"


def percent(rate: float | None) -> str:
    """Ratio (0.0–1.0) → '99.9%'. None/non-finite → '-'."""
    if rate is None or not math.isfinite(rate):
        return "-"
    return f"{rate * 100:.1f}%"


def usage(rate: float | None) -> str:
    """Utilization (0.0–1.0) → '35.0%'. Not measurable (None) → 'n/a', to tell it apart from 0%."""
    if rate is None or not math.isfinite(rate):
        return "n/a"
    return f"{rate * 100:.1f}%"


def _from_mib(mib: float | None, unit: str, small_unit: str) -> str:
    """MiB value → 'N {unit}' after /1024. Under 1024 MiB stays 'N {small_unit}'. None/non-finite → '-'."""
    if mib is None:
        return "-"
    try:
        raw = float(mib)
    except (TypeError, ValueError):
        return "-"
    if not math.isfinite(raw):
        return "-"
    value = raw / 1024
    if 0 < raw < 1024:
        return f"{raw:.0f} {small_unit}"
    if value >= 10 or value == int(value):
        return f"{value:,.0f} {unit}"
    return f"{value:.1f} {unit}"


def gib(mib: float | None) -> str:
    """MiB value → 'N GiB' (/1024 like the web console, 1024-based label too). Under 1 GiB as 'N MiB' —
    so a system pod's few dozen MiB don't collapse to '0.0 GiB'. None/non-finite → '-'."""
    return _from_mib(mib, "GiB", "MiB")


def vram(mib: float | None) -> str:
    """GPU VRAM (MiB) → 'N GB'. The number is /1024 like gib(), but the label keeps the industry 'GB' —
    the console didn't switch VRAM to a 1024-based label either (so a '24GB' card doesn't show as '24 GiB')."""
    return _from_mib(mib, "GB", "MB")


def mbps(bytes_per_second: float | None) -> str:
    """Bytes/sec → 'N Mbps'. Mbps is bits/sec ÷ 10^6 (decimal) — dividing by 1024² makes 1 Gbps read as 954 Mbps,
    about 4.6% low (same basis as the web console). None/non-finite → '-'."""
    if bytes_per_second is None:
        return "-"
    try:
        value = float(bytes_per_second) * 8 / 1_000_000
    except (TypeError, ValueError):
        return "-"
    return f"{value:.1f} Mbps" if math.isfinite(value) else "-"


def bytes_human(value: float | int | None) -> str:
    """Bytes → '1.5 KiB' / '12.3 MiB' / '2.00 GiB' (same rules as the web console, 1024-based)."""
    if value is None:
        return "-"
    try:
        amount = float(value)
    except (TypeError, ValueError):
        return "-"
    if not math.isfinite(amount) or amount < 0:
        return "-"
    if amount < 1024:
        return f"{int(amount)} B"
    for unit, size, digits in (("TiB", 1024 ** 4, 2), ("GiB", 1024 ** 3, 2), ("MiB", 1024 ** 2, 1), ("KiB", 1024, 1)):
        if amount >= size:
            return f"{amount / size:.{digits}f} {unit}"
    return f"{int(amount)} B"  # pragma: no cover - unreachable


def temperature(celsius: float | None) -> str:
    if celsius is None:
        return "-"
    try:
        value = float(celsius)
    except (TypeError, ValueError):
        return "-"
    return f"{value:.0f}°C" if math.isfinite(value) else "-"


def date_str(value: date | None) -> str:
    """date → 'YYYY-MM-DD'. None → '-'."""
    return value.isoformat() if value else "-"


def relative_time(dt: datetime | None, *, now: datetime | None = None) -> str:
    """datetime → '5 days ago' / 'in 2 hours' / 'just now'. None → '-'."""
    if dt is None:
        return "-"
    now = now or datetime.now(timezone.utc)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    seconds = (now - dt).total_seconds()
    future = seconds < 0
    seconds = abs(seconds)
    for unit, size in (
        ("year", 31_536_000),
        ("month", 2_592_000),
        ("week", 604_800),
        ("day", 86_400),
        ("hour", 3_600),
        ("minute", 60),
    ):
        if seconds >= size:
            n = int(seconds // size)
            label = f"{n} {unit}{'s' if n != 1 else ''}"
            return f"in {label}" if future else f"{label} ago"
    return "just now"


def render_table(
    headers: list[str],
    rows: list[list[str]],
    *,
    aligns: list[str] | None = None,
    colors: list[list[str | None]] | None = None,
    enabled: bool = False,
    out: TextIO | None = None,
) -> None:
    """Space-aligned table. aligns: 'l'/'r' per column. colors: color per column (None = no color)."""
    out = out if out is not None else sys.stdout
    aligns = aligns or ["l"] * len(headers)
    # Any cell may hold a server-provided value, so strip control characters (widths are computed after cleaning).
    rows = [[clean(cell) for cell in row] for row in rows]
    widths = [len(h) for h in headers]
    for row in rows:
        for i, cell in enumerate(row):
            widths[i] = max(widths[i], len(cell))

    def fmt(value: str, width: int, align: str) -> str:
        return value.rjust(width) if align == "r" else value.ljust(width)

    header_line = "  ".join(fmt(h, w, a) for h, w, a in zip(headers, widths, aligns))
    print(paint(header_line, "dim", enabled), file=out)

    for r_idx, row in enumerate(rows):
        row_colors = (colors[r_idx] if colors else None) or [None] * len(headers)
        cells = []
        for value, width, align, color in zip(row, widths, aligns, row_colors):
            padded = fmt(value, width, align)
            cells.append(paint(padded, color, enabled))
        print("  ".join(cells), file=out)
