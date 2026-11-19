from datetime import date
from decimal import Decimal

from app import (
    build_dashboard,
    convert_to_huf,
    extract_invoice,
    fetch_mnb_rates,
    get_exchange_rate,
    init_db,
    normalize_tax_id,
    parse_amount,
    parse_date,
)


def test_extracts_labeled_invoice_fields_and_direction(monkeypatch):
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    text = """
    Please include the invoice number in the transfer.
    SZÁMLASZÁM / Invoice No.:
    NW-2026-001
    Seller: Nordwind Solutions Kft.
    Seller tax number: 26481937-2-43
    Buyer: Example Partner Ltd.
    Buyer VAT: GB123456789
    Invoice date: March 15, 2026
    Service date: 13.03.2026
    Due date: 2026-04-15
    Currency: EUR
    Grand total: EUR 1,234.50
    """
    invoice = extract_invoice(text)
    from app import validate_invoice

    validated = validate_invoice(invoice)
    assert validated["invoice_number"] == "NW-2026-001"
    assert validated["issue_date"] == "2026-03-15"
    assert validated["service_date"] == "2026-03-13"
    assert validated["due_date"] == "2026-04-15"
    assert validated["gross_amount"] == Decimal("1234.50")
    assert validated["direction"] == "kimenő"
    assert validated["partner_name"] == "Example Partner Ltd."


def test_parses_common_date_and_amount_formats():
    assert parse_date("15 mars 2026") == date(2026, 3, 15)
    assert parse_date("2026. január 15.") == date(2026, 1, 15)
    assert parse_date("15 de marzo de 2026") == date(2026, 3, 15)
    assert parse_date("2026.03.15.") == date(2026, 3, 15)
    assert parse_amount("1 234,56") == Decimal("1234.56")
    assert parse_amount("1.234,56") == Decimal("1234.56")
    assert parse_amount("1,234.56") == Decimal("1234.56")


def test_huf_rate_is_one_on_the_service_date():
    service_date = date(2026, 3, 15)
    assert get_exchange_rate(service_date, "HUF") == (Decimal("1"), service_date)
    assert normalize_tax_id("HU26481937") == normalize_tax_id("26481937-2-43") == "26481937"


def test_exchange_rate_uses_last_published_rate_and_caches_interval(tmp_path, monkeypatch):
    db_path = str(tmp_path / "rates.sqlite")
    init_db(db_path)
    calls = []

    def fake_fetch(start, end, currency):
        calls.append((start, end, currency))
        if start == date(2026, 3, 16):
            return [("2026-03-16", "402.00000", 1)]
        return [("2026-03-12", "400.00000", 1), ("2026-03-13", "401.50000", 1)]

    monkeypatch.setattr("app.fetch_mnb_rates", fake_fetch)
    target = date(2026, 3, 15)
    assert get_exchange_rate(target, "EUR", db_path) == (Decimal("401.50000"), date(2026, 3, 13))
    assert get_exchange_rate(target, "EUR", db_path) == (Decimal("401.50000"), date(2026, 3, 13))
    assert get_exchange_rate(date(2026, 3, 16), "EUR", db_path) == (Decimal("402.00000"), date(2026, 3, 16))
    assert len(calls) == 2
    assert calls[1] == (date(2026, 3, 16), date(2026, 3, 16), "EUR")


def test_mnb_response_parses_escaped_xml_and_currency_units(monkeypatch):
    class FakeResponse:
        content = (
            b'<Envelope><GetExchangeRatesResult>'
            b'&lt;MNBExchangeRates&gt;&lt;Day date="2026-03-13"&gt;'
            b'&lt;Rate unit="100" curr="PLN"&gt;365,67000&lt;/Rate&gt;'
            b'&lt;/Day&gt;&lt;/MNBExchangeRates&gt;'
            b'</GetExchangeRatesResult></Envelope>'
        )

        def raise_for_status(self):
            pass

    def fake_post(url, data, headers, timeout):
        assert url == "http://www.mnb.hu/arfolyamok.asmx"
        assert b"<web:currencyNames>PLN</web:currencyNames>" in data
        assert "GetExchangeRates" in headers["SOAPAction"]
        return FakeResponse()

    monkeypatch.setattr("app.requests.post", fake_post)
    assert fetch_mnb_rates(date(2026, 3, 1), date(2026, 3, 15), "PLN") == [
        ("2026-03-13", "365.67000", 100)
    ]


def test_dashboard_aggregates_by_tax_id_and_fills_empty_months():
    rows = [
        {"status": "processed", "partner_name": "Example GmbH", "partner_tax_id": "DE123",
         "direction": "bejövő", "huf_amount": 1200, "due_date": "2026-01-20"},
        {"status": "processed", "partner_name": "EXAMPLE GMBH", "partner_tax_id": "DE123",
         "direction": "kimenő", "huf_amount": 500, "due_date": "2026-03-01"},
        {"status": "processed", "partner_name": "Example Kft.", "partner_tax_id": "HU12345678",
         "direction": "bejövő", "huf_amount": 100, "due_date": "2026-01-21"},
        {"status": "processed", "partner_name": "EXAMPLE KFT", "partner_tax_id": "12345678-2-42",
         "direction": "kimenő", "huf_amount": 200, "due_date": "2026-03-03"},
        {"status": "processed", "partner_name": "Other Ltd", "partner_tax_id": "GB456",
         "direction": "bejövő", "huf_amount": 900, "due_date": "2026-03-02"},
    ]
    report = build_dashboard(rows)
    example = next(partner for partner in report["partners"] if partner["tax_id"] == "DE123")
    assert example["bejövő"] == 1200
    assert example["kimenő"] == 500
    domestic = next(partner for partner in report["partners"] if partner["tax_id"] == "HU12345678")
    assert domestic["bejövő"] == 100
    assert domestic["kimenő"] == 200
    assert [month["month"] for month in report["months"]] == ["2026-01", "2026-02", "2026-03"]
    assert report["months"][1] == {"month": "2026-02", "bejövő": 0, "kimenő": 0}
    assert convert_to_huf(Decimal("10.5"), Decimal("1")) == 11
