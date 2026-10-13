"""Amounts → display strings. Produces **the same values as the Meshive web console**.

The SDK returns the numbers the server sent as-is (`pod.price_per_hour == "0.06770833"`) — use those for math.
When showing an amount to a person, these helpers give the same result as the console, CLI and MCP.

    >>> from meshive import format_hourly, format_usd
    >>> format_hourly(pod.price_per_hour)   # hourly rate
    '$0.068'
    >>> format_usd(credit.balance)          # any other amount
    '$12.50'

The rules match the web console: hourly rates always show 3 decimals, everything else 2. Only hourly rates
get 3 because billed amounts are hard to trust when screens round differently ($0.07 vs $0.065), and so that
sub-cent unit prices such as storage don't collapse to "$0.00".

Rounding uses Decimal + ROUND_HALF_UP, not float formatting — the console's Intl.NumberFormat defaults to
halfExpand, while Python float formatting is half-even plus binary error, so they differ at boundaries (0.015).
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
    # Refund/clawback ledger rows are negative — "-$12.50", not "$-12.50".
    sign = "-" if quantized < 0 else ""
    return f"{sign}${abs(quantized):,.{digits}f}"


def format_hourly(value: Any) -> str:
    """Hourly rate → ``'$0.068'`` (always 3 decimals). ``'-'`` if the value is missing or not a number."""
    return _usd(value, 3)


def format_usd(value: Any) -> str:
    """Non-hourly amounts such as balances and totals → ``'$12.50'`` (2 decimals). ``'-'`` if the value is missing or not a number."""
    return _usd(value, 2)
