"""Conversion between minor and major currency units.

The Meta Marketing API is not consistent about units, and the inconsistency is
easy to miss because both forms are transmitted as strings. Budgets and bids are
integers in the account currency's **minor** unit: ``"5000"`` on a USD account is
$50.00. Insights spend is already a decimal in the **major** unit: ``"50.00"``
means $50.00.

Dividing by 100 is wrong for a significant share of currencies. Japanese yen and
Korean won have no minor unit at all, so ``"5000"`` on a JPY account is ¥5,000,
and treating it as ¥50 understates the budget by two orders of magnitude.
Several Middle Eastern currencies use three decimal places and would be
overstated by ten.

All arithmetic is :class:`~decimal.Decimal`. Float cannot represent 0.01
exactly, and money that a client compares against their invoice must not carry
representation error.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Final

# ISO 4217 currencies with no minor unit: the smallest unit is the currency
# itself, so the transmitted integer needs no scaling.
_ZERO_DECIMAL_CURRENCIES: Final[frozenset[str]] = frozenset(
    {
        "BIF",
        "CLP",
        "DJF",
        "GNF",
        "ISK",
        "JPY",
        "KMF",
        "KRW",
        "PYG",
        "RWF",
        "UGX",
        "UYI",
        "VND",
        "VUV",
        "XAF",
        "XOF",
        "XPF",
    }
)

# ISO 4217 currencies whose minor unit is one thousandth.
_THREE_DECIMAL_CURRENCIES: Final[frozenset[str]] = frozenset(
    {"BHD", "IQD", "JOD", "KWD", "LYD", "OMR", "TND"}
)

_DEFAULT_MINOR_UNIT_EXPONENT: Final[int] = 2
_ZERO_MINOR_UNIT_EXPONENT: Final[int] = 0
_THREE_MINOR_UNIT_EXPONENT: Final[int] = 3


def minor_unit_exponent(currency: str | None) -> int:
    """Return how many decimal places the currency's minor unit occupies.

    Args:
        currency: ISO 4217 alphabetic code, case-insensitive. ``None`` or an
            unrecognized code falls back to two places, which is correct for the
            large majority of currencies Meta bills in.

    Returns:
        The exponent: ``0``, ``2``, or ``3``.
    """
    if currency is None:
        return _DEFAULT_MINOR_UNIT_EXPONENT
    normalized = currency.strip().upper()
    if normalized in _ZERO_DECIMAL_CURRENCIES:
        return _ZERO_MINOR_UNIT_EXPONENT
    if normalized in _THREE_DECIMAL_CURRENCIES:
        return _THREE_MINOR_UNIT_EXPONENT
    return _DEFAULT_MINOR_UNIT_EXPONENT


def minor_units_to_major(minor_amount: int | None, currency: str | None) -> Decimal | None:
    """Convert an amount in minor units to major units.

    Args:
        minor_amount: Amount as Meta transmits it, e.g. ``5000`` for $50.00.
            ``None`` passes through, since an unset budget is not zero.
        currency: ISO 4217 code of the owning account.

    Returns:
        The amount in major units, or ``None`` when ``minor_amount`` is ``None``.
    """
    if minor_amount is None:
        return None
    exponent = minor_unit_exponent(currency)
    return Decimal(minor_amount) / (Decimal(10) ** exponent)


def major_units_to_minor(major_amount: Decimal, currency: str | None) -> int:
    """Convert an amount in major units to the integer Meta expects.

    Used when writing a budget back through the API, where a non-integer value
    is rejected.

    Args:
        major_amount: Amount in major units, e.g. ``Decimal("50.00")``.
        currency: ISO 4217 code of the owning account.

    Returns:
        The amount in minor units, rounded to the nearest whole minor unit.
    """
    exponent = minor_unit_exponent(currency)
    scaled = major_amount * (Decimal(10) ** exponent)
    return int(scaled.quantize(Decimal(1)))
