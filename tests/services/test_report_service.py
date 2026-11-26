from datetime import date
from decimal import Decimal

import pytest

from app.clients.mnb_client import MnbError, RateQuote
from app.core.i18n import Translator
from app.db.database import Database
from app.extraction.company import Company
from app.extraction.models import InvoiceFields
from app.main import WEB_DIR
from app.repositories.invoice_repository import InvoiceGroup, InvoiceRepository
from app.repositories.rate_repository import RateRepository
from app.services.failed_file_service import FailedFileLog
from app.services.invoice_service import classify
from app.services.rate_service import RateService
from app.services.report_service import ReportService, month_range, partner_key

COMPANY = Company("Nordwind Solutions Kft.", "26481937-2-43", "HU26481937")


def fields(**overrides) -> InvoiceFields:
    base = dict(
        invoice_number="X-1",
        seller_name="Nordwind Solutions Kft.",
        seller_tax_id="26481937-2-43",
        buyer_name="Partner Kft.",
        buyer_tax_id="12345678-2-42",
        issue_date=date(2026, 1, 12),
        performance_date=date(2026, 1, 12),
        due_date=date(2026, 2, 11),
        currency="HUF",
        net_total=Decimal("1000"),
        vat_total=Decimal("270"),
        gross_total=Decimal("1270"),
    )
    return InvoiceFields(**{**base, **overrides})


def incoming(**overrides) -> InvoiceFields:
    return fields(
        seller_name="Partner Kft.",
        seller_tax_id="12345678-2-42",
        buyer_name="Nordwind Solutions Kft.",
        buyer_tax_id="26481937-2-43",
        **overrides,
    )


class FixedSource:
    def __init__(self, rates: dict[str, tuple[int, str]], fail: set[str] = frozenset()) -> None:
        self.rates, self.fail, self.calls = rates, fail, []

    async def fetch_rates(self, currencies, start, end):
        self.calls.append(tuple(currencies))
        if self.fail & set(currencies):
            raise MnbError("down")
        quotes = []
        for currency in currencies:
            if currency in self.rates:
                unit, value = self.rates[currency]
                quotes.append(RateQuote(currency, start, unit, Decimal(value)))
        return quotes


@pytest.fixture
async def setup(tmp_path):
    db = Database(tmp_path / "r.db")
    await db.init()
    return db, InvoiceRepository(db)


TODAY = date(2026, 6, 15)


def make_report(db, invoices, source, failure_log=None) -> ReportService:
    rates = RateService(RateRepository(db), source, today=lambda: TODAY)
    return ReportService(invoices, rates, "HUF", failure_log)


async def add(invoices, number: str, invoice_fields: InvoiceFields, status: str = "ok", issues=()) -> None:
    await invoices.save(
        f"hash-{number}", f"{number}.pdf", invoice_fields, classify(invoice_fields, COMPANY), status, list(issues)
    )


def row(report: dict, number: str) -> dict:
    return next(r for r in report["invoices"] if r["invoice_number"] == number)


async def snapshot(service: ReportService) -> dict:
    """A kimutatás és a teljes számlalista (egy oldalon) együtt, ahogy a felület látja."""
    page = await service.invoice_page(InvoiceGroup.ALL, 0, 1000)
    return {**await service.build(), "invoices": page["invoices"], "groups": page["groups"]}


async def test_cash_flow_uses_due_month_with_gap_months_and_hungarian_rounding(setup):
    db, invoices = setup
    await add(invoices, "A", fields(invoice_number="A", due_date=date(2026, 2, 11), gross_total=Decimal("1270")))
    await add(invoices, "B", incoming(invoice_number="B", due_date=date(2026, 4, 3), gross_total=Decimal("500.50")))
    await add(invoices, "C", fields(invoice_number="C", due_date=date(2026, 2, 28), gross_total=Decimal("30")))

    report = await snapshot(make_report(db, invoices, FixedSource({})))

    assert report["months"] == [
        {"month": "2026-02", "incoming": 0, "outgoing": 1300},
        {"month": "2026-03", "incoming": 0, "outgoing": 0},
        {"month": "2026-04", "incoming": 501, "outgoing": 0},
    ]


async def test_foreign_invoice_is_converted_with_performance_date_rate_and_unit(setup):
    db, invoices = setup
    await add(
        invoices,
        "J",
        fields(invoice_number="J", currency="JPY", gross_total=Decimal("10000"), performance_date=date(2026, 1, 12)),
    )
    source = FixedSource({"JPY": (100, "2.455")})

    report = await snapshot(make_report(db, invoices, source))

    item = row(report, "J")
    assert item["amount"] == 246  # 10000 * 2.455 / 100 = 245.5 -> 246
    assert (item["rate"], item["rate_unit"], item["currency"]) == ("2.455", 100, "JPY")
    assert report["months"][0]["outgoing"] == 246


