"""CLI 출력 포맷 헬퍼 — 색상/통화/상대시간/테이블 정렬.

외부 의존성 없이 ANSI escape 만 사용한다. 색상은 출력이 tty 가 아니거나
NO_COLOR(관례) / MESHIVE_NO_COLOR 가 설정되면 자동으로 꺼진다 → 파이프/리다이렉트/
--json 에서 깨지지 않는다. 정렬 폭은 색을 입히기 *전* 평문 길이로 계산하므로
ANSI 코드가 칸 맞춤을 망가뜨리지 않는다.
"""
from __future__ import annotations

import math
import os
import sys
from datetime import datetime, timezone
from typing import TextIO

_RESET = "\033[0m"
_COLORS = {
    "green": "\033[32m",
    "gray": "\033[90m",
    "yellow": "\033[33m",
    "red": "\033[31m",
    "cyan": "\033[36m",
    "dim": "\033[2m",
}

# 상태 → 색. 미지의 상태는 cyan 으로 폴백.
_STATUS_COLOR = {
    "running": "green",
    "active": "green",
    "ready": "green",
    "stopped": "gray",
    "terminated": "gray",
    "revoked": "gray",
    "paused": "yellow",
    "waiting": "yellow",
    "provisioning": "cyan",
    "pending": "cyan",
    "error": "red",
    "failed": "red",
}

_STATUS_ICON = "●"  # ●


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
    """아이콘 + 상태 텍스트 (색은 호출부에서 paint). 폭 계산용 평문."""
    return f"{_STATUS_ICON} {status}" if status else f"{_STATUS_ICON} -"


def money(value: str | None) -> str:
    """price 문자열 → '$2.10' (USD, 소수점 2자리, 천단위 콤마). 빈/잘못된 값은 '-'.

    서버 price_per_hour 는 Numeric(20,8) 이라 '2.10000000' 처럼 와서 그대로 쓰면
    불필요한 자릿수가 보인다. 웹 formatUsd 와 동일 규칙으로 2자리 반올림한다.
    """
    if value in (None, ""):
        return "-"
    try:
        amount = float(value)
    except (TypeError, ValueError):
        return "-"
    if not math.isfinite(amount):
        return "-"
    return f"${amount:,.2f}"


def yes_no(value: bool) -> str:
    return "yes" if value else "no"


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
    """공백 정렬 테이블. aligns: 칸별 'l'/'r'. colors: 칸별 색(None=무색)."""
    out = out if out is not None else sys.stdout
    aligns = aligns or ["l"] * len(headers)
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
