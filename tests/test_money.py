"""Currency exponents and decimal-string parsing.

Money is integer minor units everywhere in the core. These tests exist because
a currency-blind "multiply by 100" is wrong for a third of the world's
currencies, and silently wrong in the direction that loses money.
"""

import pytest

from reconcile.money import (
    MINOR_UNIT_EXPONENTS,
    UnsupportedCurrencyError,
    format_minor_units,
    minor_unit_exponent,
    normalize_currency,
    parse_money_to_minor_units,
)


# --------------------------------------------------------------------------
# The exponent table
# --------------------------------------------------------------------------


def test_usd_has_two_decimal_places():
    assert minor_unit_exponent("USD") == 2


def test_jpy_has_zero_decimal_places():
    assert minor_unit_exponent("JPY") == 0


@pytest.mark.parametrize(
    "currency,exponent",
    [
        ("USD", 2), ("EUR", 2), ("GBP", 2), ("CAD", 2), ("AUD", 2),
        ("CHF", 2), ("SEK", 2), ("NOK", 2), ("DKK", 2), ("PLN", 2),
        ("CZK", 2), ("HKD", 2), ("SGD", 2), ("NZD", 2), ("MXN", 2),
        ("BRL", 2), ("INR", 2), ("ILS", 2), ("PHP", 2), ("THB", 2),
        ("TWD", 2), ("MYR", 2), ("RUB", 2), ("CNY", 2),
        ("JPY", 0), ("KRW", 0), ("HUF", 0), ("ISK", 0), ("CLP", 0), ("VND", 0),
        ("BHD", 3), ("JOD", 3), ("KWD", 3), ("OMR", 3), ("TND", 3),
    ],
)
def test_iso_4217_minor_units(currency, exponent):
    assert minor_unit_exponent(currency) == exponent


def test_every_exponent_is_a_documented_iso_value():
    assert set(MINOR_UNIT_EXPONENTS.values()) <= {0, 2, 3}


def test_currency_codes_are_all_three_upper_case_letters():
    for code in MINOR_UNIT_EXPONENTS:
        assert len(code) == 3 and code.isupper() and code.isalpha(), code


def test_unsupported_currency_is_rejected_not_guessed():
    with pytest.raises(UnsupportedCurrencyError) as excinfo:
        minor_unit_exponent("XYZ")
    assert "XYZ" in str(excinfo.value)


def test_unsupported_currency_error_is_a_value_error():
    assert issubclass(UnsupportedCurrencyError, ValueError)


@pytest.mark.parametrize("bad", ["", None, "US", "USDD", "12", "  "])
def test_malformed_currency_codes_are_rejected(bad):
    with pytest.raises(ValueError):
        normalize_currency(bad)


def test_currency_is_normalized_case_and_whitespace():
    assert normalize_currency(" usd ") == "USD"
    assert minor_unit_exponent(" jpy ") == 0


# --------------------------------------------------------------------------
# Parsing decimal strings into minor units
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "value,currency,expected",
    [
        ("25.00", "USD", 2500),
        ("25", "USD", 2500),
        ("0.01", "USD", 1),
        ("10.01", "USD", 1001),
        ("1234567.89", "USD", 123456789),
        ("25.5", "USD", 2550),
        ("1000", "JPY", 1000),          # zero-exponent: no scaling
        ("1234", "JPY", 1234),
        ("1.000", "BHD", 1000),         # three-exponent
        ("0.001", "KWD", 1),
        ("19.99", "EUR", 1999),
        ("100", "KRW", 100),
    ],
)
def test_parses_decimal_strings_exactly(value, currency, expected):
    assert parse_money_to_minor_units(value, currency) == expected


def test_jpy_is_not_multiplied_by_one_hundred():
    """The defect this table exists to prevent."""
    assert parse_money_to_minor_units("1000", "JPY") == 1000
    assert parse_money_to_minor_units("1000", "USD") == 100000