async def test_each_currency_is_fetched_once_per_report(setup):
    db, invoices = setup
    for number in "ABC":
        await add(invoices, number, fields(invoice_number=number, currency="EUR"))
    source = FixedSource({"EUR": (1, "400")})
    report_service = make_report(db, invoices, source)

    await report_service.build()
    await report_service.build()

    assert source.calls == [("EUR",)]


async def test_every_foreign_currency_of_a_report_is_fetched_with_one_request(setup):
    db, invoices = setup
    await add(invoices, "A", fields(invoice_number="A", currency="EUR"))
    await add(invoices, "B", fields(invoice_number="B", currency="USD"))
    await add(invoices, "C", fields(invoice_number="C", currency="JPY"))
    source = FixedSource({"EUR": (1, "400"), "USD": (1, "350"), "JPY": (100, "2.5")})

    await snapshot(make_report(db, invoices, source))

    assert source.calls == [("EUR", "JPY", "USD")]


async def test_invoice_left_out_of_the_report_is_logged_once_with_its_reason(setup, failed_log):
    db, invoices = setup
    await add(invoices, "E", fields(invoice_number="E", currency="EUR"))
    await add(invoices, "H", fields(invoice_number="H", gross_total=Decimal("2000")))
    translator = Translator(WEB_DIR / "locales", "hu")
    service = make_report(db, invoices, FixedSource({}, fail={"EUR"}), FailedFileLog(translator))

    await service.build()
    await service.build()

    [entry] = failed_log
    assert (entry.source, entry.file_name, entry.error_code) == ("report", "E.pdf", "mnb_unavailable")
    assert entry.content_hash == "hash-E"
    assert entry.error_text == "Az MNB árfolyam most nem érhető el."


async def test_mnb_failure_marks_only_affected_invoices(setup):
    db, invoices = setup
    await add(invoices, "E", fields(invoice_number="E", currency="EUR"))
    await add(invoices, "H", fields(invoice_number="H", gross_total=Decimal("2000")))

    report = await snapshot(make_report(db, invoices, FixedSource({}, fail={"EUR"})))

    assert (row(report, "E")["amount"], row(report, "E")["problem"]) == (None, "mnb_unavailable")
    assert row(report, "H")["problem"] is None
    assert report["months"][0]["outgoing"] == 2000


async def test_mnb_failure_keeps_invoices_whose_rate_is_already_stored(setup):
    db, invoices = setup
    await add(invoices, "OLD", fields(invoice_number="OLD", currency="EUR", performance_date=date(2026, 1, 12)))
    source = FixedSource({"EUR": (1, "400")})
    report_service = make_report(db, invoices, source)
    await report_service.build()
    await add(invoices, "NEW", fields(invoice_number="NEW", currency="EUR", performance_date=date(2026, 5, 12)))
    source.fail = {"EUR"}

    report = await snapshot(report_service)

    assert (row(report, "OLD")["amount"], row(report, "OLD")["problem"]) == (508000, None)
    assert (row(report, "NEW")["amount"], row(report, "NEW")["problem"]) == (None, "mnb_unavailable")


async def test_currency_unknown_to_mnb_is_reported_not_guessed(setup):
    db, invoices = setup
    await add(invoices, "X", fields(invoice_number="X", currency="XYZ"))
    report = await snapshot(make_report(db, invoices, FixedSource({})))
    assert row(report, "X")["problem"] == "mnb_no_rate"
    assert report["months"] == []


async def test_future_performance_invoice_gets_no_rate_and_is_left_out_of_the_report(setup):
    db, invoices = setup
    flag = [{"code": "future_performance", "field": "performance_date", "severity": "info"}]
    await add(
        invoices,
        "F",
        fields(invoice_number="F", currency="EUR", performance_date=date(2027, 8, 1), due_date=date(2027, 8, 31)),
        issues=flag,
    )
    await add(invoices, "H", fields(invoice_number="H", performance_date=date(2026, 7, 1)), issues=flag)
    await add(invoices, "P", fields(invoice_number="P"))
    source = FixedSource({"EUR": (1, "400")})

    report = await snapshot(make_report(db, invoices, source))

    for number in "FH":
        item = row(report, number)
        assert (item["amount"], item["rate"], item["problem"]) == (None, None, "future_performance")
        assert (item["future"], item["excluded"]) == (True, "future_performance")
    assert (row(report, "P")["future"], row(report, "P")["excluded"]) == (False, None)
    assert source.calls == []
    assert report["months"] == [{"month": "2026-02", "incoming": 0, "outgoing": 1270}]
    assert report["coverage"]["excluded"]["future_performance"] == 2
    assert report["groups"]["future"] == 2
    assert (report["problems"], report["problem_count"]) == ([], 0)


