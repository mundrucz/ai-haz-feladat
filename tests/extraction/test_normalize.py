from datetime import date
from decimal import Decimal

import pytest

from app.extraction.company import Company
from app.extraction.normalize import (
    currency_candidates,
    find_amounts,
    infer_date_order,
    infer_decimal_separator,
    normalize_currency,
    normalize_key,
    normalize_with_map,
    parse_amount,
    parse_date,
)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("1 234,56", "1234.56"),
        ("1\u00a0234,56 Ft", "1234.56"),
        ("1\u202f234,56", "1234.56"),
        ("1.234,56", "1234.56"),
        ("1,234.56", "1234.56"),
        ("1'234.56", "1234.56"),
        ("1’234.56", "1234.56"),
        ("12 345 678", "12345678"),
        ("1.234.567", "1234567"),
        ("1,234,567.8", "1234567.8"),
        ("1,23,456.78", "123456.78"),
        ("1234,5", "1234.5"),
        ("0,5", "0.5"),
        ("0.123", "0.123"),
        ("EUR 99.90", "99.90"),
        ("€ 1 000", "1000"),
        ("12 345", "12345"),
        ("1 234,567", "1234.567"),
        ("-1 234,56", "-1234.56"),
        ("\u22121 234,56", "-1234.56"),
        ("1 234,56-", "-1234.56"),
        ("(1 234,56)", "-1234.56"),
        ("-€5", "-5"),
        ("1234567", "1234567"),
        ("１２３４，５", "1234.5"),
    ],
)
def test_parse_amount(raw, expected):
    assert parse_amount(raw) == Decimal(expected)


@pytest.mark.parametrize("raw", [None, "", "abc", "12 abc 34", "100-200", "1.2.3,4,5"])
def test_parse_amount_unparseable(raw):
    assert parse_amount(raw) is None


def test_ambiguous_group_defaults_to_thousands():
    assert parse_amount("1.234") == Decimal("1234")
    assert parse_amount("1,234") == Decimal("1234")


def test_decimal_hint_resolves_ambiguity():
    assert parse_amount("1.234", decimal_hint=".") == Decimal("1.234")
    assert parse_amount("1,234", decimal_hint=",") == Decimal("1.234")
    assert parse_amount("1.234", decimal_hint=",") == Decimal("1234")


def test_hint_does_not_override_clear_structure():
    assert parse_amount("12,5", decimal_hint=".") == Decimal("12.5")


@pytest.mark.parametrize(
    ("raws", "expected"),
    [
        (["1 234,56", "1.234"], ","),
        (["1,234.56"], "."),
        (["1.234.567"], ","),
        (["1.234", "1,234"], None),
        (["1 234,56", "1,234.56"], None),
        (["100"], None),
    ],
)
def test_infer_decimal_separator(raws, expected):
    assert infer_decimal_separator(raws) == expected


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("2026-03-15", date(2026, 3, 15)),
        ("2026-03-15T10:30:00", date(2026, 3, 15)),
        ("2026. 03. 15.", date(2026, 3, 15)),
        ("2026.03.15", date(2026, 3, 15)),
        ("15.03.2026", date(2026, 3, 15)),
        ("15/03/2026", date(2026, 3, 15)),
        ("03/15/2026", date(2026, 3, 15)),
        ("05/05/2026", date(2026, 5, 5)),
        ("20260315", date(2026, 3, 15)),
        ("2026年3月15日", date(2026, 3, 15)),
        ("2026년 3월 15일", date(2026, 3, 15)),
        ("2026. március 15.", date(2026, 3, 15)),
        ("15 mars 2026", date(2026, 3, 15)),
        ("15. März 2026", date(2026, 3, 15)),
        ("March 15, 2026", date(2026, 3, 15)),
        ("15th of March 2026", date(2026, 3, 15)),
        ("1 Sep 2026", date(2026, 9, 1)),
        ("3 września 2026", date(2026, 9, 3)),
        ("15 března 2026", date(2026, 3, 15)),
        ("1 setiembre 2026", date(2026, 9, 1)),
        ("29 februari 2028", date(2028, 2, 29)),
    ],
)
def test_parse_date(raw, expected):
    assert parse_date(raw) == expected


