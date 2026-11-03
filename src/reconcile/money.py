"""Currency minor units and exact decimal-string parsing.

Money in this system is always an integer count of a currency's smallest unit.
That is only well defined once you know how many decimal places the currency
has, and the common shortcut of multiplying by 100 is wrong for a large part of
the world: JPY and KRW have no minor unit at all, and the Gulf currencies have
three places. A currency-blind parser reads 1000 JPY as 100000 minor units, a
hundredfold error in the direction that quietly loses money.

So the exponent table is explicit, an unknown currency is refused rather than
guessed at, and parsing goes through Decimal. No float ever touches an amount:
float(0.07) * 100 is 7.000000000000001, and that rounds its way into a ledger.

Standard library only.
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Any, Mapping, Optional

# ISO 4217 minor units for the currencies PayPal transacts in.
# Source: ISO 4217 and PayPal's own currency support list. Exponent is the
# number of decimal places, so minor_units = amount * 10**exponent.
MINOR_UNIT_EXPONENTS: Mapping[str, int] = {
    # No minor unit.
    "BIF": 0, "CLP": 0, "DJF": 0, "GNF": 0, "HUF": 0, "ISK": 0,
    "JPY": 0, "KMF": 0, "KRW": 0, "PYG": 0, "RWF": 0, "UGX": 0,
    "UYI": 0, "VND": 0, "VUV": 0, "XAF": 0, "XOF": 0, "XPF": 0,
    # Two decimal places.
    "AED": 2, "ALL": 2, "ANG": 2, "ARS": 2, "AUD": 2, "AWG": 2, "BAM": 2,
    "BBD": 2, "BDT": 2, "BGN": 2, "BMD": 2, "BND": 2, "BOB": 2, "BRL": 2,
    "BSD": 2, "BWP": 2, "BZD": 2, "CAD": 2, "CHF": 2, "CNY": 2, "COP": 2,
    "CRC": 2, "CUP": 2, "CVE": 2, "CZK": 2, "DKK": 2, "DOP": 2, "DZD": 2,
    "EGP": 2, "ETB": 2, "EUR": 2, "FJD": 2, "FKP": 2, "GBP": 2, "GEL": 2,
    "GHS": 2, "GIP": 2, "GMD": 2, "GTQ": 2, "GYD": 2, "HKD": 2, "HNL": 2,
    "HRK": 2, "HTG": 2, "IDR": 2, "ILS": 2, "INR": 2, "JMD": 2, "KES": 2,
    "KGS": 2, "KHR": 2, "KYD": 2, "KZT": 2, "LAK": 2, "LBP": 2, "LKR": 2,
    "LRD": 2, "LSL": 2, "MAD": 2, "MDL": 2, "MKD": 2, "MMK": 2, "MNT": 2,
    "MOP": 2, "MUR": 2, "MVR": 2, "MWK": 2, "MXN": 2, "MYR": 2, "MZN": 2,
    "NAD": 2, "NGN": 2, "NIO": 2, "NOK": 2, "NPR": 2, "NZD": 2, "PAB": 2,
    "PEN": 2, "PGK": 2, "PHP": 2, "PKR": 2, "PLN": 2, "QAR": 2, "RON": 2,
    "RSD": 2, "RUB": 2, "SAR": 2, "SBD": 2, "SCR": 2, "SEK": 2, "SGD": 2,
    "SHP": 2, "SLL": 2, "SOS": 2, "SRD": 2, "SVC": 2, "SZL": 2, "THB": 2,
    "TJS": 2, "TOP": 2, "TRY": 2, "TTD": 2, "TWD": 2, "TZS": 2, "UAH": 2,
    "USD": 2, "UYU": 2, "UZS": 2, "WST": 2, "XCD": 2, "YER": 2, "ZAR": 2,
    "ZMW": 2,
    # Three decimal places.
    "BHD": 3, "IQD": 3, "JOD": 3, "KWD": 3, "LYD": 3, "OMR": 3, "TND": 3,
}


class UnsupportedCurrencyError(ValueError):
    """Raised for a currency whose minor unit this system does not know.

    Deliberately not a fallback to two decimal places: a wrong exponent is a
    hundredfold accounting error, so an unknown currency must stop the run.
    """


def normalize_currency(currency: Optional[str]) -> str:
    """Upper-case and strip a currency code, rejecting anything malformed."""
    if not isinstance(currency, str):
        raise ValueError(f"currency must be a 3-letter code, got {currency!r}")
    code = currency.strip().upper()
    if len(code) != 3 or not code.isalpha():
        raise ValueError(f"currency must be a 3-letter code, got {currency!r}")
    return code


def minor_unit_exponent(currency: Optional[str]) -> int:
    """Decimal places for `currency`, or raise UnsupportedCurrencyError."""
    code = normalize_currency(currency)
    try:
        return MINOR_UNIT_EXPONENTS[code]
    except KeyError:
        raise UnsupportedCurrencyError(
            f"unsupported currency {code}: its minor unit is unknown, and "
            f"assuming one would risk a scaling error in the ledger"
        ) from None


def parse_money_to_minor_units(value: Any, currency: Optional[str]) -> int:
    """Parse a decimal amount into integer minor units of `currency`.

    Accepts the decimal strings PayPal sends ("25.00"), and ints or floats for
    convenience. An amount carrying more precision than the currency can
    represent is refused rather than rounded: rounding someone's money is a
    decision this layer is not entitled to make.
    """
    exponent = minor_unit_exponent(currency)

    if value is None:
        raise ValueError("cannot parse a monetary amount from None")

    text = str(value).strip()
    if not text:
        raise ValueError("cannot parse a monetary amount from an empty string")

    try:
        amount = Decimal(text)
    except (InvalidOperation, ValueError, ArithmeticError) as exc:
        raise ValueError(f"invalid monetary value: {value!r}") from exc

    if not amount.is_finite():
        raise ValueError(f"invalid monetary value: {value!r}")

    scaled = amount.scaleb(exponent)
    if scaled != scaled.to_integral_value():
        raise ValueError(
            f"monetary value {text!r} carries more precision than "
            f"{normalize_currency(currency)} allows ({exponent} decimal places)"
        )

    return int(scaled.to_integral_value())


def format_minor_units(minor_units: int, currency: Optional[str]) -> str:
    """Render minor units as a decimal string. For display and request bodies.

    Never use the result for arithmetic; it exists so that integers can be
    handed back to PayPal and shown to people in the shape they expect.
    """
    if isinstance(minor_units, bool) or not isinstance(minor_units, int):
        raise TypeError(
            f"minor_units must be an int, got {type(minor_units).__name__}"
        )
    exponent = minor_unit_exponent(currency)
    if exponent == 0:
        return str(minor_units)
    scaled = Decimal(minor_units).scaleb(-exponent)
    return f"{scaled:.{exponent}f}"
