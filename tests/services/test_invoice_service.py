import asyncio
from dataclasses import asdict
from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy.exc import IntegrityError

from app.core.errors import ProcessingError
from app.db.database import Database
from app.db.tables import InvoiceRow
from app.extraction.company import Company
from app.extraction.models import InvoiceFields
from app.repositories.invoice_repository import Classification, Direction, InvoiceRepository, identity_key
from app.services.invoice_service import InvoiceAnalyzer, classify

COMPANY = Company("Nordwind Solutions Kft.", "26481937-2-43", "HU26481937")
# A dokumentum szövege: a validáció ebben keresi a saját cég nevét.
TEXT = "Nordwind Solutions Kft."


def fields(**overrides) -> InvoiceFields:
    base = dict(
        invoice_number="X-1",
        seller_name="Nordwind Solutions Kft.",
        seller_tax_id="26481937-2-43",
        buyer_name="Partner Kft.",
        buyer_tax_id="12345678-2-42",
        issue_date=date(2026, 1, 12),
        performance_date=date(2026, 1, 11),
        due_date=date(2026, 2, 11),
        currency="HUF",
        net_total=Decimal("1000.00"),
        vat_total=Decimal("270.00"),
        gross_total=Decimal("1270.00"),
    )
    return InvoiceFields(**{**base, **overrides})


def test_company_as_seller_is_outgoing_with_buyer_as_partner():
    result = classify(fields(), COMPANY)
    assert result == Classification(Direction.OUTGOING, "Partner Kft.", "12345678-2-42")


def test_company_as_buyer_is_incoming_with_seller_as_partner():
    swapped = fields(
        seller_name="Partner Kft.",
        seller_tax_id="12345678-2-42",
        buyer_name="NORDWIND SOLUTIONS KFT.",
        buyer_tax_id="HU26481937",
    )
    assert classify(swapped, COMPANY) == Classification(Direction.INCOMING, "Partner Kft.", "12345678-2-42")


def test_unrelated_or_ambiguous_parties_have_no_direction():
    unrelated = fields(seller_name="A Kft.", seller_tax_id="1", buyer_name="B Kft.", buyer_tax_id="2")
    both = fields(buyer_name="Nordwind Solutions Kft.", buyer_tax_id="26481937-2-43")
    assert classify(unrelated, COMPANY).direction is None
    assert classify(both, COMPANY).direction is None


@pytest.fixture
async def invoices(tmp_path):
    db = Database(tmp_path / "t.db")
    await db.init()
    return InvoiceRepository(db)


async def test_repository_round_trips_values_exactly_and_upserts_by_content(invoices):
    await invoices.save("h1", "a.pdf", fields(), classify(fields(), COMPANY), "ok", [])
    await invoices.save("h1", "b.pdf", fields(invoice_number="X-2"), classify(fields(), COMPANY), "needs_review", [])

    (record,) = await invoices.list_all()
    assert record.fields.invoice_number == "X-2"
    assert record.fields.gross_total == Decimal("1270.00")
    assert record.fields.issue_date == date(2026, 1, 12)
    assert record.direction is Direction.OUTGOING
    assert record.status == "needs_review"
    assert record.filename == "b.pdf"
    assert await invoices.exists("h1") is True
    assert await invoices.exists("masik") is False


async def test_same_invoice_from_another_file_is_a_duplicate_and_stored_once(invoices):
    await InvoiceAnalyzer(_Fixed(invoice_number="NW-2026-0001"), invoices, COMPANY).analyze("h1", "eredeti.pdf", TEXT)

    reissued = _Fixed(invoice_number="nw 2026/0001", seller_tax_id="26481937-2-43 ")
    with pytest.raises(ProcessingError) as error:
        await InvoiceAnalyzer(reissued, invoices, COMPANY).analyze("h2", "ujra-exportalt.pdf", TEXT)

    assert error.value.code == "duplicate_invoice"
    assert "eredeti.pdf" in error.value.message
    assert len(await invoices.list_all()) == 1


async def test_different_invoice_number_or_seller_is_not_a_duplicate(invoices):
    await InvoiceAnalyzer(_Fixed(), invoices, COMPANY).analyze("h1", "a.pdf", TEXT)
    await InvoiceAnalyzer(_Fixed(invoice_number="X-2"), invoices, COMPANY).analyze("h2", "b.pdf", TEXT)
    other_seller = _Fixed(seller_name="Partner Kft.", seller_tax_id="12345678-2-42", buyer_name=COMPANY.name, buyer_tax_id=COMPANY.tax_number)
    await InvoiceAnalyzer(other_seller, invoices, COMPANY).analyze("h3", "c.pdf", TEXT)
    assert len(await invoices.list_all()) == 3


