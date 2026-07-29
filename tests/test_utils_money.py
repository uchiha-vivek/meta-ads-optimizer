"""Tests for minor/major currency unit conversion."""

from __future__ import annotations

from decimal import Decimal

import pytest

from app.utils.money import major_units_to_minor, minor_unit_exponent, minor_units_to_major


@pytest.mark.parametrize(
    ("currency", "expected_exponent"),
    [
        ("USD", 2),
        ("EUR", 2),
        ("GBP", 2),
        ("JPY", 0),
        ("KRW", 0),
        ("KWD", 3),
        ("jpy", 0),
        ("  JPY  ", 0),
        (None, 2),
        ("ZZZ", 2),
    ],
)
def test_minor_unit_exponent(currency: str | None, expected_exponent: int) -> None:
    assert minor_unit_exponent(currency) == expected_exponent


def test_two_decimal_currency_converts_by_one_hundred() -> None:
    assert minor_units_to_major(5000, "USD") == Decimal("50.00")


def test_zero_decimal_currency_is_not_divided() -> None:
    # The bug this guards against: dividing yen by 100 understates a ¥5,000
    # budget as ¥50, a hundredfold error on a live ad account.
    assert minor_units_to_major(5000, "JPY") == Decimal(5000)


def test_three_decimal_currency_converts_by_one_thousand() -> None:
    assert minor_units_to_major(5000, "KWD") == Decimal(5)


def test_none_passes_through_because_unset_is_not_zero() -> None:
    # An absent budget and a zero budget mean very different things: one is
    # inherited from the parent, the other stops delivery.
    assert minor_units_to_major(None, "USD") is None


def test_conversion_is_exact_rather_than_floating_point() -> None:
    converted = minor_units_to_major(1, "USD")

    assert converted == Decimal("0.01")
    assert isinstance(converted, Decimal)


@pytest.mark.parametrize("currency", ["USD", "JPY", "KWD", None])
def test_round_trip_preserves_the_original_amount(currency: str | None) -> None:
    original_minor = 123_456

    major = minor_units_to_major(original_minor, currency)
    assert major is not None

    assert major_units_to_minor(major, currency) == original_minor


def test_major_to_minor_rounds_to_a_whole_minor_unit() -> None:
    # Meta rejects fractional budgets, so the result must always be an integer.
    assert major_units_to_minor(Decimal("50.004"), "USD") == 5000
    assert major_units_to_minor(Decimal("50.006"), "USD") == 5001