@pytest.mark.parametrize(
    "raw",
    [None, "", "nincs", "03/04/2026", "31.02.2026", "15.03.26", "2026-13-01", "15 2026", "March April 2026", "2026-03"],
)
def test_parse_date_rejects_ambiguous_or_invalid(raw):
    assert parse_date(raw) is None


def test_date_order_hint_resolves_ambiguity():
    assert parse_date("03/04/2026", order="DMY") == date(2026, 4, 3)
    assert parse_date("03/04/2026", order="MDY") == date(2026, 3, 4)


def test_infer_date_order():
    assert infer_date_order(["15/03/2026", "03/04/2026"]) == "DMY"
    assert infer_date_order(["03/15/2026"]) == "MDY"
    assert infer_date_order(["03/04/2026"]) is None
    assert infer_date_order(["15/03/2026", "03/15/2026"]) is None


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("HUF", "HUF"),
        ("JPY", "JPY"),
        ("CHF", "CHF"),
        ("SEK", "SEK"),
        ("CZK", "CZK"),
        ("RON", "RON"),
        ("€", "EUR"),
        ("Ft", "HUF"),
        ("Ft.", "HUF"),
        ("zł", "PLN"),
        ("円", "JPY"),
        ("₹", "INR"),
        ("euro", "EUR"),
        (" EUR ", "EUR"),
    ],
)
def test_normalize_currency(raw, expected):
    assert normalize_currency(raw) == expected


@pytest.mark.parametrize("raw", [None, "", "VAT", "NET", "TAX", "all", "try", "$", "kr", "¥", "xyz"])
def test_normalize_currency_unknown_or_ambiguous(raw):
    assert normalize_currency(raw) is None


def test_ambiguous_symbols_list_candidates():
    assert set(currency_candidates("$")) >= {"USD", "CAD", "AUD"}
    assert set(currency_candidates("kr")) >= {"SEK", "NOK", "DKK"}
    assert currency_candidates("VAT") == ()


def test_normalize_key():
    assert normalize_key("26481937-2-43") == "26481937243"
    assert normalize_key("Nordwind Solutions Kft.") == "NORDWINDSOLUTIONSKFT"
    assert normalize_key("Árvíztűrő tükörfúrógép") == "ARVIZTUROTUKORFUROGEP"
    assert normalize_key("NW/2026-0042") == normalize_key("nw 2026 0042")
    assert normalize_key(None) == ""
    assert normalize_key("株式会社テスト 123") == normalize_key("株式会社テスト123")


def test_company_appears_in_any_formatting():
    company = Company(name="Nordwind Solutions Kft.", tax_number="26481937-2-43", eu_vat="HU26481937")
    assert company.appears_in("Vevő adószáma: 26481937 2 43")
    assert company.appears_in("VAT ID: HU 26481937")
    assert company.appears_in("NORDWIND  SOLUTIONS KFT")
    assert not company.appears_in("Teljesen más cég 12345678-1-42")


def test_company_without_identifiers_never_matches():
    assert not Company().appears_in("bármi")


def test_dotted_date_ending_with_year_is_day_first():
    assert parse_date("09.03.2026") == date(2026, 3, 9)
    assert parse_date("03/04/2026") is None


def test_find_amounts_skips_identifiers_and_dates():
    text = "Összesen 1 234,56 Ft, számla 2026/0042, dátum 06.01.2026, IBAN HU17 1040 5004"
    assert [match.value for match in find_amounts(text)] == [Decimal("1234.56")]


def test_find_amounts_positions_and_negatives():
    text = "Jóváírás -500,00 EUR"
    (match,) = find_amounts(text)
    assert match.value == Decimal("-500.00")
    assert text[match.end :].strip() == "EUR"


def test_find_amounts_on_a_bare_amount_line():
    assert [match.value for match in find_amounts("16 222,00")] == [Decimal("16222.00")]


def test_normalize_with_map_tracks_original_positions():
    norm, origin = normalize_with_map("Számla száma: É")
    assert norm == "szamla szama: e"
    assert len(origin) == len(norm)
    assert "Számla száma: É"[origin[-1]] == "É"