async def test_reprocessing_the_same_content_is_not_a_duplicate(invoices):
    await invoices.save("h1", "a.pdf", fields(), classify(fields(), COMPANY), "ok", [])
    await invoices.save("h1", "a.pdf", fields(), classify(fields(), COMPANY), "ok", [])
    assert len(await invoices.list_all()) == 1


async def test_parallel_saves_of_one_invoice_store_it_once(invoices):
    results = await asyncio.gather(
        *(invoices.save(f"h{i}", f"{i}.pdf", fields(), classify(fields(), COMPANY), "ok", []) for i in range(5)),
        return_exceptions=True,
    )
    assert sum(isinstance(result, ProcessingError) for result in results) == 4
    assert len(await invoices.list_all()) == 1


async def test_database_enforces_unique_identity_key_even_without_the_application_check(invoices):
    await invoices.save("h1", "a.pdf", fields(), classify(fields(), COMPANY), "ok", [])
    # Ugyanaz a számla másik fájlból, közvetlenül az adatbázisba írva (a `save` ellenőrzését megkerülve).
    copy = InvoiceRow(
        content_hash="h2", filename="b.pdf", status="ok", identity_key=identity_key(fields()), **asdict(fields())
    )
    with pytest.raises(IntegrityError):
        async with invoices._db.transaction() as session:
            session.add(copy)


async def test_unique_key_conflict_during_save_is_reported_as_duplicate(invoices, monkeypatch):
    await invoices.save("h1", "a.pdf", fields(), classify(fields(), COMPANY), "ok", [])
    # Az alkalmazásszintű ellenőrzés kihagyása: csak az adatbázis egyedi kulcsa véd.
    original = InvoiceRepository._other_with_key
    calls = []

    async def blind_first_time(conn, key, content_hash):
        calls.append(1)
        return None if len(calls) == 1 else await original(conn, key, content_hash)

    monkeypatch.setattr(InvoiceRepository, "_other_with_key", staticmethod(blind_first_time))
    with pytest.raises(ProcessingError) as error:
        await invoices.save("h2", "b.pdf", fields(), classify(fields(), COMPANY), "ok", [])
    assert error.value.code == "duplicate_invoice"
    assert len(await invoices.list_all()) == 1


async def test_database_refuses_incomplete_invoice(invoices):
    with pytest.raises(Exception):
        await invoices.save("h1", "a.pdf", fields(due_date=None), classify(fields(), COMPANY), "ok", [])
    assert await invoices.list_all() == []


async def test_analyzer_stores_validated_and_classified_invoice(invoices):
    await InvoiceAnalyzer(_Fixed(), invoices, COMPANY).analyze("h1", "szamla.pdf", TEXT)

    (record,) = await invoices.list_all()
    assert record.fields.invoice_number == "X-1"
    assert record.fields.gross_total == Decimal("1270.00")
    assert record.direction is Direction.OUTGOING
    assert record.partner_name == "Partner Kft."
    assert record.status == "ok"


async def test_analyzer_rejects_text_without_invoice_data(invoices):
    class Empty:
        async def extract(self, text: str) -> InvoiceFields:
            return InvoiceFields(**dict.fromkeys(InvoiceFields.names()))

    with pytest.raises(ProcessingError) as error:
        await InvoiceAnalyzer(Empty(), invoices, COMPANY).analyze("h1", "level.pdf", "Ez csak egy levél.")

    assert error.value.code == "no_invoice_found"
    assert await invoices.list_all() == []


async def test_analyzer_names_the_missing_fields_and_stores_nothing(invoices):
    with pytest.raises(ProcessingError) as error:
        await InvoiceAnalyzer(_Fixed(due_date=None), invoices, COMPANY).analyze("h1", "hianyos.pdf", TEXT)

    assert error.value.code == "incomplete_data"
    assert error.value.fields == ("due_date",)
    assert await invoices.list_all() == []


async def test_analyzer_treats_an_empty_value_as_missing(invoices):
    with pytest.raises(ProcessingError) as error:
        await InvoiceAnalyzer(_Fixed(seller_tax_id=""), invoices, COMPANY).analyze("h1", "x.pdf", "Nordwind Solutions Kft.")

    assert error.value.code == "incomplete_data"
    assert error.value.fields == ("seller_tax_id",)
    assert await invoices.list_all() == []


class _Fixed:
    def __init__(self, **overrides) -> None:
        self._fields = fields(**overrides)

    async def extract(self, text: str) -> InvoiceFields:
        return self._fields


class _Quotes:
    def __init__(self, answer: bool | None) -> None:
        self.answer, self.asked = answer, []

    async def is_quoted(self, currency: str) -> bool | None:
        self.asked.append(currency)
        return self.answer


async def _analyze_with_currency(invoices, check, currency="AFN"):
    analyzer = InvoiceAnalyzer(_Fixed(currency=currency), invoices, COMPANY, check, "HUF")
    await analyzer.analyze("h1", "x.pdf", "Nordwind Solutions Kft.")
    return (await invoices.list_all())[0]