def test_no_floating_point_is_used():
    """0.07 * 100 is 7.000000000000001 in binary float; Decimal is exact."""
    assert parse_money_to_minor_units("0.07", "USD") == 7
    assert parse_money_to_minor_units("1.10", "USD") == 110
    assert parse_money_to_minor_units("8.20", "USD") == 820
    for cents in range(1, 500):
        text = f"{cents // 100}.{cents % 100:02d}"
        assert parse_money_to_minor_units(text, "USD") == cents


def test_accepts_numeric_input_as_well_as_strings():
    assert parse_money_to_minor_units(25, "USD") == 2500
    assert parse_money_to_minor_units(25.50, "USD") == 2550


def test_negative_and_zero_amounts_round_trip():
    assert parse_money_to_minor_units("0.00", "USD") == 0
    assert parse_money_to_minor_units("-5.00", "USD") == -500


@pytest.mark.parametrize("bad", [None, "", "   ", "invalid", "1.2.3", "12,34", "NaN", "Infinity", "1e"])
def test_invalid_monetary_values_are_rejected(bad):
    with pytest.raises(ValueError):
        parse_money_to_minor_units(bad, "USD")


def test_more_precision_than_the_currency_allows_is_rejected():
    """0.005 USD is not representable in cents; refuse rather than round."""
    with pytest.raises(ValueError) as excinfo:
        parse_money_to_minor_units("0.005", "USD")
    assert "precision" in str(excinfo.value).lower()

    with pytest.raises(ValueError):
        parse_money_to_minor_units("10.5", "JPY")

    with pytest.raises(ValueError):
        parse_money_to_minor_units("1.0001", "BHD")


def test_trailing_zeros_within_the_allowed_precision_are_fine():
    assert parse_money_to_minor_units("10.00", "JPY") == 10
    assert parse_money_to_minor_units("10.500", "USD") == 1050


def test_parsing_an_unsupported_currency_is_rejected():
    with pytest.raises(UnsupportedCurrencyError):
        parse_money_to_minor_units("10.00", "XYZ")


# --------------------------------------------------------------------------
# Formatting back out (reporting only; never used for arithmetic)
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "minor,currency,expected",
    [
        (2500, "USD", "25.00"),
        (1, "USD", "0.01"),
        (0, "USD", "0.00"),
        (-500, "USD", "-5.00"),
        (1000, "JPY", "1000"),
        (1, "KWD", "0.001"),
        (123456789, "USD", "1234567.89"),
    ],
)
def test_formats_minor_units_for_display(minor, currency, expected):
    assert format_minor_units(minor, currency) == expected


def test_format_round_trips_with_parse():
    for currency in ("USD", "JPY", "BHD", "EUR"):
        for minor in (0, 1, 999, 100000):
            text = format_minor_units(minor, currency)
            assert parse_money_to_minor_units(text, currency) == minor


def test_format_rejects_a_non_integer_amount():
    with pytest.raises(TypeError):
        format_minor_units(25.5, "USD")


# --------------------------------------------------------------------------
# Additional thorough coverage for D2 requirements
# --------------------------------------------------------------------------


def test_inr_exponent_is_two_and_scales_correctly():
    """USD and INR must both have exponent 2."""
    assert minor_unit_exponent("INR") == 2
    assert parse_money_to_minor_units("100.50", "INR") == 10050
    assert parse_money_to_minor_units("0.05", "INR") == 5
    assert format_minor_units(10050, "INR") == "100.50"


@pytest.mark.parametrize(
    "currency,value",
    [
        ("USD", "10.001"),
        ("USD", "0.0001"),
        ("USD", "25.999"),
        ("INR", "100.555"),
        ("INR", "0.001"),
        ("EUR", "19.999"),
        ("JPY", "1.1"),
        ("JPY", "0.01"),
        ("JPY", "100.5"),
        ("KRW", "1000.5"),
        ("HUF", "250.2"),
        ("CLP", "500.1"),
        ("BHD", "1.0001"),
        ("KWD", "0.0005"),
        ("OMR", "10.1234"),
        ("TND", "5.00001"),
    ],
)
def test_precision_beyond_supported_minor_unit_fails_closed(currency, value):
    """Never silently round: excess precision must raise a ValueError mentioning precision."""
    with pytest.raises(ValueError) as excinfo:
        parse_money_to_minor_units(value, currency)
    assert "precision" in str(excinfo.value).lower()


