"""금액 → 표시 문자열. **Meshive 웹 콘솔과 같은 값**을 낸다.

SDK 는 서버가 준 숫자를 그대로 돌려준다(`pod.price_per_hour == "0.06770833"`) — 계산에 쓰라는 뜻이다.
사람에게 보여줄 때는 이 헬퍼를 쓰면 콘솔·CLI·MCP 와 같은 금액이 나온다.

    >>> from meshive import format_hourly, format_usd
    >>> format_hourly(pod.price_per_hour)   # 시간당 요금
    '$0.068'
    >>> format_usd(credit.balance)          # 그 외 금액
    '$12.50'

규칙의 소스는 콘솔 `Formatter.tsx` 다: 시간당 요금은 3자리 고정(`formatHourlyUsd`), 그 외는 2자리
(`formatUsd`). 시간당만 3자리인 이유는 화면마다 반올림이 다르면($0.07 vs $0.065) 청구 금액을 믿기
어렵기 때문이고, 스토리지처럼 1센트 미만인 단가가 "$0.00" 으로 뭉개지지 않게 하려는 것이기도 하다.

반올림은 float 포맷이 아니라 Decimal + ROUND_HALF_UP 이다 — 콘솔 Intl.NumberFormat 의 기본이
halfExpand 인데 파이썬 float 포맷은 half-even + 이진 오차라 경계값에서 갈린다(0.015).
"""
from __future__ import annotations

from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Any

__all__ = ["format_hourly", "format_usd"]

MISSING = "-"


def _usd(value: Any, digits: int) -> str:
    if value is None or value == "":
        return MISSING
    try:
        amount = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return MISSING
    if not amount.is_finite():
        return MISSING
    quantized = amount.quantize(Decimal(1).scaleb(-digits), rounding=ROUND_HALF_UP)
    # 환불/회수 원장행은 음수 — "$-12.50" 이 아니라 "-$12.50".
    sign = "-" if quantized < 0 else ""
    return f"{sign}${abs(quantized):,.{digits}f}"


def format_hourly(value: Any) -> str:
    """시간당 요금 → ``'$0.068'`` (소수점 3자리 고정). 값이 없거나 숫자가 아니면 ``'-'``."""
    return _usd(value, 3)


def format_usd(value: Any) -> str:
    """잔액·합계 등 시간당이 아닌 금액 → ``'$12.50'`` (2자리). 값이 없거나 숫자가 아니면 ``'-'``."""
    return _usd(value, 2)