async def test_currency_not_quoted_by_mnb_needs_review_but_is_stored(invoices):
    record = await _analyze_with_currency(invoices, _Quotes(False))
    assert record.status == "needs_review"
    assert ("currency_not_quoted", "currency") in {(i["code"], i["field"]) for i in record.issues}


async def test_quoted_or_unknown_support_does_not_raise_a_warning(invoices):
    for answer in (True, None):
        await invoices.delete_all()
        record = await _analyze_with_currency(invoices, _Quotes(answer))
        assert record.status == "ok"


async def test_report_currency_is_not_checked_against_the_mnb_list(invoices):
    check = _Quotes(False)
    record = await _analyze_with_currency(invoices, check, currency="HUF")
    assert record.status == "ok"
    assert check.asked == []


async def test_invoice_without_net_and_vat_is_stored_as_clean(invoices):
    extractor = _Fixed(net_total=None, vat_total=None)

    await InvoiceAnalyzer(extractor, invoices, COMPANY).analyze("h1", "x.pdf", "Nordwind Solutions Kft.")

    (record,) = await invoices.list_all()
    assert (record.fields.net_total, record.fields.vat_total) == (None, None)
    assert record.fields.gross_total == Decimal("1270.00")
    assert record.status == "ok"


async def test_invoice_without_performance_date_is_rejected_and_not_stored(invoices):
    extractor = _Fixed(performance_date=None)

    with pytest.raises(ProcessingError) as error:
        await InvoiceAnalyzer(extractor, invoices, COMPANY).analyze("h1", "x.pdf", "Nordwind Solutions Kft.")

    assert error.value.code == "incomplete_data"
    assert error.value.fields == ("performance_date",)
    assert await invoices.list_all() == []


_UNRECOGNISED_US = dict(seller_name="Nordwind Solutions Ltd.", seller_tax_id="HU 26481937 / 2-43")


def _issue_codes(record) -> set[str]:
    return {issue["code"] for issue in record.issues}


async def test_invoice_without_direction_is_stored_for_review(invoices):
    await InvoiceAnalyzer(_Fixed(**_UNRECOGNISED_US), invoices, COMPANY).analyze("h1", "x.pdf", "Nordwind")

    (record,) = await invoices.list_all()
    assert record.direction is None
    assert record.status == "needs_review"
    assert "no_direction" in _issue_codes(record)


async def test_missing_required_field_is_still_rejected_next_to_missing_optional_ones(invoices):
    extractor = _Fixed(net_total=None, vat_total=None, due_date=None)

    with pytest.raises(ProcessingError) as error:
        await InvoiceAnalyzer(extractor, invoices, COMPANY).analyze("h1", "x.pdf", "Nordwind Solutions Kft.")

    assert error.value.code == "incomplete_data"
    assert error.value.fields == ("due_date",)


async def test_analyzer_rejects_invalid_values_such_as_unknown_currency(invoices):
    class Fixed:
        async def extract(self, text: str) -> InvoiceFields:
            return fields(currency="EURO")

    with pytest.raises(ProcessingError) as error:
        await InvoiceAnalyzer(Fixed(), invoices, COMPANY).analyze("h1", "x.pdf", "Nordwind Solutions Kft.")

    assert error.value.code == "invalid_data"
    assert error.value.fields == ("currency",)
    assert await invoices.list_all() == []


async def test_database_refuses_invoice_without_performance_date(invoices):
    with pytest.raises(Exception):
        await invoices.save("h1", "a.pdf", fields(performance_date=None), classify(fields(), COMPANY), "ok", [])
    assert await invoices.list_all() == []


def test_api_lists_stored_invoice(client, app):
    client.portal.call(
        app.state.invoice_repository.save, "h1", "szamla.pdf", fields(), classify(fields(), COMPANY), "ok", []
    )

    body = client.get("/api/invoices").json()

    (invoice,) = body["invoices"]
    assert invoice["invoice_number"] == "X-1"
    assert invoice["seller"]["tax_id"] == "26481937-2-43"
    assert invoice["buyer"]["name"] == "Partner Kft."
    assert invoice["gross_total"] == "1270.00"
    assert invoice["currency"] == "HUF"
    assert invoice["direction"] == "outgoing"
    assert invoice["partner"]["name"] == "Partner Kft."
    assert invoice["status"] == "ok"
    assert body["counts"] == {"ok": 1, "needs_review": 0}


def test_api_is_empty_without_invoices(client):
    assert client.get("/api/invoices").json() == {
        "invoices": [],
        "counts": {"ok": 0, "needs_review": 0},
    }


def test_index_page_contains_invoice_table(client):
    html = client.get("/").text
    assert 'id="inv-table"' in html
    assert "Kinyert számlák" in html
