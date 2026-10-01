"""Reusable query operators. No SQL, no products, no HTTP.

A registered query may apply these after its SQL template has filtered rows.
Adding an operator here does not add a business question; the query register does that.
"""

from datetime import date
from decimal import Decimal, InvalidOperation

GRAINS = ("year", "quarter", "month")


def top_n(rows: list[dict], n: int, order_by: str, descending: bool = True) -> list[dict]:
    """First n rows by `order_by`. Missing values sort last. Numeric values sort as numbers."""
    if n < 1:
        raise ValueError("n must be >= 1")
    keyed = [( _rank(row.get(order_by), descending), row) for row in rows]
    keyed.sort(key=lambda item: item[0])
    return [row for _, row in keyed[:n]]


def time_aggregate(rows: list[dict], time_field: str, grain: str, sum_field: str | None = None) -> tuple[list[dict], int]:
    """Bucket rows by year, quarter, or month. Returns (buckets, rows skipped for a missing date).

    `sum` is a decimal string so money is not passed through binary floats.
    """
    if grain not in GRAINS:
        raise ValueError(f"grain must be one of {', '.join(GRAINS)}")
    buckets: dict[str, dict] = {}
    skipped = 0
    for row in rows:
        period = period_of(row.get(time_field), grain)
        if period is None:
            skipped += 1
            continue
        bucket = buckets.get(period)
        if bucket is None:
            bucket = {"period": period, "count": 0, "sum": Decimal("0")}
            buckets[period] = bucket
        bucket["count"] += 1
        if sum_field is not None and row.get(sum_field) is not None:
            bucket["sum"] += Decimal(str(row[sum_field]))
    out = []
    for period in sorted(buckets):
        bucket = buckets[period]
        item = {"period": period, "count": bucket["count"]}
        if sum_field is not None:
            item["sum"] = format(bucket["sum"], "f")
        out.append(item)
    return out, skipped


def period_of(value, grain: str) -> str | None:
    if value is None or value == "":
        return None
    try:
        day = date.fromisoformat(str(value)[:10])
    except ValueError:
        return None
    if grain == "year":
        return f"{day.year:04d}"
    if grain == "quarter":
        return f"{day.year:04d}-Q{(day.month - 1) // 3 + 1}"
    return f"{day.year:04d}-{day.month:02d}"


def _rank(value, descending: bool):
    """Sort key: present values first, then numeric or text, honouring direction."""
    if value is None or value == "":
        return (1, 0, "")
    number = _decimal(value)
    if number is not None:
        return (0, -number if descending else number, "")
    text = str(value)
    return (0, 0, _invert(text) if descending else text)


def _decimal(value) -> Decimal | None:
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None


def _invert(text: str) -> str:
    return "".join(chr(0x10FFFF - ord(ch)) for ch in text)
