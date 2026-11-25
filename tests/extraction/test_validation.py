from dataclasses import replace
from datetime import date
from decimal import Decimal

from app.extraction.company import Company
from app.extraction.models import InvoiceFields
from app.extraction.validation import InvoiceStatus, Severity, validate_invoice

TODAY = date(2026, 6, 1)

GOOD = InvoiceFields(
    invoice_number="NW-2026-0042",
    seller_name="Alfa Kft.",
    seller_tax_id="11111111-2-11",
    buyer_name="Nordwind Solutions Kft.",
    buyer_tax_id="26481937-2-43",
    issue_date=date(2026, 3, 1),
    performance_date=date(2026, 3, 1),
    due_date=date(2026, 3, 31),
    currency="EUR",
    net_total=Decimal("1000.00"),
    vat_total=Decimal("270.00"),
    gross_total=Decimal("1270.00"),
)


def codes(result):
    return {(issue.code, issue.field) for issue in result.issues}


def validate(fields, **kwargs):
    return validate_invoice(fields, today=TODAY, **kwargs)


def test_complete_invoice_is_ok():
    result = validate(GOOD)
    assert result.status is InvoiceStatus.OK
    assert result.issues == ()


def test_each_required_field_is_an_error():
    for name in ("invoice_number", "issue_date", "performance_date", "due_date", "currency", "gross_total"):
        result = validate(replace(GOOD, **{name: None}))
        assert result.status is InvoiceStatus.INVALID
        assert ("missing_field", name) in codes(result)


def test_party_needs_name_or_tax_id():
    only_tax = replace(GOOD, seller_name=None)
    assert validate(only_tax).status is InvoiceStatus.OK
    nothing = replace(GOOD, seller_name=None, seller_tax_id=None)
    assert ("missing_field", "seller") in codes(validate(nothing))
    nothing = replace(GOOD, buyer_name=None, buyer_tax_id=None)
    assert ("missing_field", "buyer") in codes(validate(nothing))


def test_unknown_currency_is_an_error():
    result = validate(replace(GOOD, currency="XYZ"))
    assert result.status is InvoiceStatus.INVALID
    assert ("unknown_currency", "currency") in codes(result)


def test_missing_performance_date_is_an_error():
    result = validate(replace(GOOD, performance_date=None))
    assert result.status is InvoiceStatus.INVALID
    assert codes(result) == {("missing_field", "performance_date")}


def test_totals_mismatch():
    result = validate(replace(GOOD, vat_total=Decimal("100.00")))
    assert result.status is InvoiceStatus.NEEDS_REVIEW
    assert ("totals_mismatch", "gross_total") in codes(result)


def test_rounding_difference_is_tolerated():
    assert validate(replace(GOOD, gross_total=Decimal("1270.01"))).status is InvoiceStatus.OK


def test_zero_decimal_currency_scale_is_fine():
    fields = replace(
        GOOD, currency="JPY", net_total=Decimal("100000"), vat_total=Decimal("10000"), gross_total=Decimal("110000")
    )
    assert validate(fields).status is InvoiceStatus.OK


def vat_text(rates: str) -> str:
    return f"Nettó összesen 1 000,00 EUR\nÁFA összesen {rates} 270,00 EUR\nFizetendő 1 270,00 EUR"


def test_vat_matching_the_stated_rate_is_ok():
    assert validate(GOOD, source_text=vat_text("(27%)")).status is InvoiceStatus.OK


def test_vat_not_matching_the_stated_rate_needs_review():
    fields = replace(GOOD, vat_total=Decimal("190.00"), gross_total=Decimal("1190.00"))
    result = validate(fields, source_text=vat_text("(27%)"))
    assert result.status is InvoiceStatus.NEEDS_REVIEW
    assert codes(result) == {("vat_rate_mismatch", "vat_total")}


def test_vat_rate_with_decimal_comma_and_rounding_is_tolerated():
    fields = replace(GOOD, net_total=Decimal("12345"), vat_total=Decimal("3333"), gross_total=Decimal("15678"))
    assert validate(fields, source_text="ÁFA (27,0 %) 3 333 Ft").status is InvoiceStatus.OK


def test_vat_rate_in_a_separate_line_after_the_vat_header_is_used():
    fields = replace(GOOD, vat_total=Decimal("190.00"), gross_total=Decimal("1190.00"))
    result = validate(fields, source_text="Nettó\nÁFA\n27%\n190,00")
    assert codes(result) == {("vat_rate_mismatch", "vat_total")}