async def test_storno_is_summed_with_its_negative_amount(setup):
    db, invoices = setup
    await add(invoices, "A", fields(invoice_number="A", gross_total=Decimal("1270")))
    await add(invoices, "S", fields(invoice_number="S", gross_total=Decimal("-1270"), net_total=None, vat_total=None))

    report = await snapshot(make_report(db, invoices, FixedSource({})))

    assert (row(report, "S")["storno"], row(report, "S")["amount"], row(report, "S")["excluded"]) == (True, -1270, None)
    assert report["months"] == [{"month": "2026-02", "incoming": 0, "outgoing": 0}]
    assert report["total_outgoing"] == 0
    assert (report["coverage"]["included"], report["coverage"]["storno"]) == (2, 1)
    assert report["groups"]["storno"] == 1


async def test_invoice_needing_review_is_listed_but_left_out_of_chart_and_summary(setup):
    db, invoices = setup
    await add(invoices, "OK", fields(invoice_number="OK", gross_total=Decimal("1000")))
    await add(invoices, "R", fields(invoice_number="R", gross_total=Decimal("5000")), status="needs_review")

    report = await snapshot(make_report(db, invoices, FixedSource({})))

    assert (row(report, "R")["amount"], row(report, "R")["excluded"], row(report, "R")["problem"]) == (
        5000,
        "needs_review",
        None,
    )
    assert report["months"][0]["outgoing"] == 1000
    assert report["total_outgoing"] == 1000
    assert report["coverage"] == {
        "included": 1,
        "storno": 0,
        "excluded": {"needs_review": 1, "future_performance": 0, "no_rate": 0, "no_direction": 0},
    }
    assert report["groups"] == {"all": 2, "storno": 0, "future": 0, "needs_review": 1}


async def test_invoice_without_direction_is_listed_but_not_summed(setup):
    db, invoices = setup
    stranger = fields(invoice_number="S", seller_name="A Kft.", seller_tax_id="1", buyer_name="B Kft.", buyer_tax_id="2")
    await add(invoices, "S", stranger)
    report = await snapshot(make_report(db, invoices, FixedSource({})))
    assert row(report, "S")["problem"] == "no_direction"
    assert report["months"] == [] and report["partners"] == []


async def test_partners_are_merged_by_tax_number_despite_name_and_format_differences(setup):
    db, invoices = setup
    await add(invoices, "A", fields(invoice_number="A", buyer_name="Partner Kft.", buyer_tax_id="12345678-2-42"))
    await add(invoices, "B", fields(invoice_number="B", buyer_name="PARTNER Kft", buyer_tax_id="HU12345678"))
    await add(invoices, "C", incoming(invoice_number="C", gross_total=Decimal("400")))
    await add(
        invoices,
        "D",
        fields(invoice_number="D", buyer_name="Más Zrt.", buyer_tax_id="87654321-2-13", gross_total=Decimal("100")),
    )

    report = await snapshot(make_report(db, invoices, FixedSource({})))

    partners = {p["tax_id"]: p for p in report["partners"]}
    assert len(report["partners"]) == 2
    merged = next(p for p in report["partners"] if p["incoming"] == 400)
    assert (merged["outgoing"], merged["count"]) == (2540, 3)
    assert report["total_outgoing"] == 2640 and report["total_incoming"] == 400
    assert len(partners) == 2


async def test_empty_report(setup):
    db, invoices = setup
    report = await snapshot(make_report(db, invoices, FixedSource({})))
    assert report["months"] == [] and report["partners"] == [] and report["invoices"] == []
    assert (report["total_incoming"], report["total_outgoing"]) == (0, 0)


async def test_conversion_is_stored_and_not_repeated(setup):
    db, invoices = setup
    await add(invoices, "A", fields(invoice_number="A", currency="EUR"))
    service = make_report(db, invoices, FixedSource({"EUR": (1, "400")}))

    assert await service.ensure_converted() == 1
    assert await service.ensure_converted() == 0

    (record,) = await invoices.list_all()
    assert (record.conversion.amount, record.conversion.quote.rate) == (508000, Decimal("400"))


async def test_resaved_invoice_is_converted_again(setup):
    db, invoices = setup
    await add(invoices, "A", fields(invoice_number="A", gross_total=Decimal("1000")))
    service = make_report(db, invoices, FixedSource({}))
    await service.build()

    await add(invoices, "A", fields(invoice_number="A", gross_total=Decimal("2000")))
    report = await service.build()

    assert report["total_outgoing"] == 2000


