"""Kinyert mezők validációja: kötelező mezők, számtani és dátumkonzisztencia."""

import re
import unicodedata
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal
from enum import StrEnum

from app.extraction.company import Company
from app.extraction.labels import Labels, make_lines
from app.extraction.models import InvoiceFields
from app.extraction.normalize import is_known_currency, normalize_key

_MIN_YEAR = 2000
_MAX_YEARS_AHEAD = 5
_BASE_TOLERANCE = Decimal("0.02")
_RELATIVE_TOLERANCE = Decimal("0.0005")
_RATE_ROUNDING = Decimal("0.5")  # tételenkénti és egész pénznemre kerekítés
_PERCENT = re.compile(r"(?<![\d.,])(\d{1,2}(?:[.,]\d{1,2})?)\s?%")


class InvoiceStatus(StrEnum):
    OK = "ok"
    NEEDS_REVIEW = "needs_review"
    INVALID = "invalid"


class Severity(StrEnum):
    ERROR = "error"
    WARNING = "warning"


@dataclass(frozen=True, slots=True)
class Issue:
    code: str
    field: str | None
    severity: Severity

    def as_dict(self) -> dict[str, str | None]:
        return {"code": self.code, "field": self.field, "severity": self.severity.value}


@dataclass(frozen=True, slots=True)
class ValidationResult:
    status: InvoiceStatus
    issues: tuple[Issue, ...]


def validate_invoice(
    fields: InvoiceFields,
    *,
    company: Company | None = None,
    source_text: str | None = None,
    today: date | None = None,
) -> ValidationResult:
    """Hibák (hiányzó kötelező mező) → INVALID; gyanús értékek → NEEDS_REVIEW; különben OK."""
    issues: list[Issue] = []

    def error(code: str, field: str | None = None) -> None:
        issues.append(Issue(code, field, Severity.ERROR))

    def warning(code: str, field: str | None = None) -> None:
        issues.append(Issue(code, field, Severity.WARNING))

    _check_required(fields, error)
    if fields.currency and not is_known_currency(fields.currency):
        error("unknown_currency", "currency")

    _check_totals(fields, warning)
    if source_text is not None:
        _check_vat_rate(fields, source_text, warning)
    _check_dates(fields, warning, today or date.today())

    if _same_party(fields):
        warning("same_party")
    if company is not None and source_text is not None and not company.appears_in(source_text):
        warning("company_not_found")

    if any(issue.severity is Severity.ERROR for issue in issues):
        status = InvoiceStatus.INVALID
    elif issues:
        status = InvoiceStatus.NEEDS_REVIEW
    else:
        status = InvoiceStatus.OK
    return ValidationResult(status, tuple(issues))


def _check_required(fields: InvoiceFields, error) -> None:
    if not fields.invoice_number:
        error("missing_field", "invoice_number")
    if not (fields.seller_name or fields.seller_tax_id):
        error("missing_field", "seller")
    if not (fields.buyer_name or fields.buyer_tax_id):
        error("missing_field", "buyer")
    for name in ("issue_date", "performance_date", "due_date", "currency", "gross_total"):
        if getattr(fields, name) is None:
            error("missing_field", name)


def _check_totals(fields: InvoiceFields, warning) -> None:
    net, vat, gross = fields.net_total, fields.vat_total, fields.gross_total
    if gross is None:
        return
    if gross <= 0:
        warning("non_positive_total", "gross_total")
    if net is not None and gross > 0 and net > gross:
        warning("net_exceeds_gross", "net_total")
    if net is not None and vat is not None:
        tolerance = _BASE_TOLERANCE + abs(gross) * _RELATIVE_TOLERANCE
        if abs(net + vat - gross) > tolerance:
            warning("totals_mismatch", "gross_total")


def _check_vat_rate(fields: InvoiceFields, text: str, warning) -> None:
    """Az ÁFA összege a számlán szereplő ÁFA-kulcs(ok) és a nettó alapján megfelel-e."""
    net, vat = fields.net_total, fields.vat_total
    if net is None or vat is None or net <= 0 or vat < 0:
        return
    rates = _stated_vat_rates(text)
    if not rates:
        return
    tolerance = _RATE_ROUNDING + net * _RELATIVE_TOLERANCE
    highest = net * max(rates) / 100
    # Több kulcsnál a nem említett (pl. adómentes) tételek miatt az alsó határ nulla.
    lowest = net * rates[0] / 100 if len(rates) == 1 else Decimal(0)
    if not lowest - tolerance <= vat <= highest + tolerance:
        warning("vat_rate_mismatch", "vat_total")


def _stated_vat_rates(text: str) -> list[Decimal]:
    """A számla ÁFA-feliratos soraiban (és az ÁFA-fejléc utáni sorban) szereplő százalékok, növekvő sorrendben."""
    labels = Labels.default()
    lines = make_lines(unicodedata.normalize("NFKC", text))
    rates: set[Decimal] = set()
    for index, line in enumerate(lines):
        if not labels.find(line, ["vat_total"]):
            continue
        scanned = [line]
        if not line.has_digit and index + 1 < len(lines):
            scanned.append(lines[index + 1])
        for item in scanned:
            for match in _PERCENT.finditer(item.raw):
                rates.add(Decimal(match.group(1).replace(",", ".")))
    return sorted(rates)


def _check_dates(fields: InvoiceFields, warning, today: date) -> None:
    latest = today + timedelta(days=366 * _MAX_YEARS_AHEAD)
    for name in ("issue_date", "performance_date", "due_date"):
        value = getattr(fields, name)
        if value is not None and not date(_MIN_YEAR, 1, 1) <= value <= latest:
            warning("date_out_of_range", name)
    if fields.issue_date and fields.due_date and fields.due_date < fields.issue_date:
        warning("due_before_issue", "due_date")
    if fields.performance_date and fields.due_date and fields.performance_date > fields.due_date:
        warning("performance_after_due", "performance_date")


def _same_party(fields: InvoiceFields) -> bool:
    seller_tax, buyer_tax = normalize_key(fields.seller_tax_id), normalize_key(fields.buyer_tax_id)
    if seller_tax and seller_tax == buyer_tax:
        return True
    seller_name, buyer_name = normalize_key(fields.seller_name), normalize_key(fields.buyer_name)
    return bool(seller_name) and seller_name == buyer_name