def test_mixed_rates_accept_any_vat_up_to_the_highest_rate():
    text = "ÁFA 27% 270,00\nÁFA 5% 10,00"
    assert validate(replace(GOOD, vat_total=Decimal("150.00"), gross_total=Decimal("1150.00")), source_text=text).status is InvoiceStatus.OK
    too_much = replace(GOOD, vat_total=Decimal("400.00"), gross_total=Decimal("1400.00"))
    assert ("vat_rate_mismatch", "vat_total") in codes(validate(too_much, source_text=text))


def test_zero_vat_with_a_zero_rate_is_ok():
    fields = replace(GOOD, vat_total=Decimal("0.00"), gross_total=Decimal("1000.00"))
    assert validate(fields, source_text="ÁFA 0% 0,00").status is InvoiceStatus.OK


def test_vat_rate_check_ignores_percentages_outside_vat_lines():
    text = "Kedvezmény 5%\nÁFA összesen 270,00 EUR"
    assert validate(GOOD, source_text=text).status is InvoiceStatus.OK


def test_vat_rate_check_skipped_without_a_stated_rate_or_text():
    fields = replace(GOOD, vat_total=Decimal("190.00"), gross_total=Decimal("1190.00"))
    assert validate(fields, source_text="ÁFA összesen 190,00").status is InvoiceStatus.OK
    assert validate(fields).status is InvoiceStatus.OK


def test_totals_check_skipped_when_a_part_is_missing():
    assert validate(replace(GOOD, vat_total=None)).status is InvoiceStatus.OK
    assert validate(replace(GOOD, net_total=None)).status is InvoiceStatus.OK


def test_net_exceeds_gross():
    result = validate(replace(GOOD, net_total=Decimal("2000.00"), vat_total=None))
    assert ("net_exceeds_gross", "net_total") in codes(result)


def test_non_positive_total_needs_review():
    fields = replace(
        GOOD, net_total=Decimal("-1000.00"), vat_total=Decimal("-270.00"), gross_total=Decimal("-1270.00")
    )
    result = validate(fields)
    assert result.status is InvoiceStatus.NEEDS_REVIEW
    assert codes(result) == {("non_positive_total", "gross_total")}


def test_date_consistency():
    result = validate(replace(GOOD, due_date=date(2026, 2, 1)))
    assert ("due_before_issue", "due_date") in codes(result)
    result = validate(replace(GOOD, performance_date=date(2026, 4, 15)))
    assert ("performance_after_due", "performance_date") in codes(result)


def test_date_out_of_range():
    assert ("date_out_of_range", "issue_date") in codes(validate(replace(GOOD, issue_date=date(1999, 12, 31))))
    far_future = replace(GOOD, due_date=date(2040, 1, 1))
    assert ("date_out_of_range", "due_date") in codes(validate(far_future))


def test_same_party():
    same_tax = replace(GOOD, buyer_tax_id="11111111 2 11")
    assert ("same_party", None) in codes(validate(same_tax))
    same_name = replace(GOOD, buyer_name="ALFA KFT")
    assert ("same_party", None) in codes(validate(same_name))


def test_company_must_appear_in_source_text():
    company = Company(name="Nordwind Solutions Kft.", tax_number="26481937-2-43")
    assert validate(GOOD, company=company, source_text="... 26481937-2-43 ...").status is InvoiceStatus.OK
    result = validate(GOOD, company=company, source_text="semmi köze")
    assert ("company_not_found", None) in codes(result)
    assert validate(GOOD, company=company).status is InvoiceStatus.OK


def test_errors_take_precedence_over_warnings():
    result = validate(replace(GOOD, invoice_number=None, vat_total=Decimal("100.00")))
    assert result.status is InvoiceStatus.INVALID
    assert {issue.severity for issue in result.issues} == {Severity.ERROR, Severity.WARNING}


def test_filled_and_issue_dict():
    assert InvoiceFields().filled() == {}
    assert GOOD.filled()["currency"] == "EUR"
    issue = validate(replace(GOOD, invoice_number=None)).issues[0]
    assert issue.as_dict() == {"code": "missing_field", "field": "invoice_number", "severity": "error"}
