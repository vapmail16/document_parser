"""Deterministic value normaliser (spec §10, §15). No model involved.

normalise(raw) turns a printed value into a typed NormalisedValue; find_values(text) finds every
typed mention in a passage. Grounding uses both: a value is derivable from a quote only if the
quote contains a mention equal to it.
"""

import re
from datetime import date
from decimal import Decimal, InvalidOperation

from .contracts import NormalisedValue, ValueType

_NUM = r"\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?"
_MULT = {"thousand": Decimal(1_000), "million": Decimal(1_000_000), "billion": Decimal(1_000_000_000)}
_CURRENCY = {"us$": "USD", "u.s.$": "USD", "usd": "USD", "€": "EUR", "eur": "EUR", "£": "GBP", "gbp": "GBP", "$": None}
_MONTHS = {m: i for i, m in enumerate(
    ["january", "february", "march", "april", "may", "june", "july", "august", "september",
     "october", "november", "december"], start=1)}
_MON = "|".join(_MONTHS)

_MONEY_RE = re.compile(
    rf"(?P<cur>US\$|U\.S\.\s?\$|USD|EUR|GBP|\$|€|£)\s?(?P<num>{_NUM})(?:\s?(?P<mult>thousand|million|billion))?",
    re.IGNORECASE)
_PCT_RE = re.compile(rf"(?P<num>{_NUM})\s?(?:%|percent\b|per\scent\b)", re.IGNORECASE)
_BPS_RE = re.compile(rf"(?P<num>{_NUM})\s?(?:basis\s+points?|bps)\b", re.IGNORECASE)
_RATIO_RE = re.compile(rf"(?P<a>{_NUM})\s?(?:to|:)\s?(?P<b>{_NUM})(?!\s?(?:%|percent))|(?P<x>{_NUM})\s?x\b",
                       re.IGNORECASE)
_DATE_MDY_RE = re.compile(rf"\b(?P<m>{_MON})\s+(?P<d>\d{{1,2}}),?\s+(?P<y>\d{{4}})\b", re.IGNORECASE)
_DATE_DMY_RE = re.compile(rf"\b(?P<d>\d{{1,2}})\s+(?P<m>{_MON}),?\s+(?P<y>\d{{4}})\b", re.IGNORECASE)
_DATE_NUM_RE = re.compile(r"\b(?P<m>\d{1,2})/(?P<d>\d{1,2})/(?P<y>\d{4})\b")  # US agreements: M/D/Y
_INT_RE = re.compile(rf"^\s*(?P<num>{_NUM})\s*$")


def _dec(s: str) -> Decimal | None:
    try:
        return Decimal(s.replace(",", ""))
    except InvalidOperation:
        return None


def _date(y: str, m: str, d: str) -> date | None:
    try:
        month = int(m) if m.isdigit() else _MONTHS[m.casefold()]
        return date(int(y), month, int(d))
    except (ValueError, KeyError):
        return None


def find_values(text: str) -> list[NormalisedValue]:
    """Every typed mention in `text`, in order of appearance."""
    found: list[tuple[int, NormalisedValue]] = []
    taken: list[tuple[int, int]] = []

    def free(m: re.Match) -> bool:
        return all(m.end() <= a or m.start() >= b for a, b in taken)

    def add(m: re.Match, v: NormalisedValue) -> None:
        taken.append(m.span())
        found.append((m.start(), v))

    for m in _MONEY_RE.finditer(text):
        amount = _dec(m["num"])
        if amount is not None and free(m):
            if m["mult"]:
                amount *= _MULT[m["mult"].casefold()]
            cur = _CURRENCY.get(re.sub(r"\s", "", m["cur"]).casefold())
            add(m, NormalisedValue(type=ValueType.money, amount=amount, currency=cur))
    for regex, vtype, unit in ((_BPS_RE, ValueType.bps, "bps"), (_PCT_RE, ValueType.percentage, "%")):
        for m in regex.finditer(text):
            if free(m) and (amount := _dec(m["num"])) is not None:
                add(m, NormalisedValue(type=vtype, amount=amount, unit=unit))
    for regex in (_DATE_MDY_RE, _DATE_DMY_RE, _DATE_NUM_RE):
        for m in regex.finditer(text):
            if free(m) and (d := _date(m["y"], m["m"], m["d"])):
                add(m, NormalisedValue(type=ValueType.date, date_value=d))
    for m in _RATIO_RE.finditer(text):
        if not free(m):
            continue
        if m["x"]:
            amount = _dec(m["x"])
        else:
            a, b = _dec(m["a"]), _dec(m["b"])
            amount = (a / b).normalize() if a is not None and b else None
        if amount is not None:
            add(m, NormalisedValue(type=ValueType.ratio, amount=amount, unit="x"))
    return [v for _, v in sorted(found, key=lambda t: t[0])]


def normalise(raw: str | None) -> NormalisedValue | None:
    """Typed value if `raw` states exactly one distinct typed value; plain integer; else text."""
    if raw is None or not raw.strip():
        return None
    distinct: list[NormalisedValue] = []
    for v in find_values(raw):
        if not any(same_value(v, d) for d in distinct):
            distinct.append(v)
    if len(distinct) == 1:
        return distinct[0]
    if not distinct and (m := _INT_RE.match(raw)) and "," not in raw.strip() and "." not in raw.strip():
        return NormalisedValue(type=ValueType.integer, amount=Decimal(m["num"]))
    return NormalisedValue(type=ValueType.text, text=" ".join(raw.split()))


def same_value(a: NormalisedValue, b: NormalisedValue) -> bool:
    """Typed equality. Currency only compared when both sides state it ('$' is ambiguous)."""
    if a.type is not b.type:
        return False
    if a.type is ValueType.date:
        return a.date_value == b.date_value
    if a.type is ValueType.text:
        return text_key(a.text) == text_key(b.text)
    if a.amount is None or b.amount is None or a.amount != b.amount:
        return False
    return a.currency is None or b.currency is None or a.currency == b.currency


_QUOTES = str.maketrans({"“": '"', "”": '"', "‘": "'", "’": "'", "|": " "})


def text_key(s: str | None) -> str:
    """Comparison form of free text: unified quotes, casefolded, table pipes and whitespace collapsed."""
    return " ".join((s or "").translate(_QUOTES).casefold().split())
