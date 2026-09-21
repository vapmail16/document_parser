from datetime import date
from decimal import Decimal

import pytest

from credit_extract.contracts import ValueType
from credit_extract.normalise import find_values, normalise, same_value


@pytest.mark.parametrize("raw, vtype, amount, currency", [
    ("$50,000,000", ValueType.money, "50000000", None),
    ("Fifty Million Dollars ($50,000,000)", ValueType.money, "50000000", None),
    ("US$1.5 billion", ValueType.money, "1500000000", "USD"),
    ("€250,000,000", ValueType.money, "250000000", "EUR"),
    ("0.910%", ValueType.percentage, "0.91", None),
    ("2.25 percent", ValueType.percentage, "2.25", None),
    ("25 basis points", ValueType.bps, "25", None),
    ("3.50 to 1.00", ValueType.ratio, "3.5", None),
    ("3.50:1.00", ValueType.ratio, "3.5", None),
    ("4.25x", ValueType.ratio, "4.25", None),
    ("30", ValueType.integer, "30", None),
])
def test_typed_values(raw, vtype, amount, currency):
    v = normalise(raw)
    assert v.type is vtype and v.amount == Decimal(amount) and v.currency == currency


@pytest.mark.parametrize("raw", ["October 28, 2022", "28 October 2022", "10/28/2022"])
def test_dates(raw):
    v = normalise(raw)
    assert v.type is ValueType.date and v.date_value == date(2022, 10, 28)


def test_text_and_ambiguous_fall_back_to_text():
    assert normalise("Bank of America, N.A.").type is ValueType.text
    assert normalise("1.375% | 0.375%").type is ValueType.text  # two distinct values: not one field
    assert normalise("1.375% (or 1.375 percent)").type is ValueType.percentage  # same value twice
    assert normalise("  ") is None


def test_find_values_in_prose():
    quote = "an aggregate principal amount not to exceed $100,000,000 at any time, plus 0.50%"
    kinds = [(v.type, v.amount) for v in find_values(quote)]
    assert kinds == [(ValueType.money, Decimal("100000000")), (ValueType.percentage, Decimal("0.50"))]


def test_percent_not_misread_as_ratio():
    assert [v.type for v in find_values("from 1.50% to 2.50%")] == [ValueType.percentage, ValueType.percentage]


def test_same_value_currency_rule():
    assert same_value(normalise("$5,000,000"), normalise("USD 5,000,000"))  # '$' ambiguous -> matches
    assert not same_value(normalise("€5,000,000"), normalise("USD 5,000,000"))
    assert not same_value(normalise("1.375%"), normalise("1.875%"))