@pytest.mark.parametrize(
    "bad",
    [
        "   \t\n  ",
        "$10.00",
        "10.00 USD",
        "10,000.00",
        "10..00",
        "--10.00",
        "++10.00",
        "10-00",
        "10/2",
        "-NaN",
        "+Infinity",
        "-inf",
        "None",
        "null",
        "undefined",
        "0x10",
        "10.0.0",
        "10e2",
    ],
)
def test_malformed_string_values_are_strictly_rejected(bad):
    with pytest.raises(ValueError):
        parse_money_to_minor_units(bad, "USD")


@pytest.mark.parametrize(
    "currency",
    [
        "BIF", "CLP", "DJF", "GNF", "HUF", "ISK", "JPY", "KMF",
        "KRW", "PYG", "RWF", "UGX", "UYI", "VND", "VUV", "XAF", "XOF", "XPF",
    ],
)
def test_all_zero_decimal_currencies_parse_and_format_without_scaling(currency):
    """Zero-decimal currencies must have exponent 0 and no fractional parts."""
    assert minor_unit_exponent(currency) == 0
    assert parse_money_to_minor_units("500", currency) == 500
    assert parse_money_to_minor_units("0", currency) == 0
    assert parse_money_to_minor_units("-50", currency) == -50
    assert format_minor_units(500, currency) == "500"
    with pytest.raises(ValueError):
        parse_money_to_minor_units("500.5", currency)


@pytest.mark.parametrize(
    "currency",
    ["BHD", "IQD", "JOD", "KWD", "LYD", "OMR", "TND"],
)
def test_all_three_decimal_currencies_parse_and_format_three_places(currency):
    """Three-decimal currencies must have exponent 3 and format with 3 decimal places."""
    assert minor_unit_exponent(currency) == 3
    assert parse_money_to_minor_units("1.234", currency) == 1234
    assert parse_money_to_minor_units("0.001", currency) == 1
    assert parse_money_to_minor_units("-1.234", currency) == -1234
    assert format_minor_units(1234, currency) == "1.234"
    assert format_minor_units(1, currency) == "0.001"
    assert format_minor_units(0, currency) == "0.000"
    with pytest.raises(ValueError):
        parse_money_to_minor_units("1.2345", currency)


@pytest.mark.parametrize(
    "crypto_or_invalid",
    ["BTC", "ETH", "SOL", "XYZ", "ZZZ", "XAU", "XAG", "ABC"],
)
def test_unsupported_and_crypto_currencies_raise_unsupported_error(crypto_or_invalid):
    with pytest.raises(UnsupportedCurrencyError) as excinfo:
        minor_unit_exponent(crypto_or_invalid)
    assert crypto_or_invalid in str(excinfo.value)
    with pytest.raises(UnsupportedCurrencyError):
        parse_money_to_minor_units("10.00", crypto_or_invalid)


@pytest.mark.parametrize(
    "non_three_letter",
    ["USDT", "DOGE", "TOOLONG", "A", "AB", "123", "US1"],
)
def test_non_three_letter_currencies_raise_value_error(non_three_letter):
    with pytest.raises(ValueError):
        minor_unit_exponent(non_three_letter)
    with pytest.raises(ValueError):
        parse_money_to_minor_units("10.00", non_three_letter)


def test_accounting_zero_and_negative_amounts_preserve_exact_minor_units():
    """Verify that zero and negative amounts preserve exact values across 0, 2, and 3 exponents."""
    assert parse_money_to_minor_units("0.00", "USD") == 0
    assert parse_money_to_minor_units("-10.00", "USD") == -1000
    assert parse_money_to_minor_units("0", "JPY") == 0
    assert parse_money_to_minor_units("-100", "JPY") == -100
    assert parse_money_to_minor_units("0.000", "BHD") == 0
    assert parse_money_to_minor_units("-1.500", "BHD") == -1500
