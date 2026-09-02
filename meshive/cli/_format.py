"""CLI 출력 포맷 헬퍼 — 색상/통화/상대시간/테이블 정렬.

외부 의존성 없이 ANSI escape 만 사용한다. 색상은 출력이 tty 가 아니거나
NO_COLOR(관례) / MESHIVE_NO_COLOR 가 설정되면 자동으로 꺼진다 → 파이프/리다이렉트/
--json 에서 깨지지 않는다. 정렬 폭은 색을 입히기 *전* 평문 길이로 계산하므로
ANSI 코드가 칸 맞춤을 망가뜨리지 않는다.
"""
from __future__ import annotations

import math
import os
import re
import sys
from datetime import date, datetime, timezone
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
    "succeeded": "green",       # task terminal (성공)
    "queued": "cyan",           # task 대기/준비 단계
    "scheduling": "cyan",
    "pulling": "cyan",
    "fetching": "cyan",
    "scaling": "yellow",        # serving group status
    "draining": "yellow",
    "expired": "gray",
    "uploading": "cyan",        # asset version
    "frozen": "yellow",         # asset (admin freeze)
    "source_missing": "red",    # asset (유저 S3 원본 유실)
    "deleted": "gray",
    "purged": "gray",
    "merged": "gray",
    "timed_out": "red",         # task terminal (실패 취급)
    "error": "red",
    "failed": "red",
    "node_not_ready": "red",    # machine state.name
    "agent_not_ready": "red",   # machine state.name
    "delete_machine": "red",    # machine state.name
}

_STATUS_ICON = "●"  # ●

# C0/C1 제어문자 (탭·개행 포함) + 유니코드 bidi/서식 제어문자.
# 서버가 주는 문자열(alias/name 등)에 이스케이프 시퀀스가 섞이면 터미널 조작이,
# RTL override(U+202E) 등이 섞이면 출력 순서 뒤집기 스푸핑(Trojan Source 류)이
# 가능하므로 출력 전에 제거한다.
_CONTROL_CHARS = re.compile(
    "[\x00-\x1f\x7f-\x9f"
    "\u200e\u200f"        # LRM/RLM
    "\u202a-\u202e"       # LRE/RLE/PDF/LRO/RLO
    "\u2066-\u2069"       # LRI/RLI/FSI/PDI
    "]"
)


def clean(text: str) -> str:
    """서버 유래 문자열에서 제어문자 제거 (터미널 이스케이프 인젝션 방어)."""
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
    """아이콘 + 상태 텍스트 (색은 호출부에서 paint). 폭 계산용 평문."""
    return f"{_STATUS_ICON} {status}" if status else f"{_STATUS_ICON} -"


def money(value: str | float | None) -> str:
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
    # 환불/회수 원장행은 음수 — '$-12.50' 이 아니라 '-$12.50'.
    sign = "-" if amount < 0 else ""
    return f"{sign}${abs(amount):,.2f}"


def yes_no(value: bool) -> str:
    return "yes" if value else "no"


def percent(rate: float | None) -> str:
    """비율(0.0~1.0) → '99.9%'. None/비유한값은 '-'."""
    if rate is None or not math.isfinite(rate):
        return "-"
    return f"{rate * 100:.1f}%"


def usage(rate: float | None) -> str:
    """사용률(0.0~1.0) → '35.0%'. 측정 불가(None)는 'n/a' — 0% 와 구분한다."""
    if rate is None or not math.isfinite(rate):
        return "n/a"
    return f"{rate * 100:.1f}%"


def gib(mib: float | None) -> str:
    """MiB 값 → 'N GB' (웹 콘솔과 동일하게 /1024). None/비유한값은 '-'."""
    if mib is None:
        return "-"
    try:
        value = float(mib) / 1024
    except (TypeError, ValueError):
        return "-"
    if not math.isfinite(value):
        return "-"
    if value >= 10 or value == int(value):
        return f"{value:,.0f} GB"
    return f"{value:.1f} GB"


def mbps(bytes_per_second: float | None) -> str:
    """바이트/초 → 'N Mbps' (웹 콘솔과 동일: x8 / 1024 / 1024). None/비유한값은 '-'."""
    if bytes_per_second is None:
        return "-"
    try:
        value = float(bytes_per_second) * 8 / 1024 / 1024
    except (TypeError, ValueError):
        return "-"
    return f"{value:.1f} Mbps" if math.isfinite(value) else "-"


def bytes_human(value: float | int | None) -> str:
    """바이트 → '1.5 KB' / '12.3 MB' / '2.00 GB' (웹 콘솔 formatBytes 와 동일 규칙, 1024 기준)."""
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
    for unit, size, digits in (("TB", 1024 ** 4, 2), ("GB", 1024 ** 3, 2), ("MB", 1024 ** 2, 1), ("KB", 1024, 1)):
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
    """공백 정렬 테이블. aligns: 칸별 'l'/'r'. colors: 칸별 색(None=무색)."""
    out = out if out is not None else sys.stdout
    aligns = aligns or ["l"] * len(headers)
    # 모든 셀은 서버 유래 값일 수 있으므로 제어문자를 걷어낸다 (폭 계산도 정제 후 기준).
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