async def test_removed_future_flag_brings_the_invoice_into_the_report(setup):
    db, invoices = setup
    flag = [{"code": "future_performance", "field": "performance_date", "severity": "info"}]
    await add(invoices, "F", fields(invoice_number="F"), issues=flag)
    service = make_report(db, invoices, FixedSource({}))
    assert (await service.build())["total_outgoing"] == 0

    assert await invoices.remove_issue("hash-F", "future_performance")
    report = await service.build()

    assert report["total_outgoing"] == 1270
    assert report["coverage"]["excluded"]["future_performance"] == 0


async def test_conversion_of_a_meanwhile_changed_invoice_is_not_stored(setup):
    db, invoices = setup
    await add(invoices, "A", fields(invoice_number="A", gross_total=Decimal("1000")))
    (stale,) = await invoices.list_all()
    await add(invoices, "A", fields(invoice_number="A", gross_total=Decimal("2000")))
    service = make_report(db, invoices, FixedSource({}))

    conversion = await service._convert(stale, {})
    assert await invoices.store_conversions([(stale, conversion)]) == 0

    assert (await service.build())["total_outgoing"] == 2000


async def test_changed_report_currency_converts_again(setup):
    db, invoices = setup
    await add(invoices, "A", fields(invoice_number="A", gross_total=Decimal("4000")))
    await make_report(db, invoices, FixedSource({})).build()
    rates = RateService(RateRepository(db), FixedSource({"HUF": (1, "1")}), today=lambda: TODAY)
    eur_service = ReportService(invoices, rates, "EUR")

    assert await eur_service.ensure_converted() == 1
    (record,) = await invoices.list_all()
    assert record.conversion.currency == "EUR"


async def test_invoice_page_filters_by_group_and_pages(setup):
    db, invoices = setup
    for day in range(1, 6):
        number = f"N{day}"
        await add(invoices, number, fields(invoice_number=number, issue_date=date(2026, 1, day)))
    await add(invoices, "S", fields(invoice_number="S", gross_total=Decimal("-1270")))
    await add(invoices, "R", fields(invoice_number="R"), status="needs_review")
    service = make_report(db, invoices, FixedSource({}))

    page = await service.invoice_page(InvoiceGroup.ALL, 2, 2)
    assert [item["invoice_number"] for item in page["invoices"]] == ["N3", "N4"]
    assert (page["total"], page["offset"], page["limit"]) == (7, 2, 2)
    assert page["groups"] == {"all": 7, "storno": 1, "future": 0, "needs_review": 1}
    assert page["counts"] == {"ok": 6, "needs_review": 1}

    storno = await service.invoice_page(InvoiceGroup.STORNO, 0, 50)
    assert [item["invoice_number"] for item in storno["invoices"]] == ["S"]
    assert storno["total"] == 1


async def test_report_lists_only_problem_invoices_with_their_count(setup):
    db, invoices = setup
    await add(invoices, "E", fields(invoice_number="E", currency="EUR"))
    await add(invoices, "H", fields(invoice_number="H"))

    report = await make_report(db, invoices, FixedSource({}, fail={"EUR"})).build()

    assert [item["invoice_number"] for item in report["problems"]] == ["E"]
    assert report["problem_count"] == 1
    assert "invoices" not in report


def test_api_report_is_served(client):
    response = client.get("/api/report")
    assert response.status_code == 200
    assert (response.json()["problems"], response.json()["problem_count"]) == ([], 0)


def test_static_assets_are_versioned_so_browsers_do_not_serve_stale_scripts(client):
    html = client.get("/").text
    assert "/static/app.js?v=" in html and "/static/style.css?v=" in html
    assert client.get("/static/app.js?v=1").status_code == 200


def test_index_page_serves_vendored_chart_library_and_report_sections(client):
    html = client.get("/").text
    assert 'id="cashflow-chart"' in html and 'id="partners-table"' in html
    assert html.count("data-coverage") == 2
    assert html.count("data-provisional") == 3
    assert all(f'data-group="{group}"' in html for group in ("all", "storno", "future", "needs_review"))
    script = client.get("/static/vendor/chart.umd.js")
    assert script.status_code == 200 and "Chart.js" in script.text[:400]
    assert "cdn" not in html.lower()


def test_month_range_is_continuous_across_years():
    assert month_range("2025-11", "2026-02") == ["2025-11", "2025-12", "2026-01", "2026-02"]
    assert month_range("2026-03", "2026-03") == ["2026-03"]


def test_partner_key_ignores_country_prefix_and_group_digits():
    keys = {
        partner_key("12345678-2-42"),
        partner_key("HU12345678"),
        partner_key("12345678"),
        partner_key(" hu 12345678 "),
    }
    assert keys == {"12345678"}
    assert partner_key("DE123456789") == "DE123456789"
    assert partner_key("-") is None
