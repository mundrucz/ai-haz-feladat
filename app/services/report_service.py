"""Kimutatás: számlák forintra váltása, havi cash-flow és cégenkénti összesítés."""

import logging
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import date
from decimal import ROUND_HALF_UP, Decimal

from app.errors import AppError
from app.extraction.normalize import normalize_key
from app.failed_files import SOURCE_REPORT, FailedFileLog
from app.invoices import Direction, InvoiceRecord, InvoiceRepository
from app.mnb import RateQuote
from app.rates import RateService, convert

logger = logging.getLogger(__name__)

PROBLEM_NO_AMOUNT = "missing_amount"
PROBLEM_NO_RATE_DATE = "missing_rate_date"
PROBLEM_NO_DIRECTION = "no_direction"
PROBLEM_NO_DUE_DATE = "missing_due_date"

_HU_PREFIX = "HU"
_HU_TAX_NUMBER_LENGTH = 11
_HU_TAX_BASE_LENGTH = 8


@dataclass(frozen=True, slots=True)
class ConvertedInvoice:
    record: InvoiceRecord
    quote: RateQuote | None
    amount: Decimal | None
    problem: str | None

    @property
    def in_partner_totals(self) -> bool:
        return self.amount is not None and self.record.direction is not None

    @property
    def in_cash_flow(self) -> bool:
        return self.in_partner_totals and self.record.fields.due_date is not None


def partner_key(tax_id: str | None, name: str | None) -> str | None:
    """Ugyanazt a partnert azonosító kulcs: adószám (országelőtag és csoportazonosító nélkül), tartaléknak a név."""
    key = normalize_key(tax_id)
    if key.startswith(_HU_PREFIX) and key[len(_HU_PREFIX) :].isdigit():
        key = key[len(_HU_PREFIX) :]
    if key.isdigit() and len(key) == _HU_TAX_NUMBER_LENGTH:
        key = key[:_HU_TAX_BASE_LENGTH]
    return key or normalize_key(name) or None


def rate_date(record: InvoiceRecord) -> date | None:
    """Az árfolyam napja: a teljesítés dátuma, ennek hiányában a kiállítás dátuma."""
    return record.fields.performance_date or record.fields.issue_date


def month_range(first: str, last: str) -> list[str]:
    year, month = int(first[:4]), int(first[5:])
    end_year, end_month = int(last[:4]), int(last[5:])
    months = []
    while (year, month) <= (end_year, end_month):
        months.append(f"{year:04d}-{month:02d}")
        year, month = (year + 1, 1) if month == 12 else (year, month + 1)
    return months


def monthly_series(items: list[ConvertedInvoice]) -> list[dict]:
    """Havi bejövő/kimenő összeg a fizetési határidő szerint; a hónapok között nincs kihagyás."""
    totals: dict[str, dict[Direction, Decimal]] = defaultdict(lambda: defaultdict(Decimal))
    for item in items:
        if item.in_cash_flow:
            due = item.record.fields.due_date
            totals[f"{due.year:04d}-{due.month:02d}"][item.record.direction] += item.amount
    if not totals:
        return []
    return [
        {
            "month": month,
            "incoming": int(totals[month][Direction.INCOMING]),
            "outgoing": int(totals[month][Direction.OUTGOING]),
        }
        for month in month_range(min(totals), max(totals))
    ]


def partner_summary(items: list[ConvertedInvoice]) -> dict:
    """Cégenkénti bejövő/kimenő összesítés, a végén az összes partner összegével."""
    groups: dict[str, list[ConvertedInvoice]] = defaultdict(list)
    for item in items:
        if item.in_partner_totals:
            key = partner_key(item.record.partner_tax_id, item.record.partner_name)
            groups[key or f"#{item.record.id}"].append(item)
    partners = []
    for group in groups.values():
        incoming = sum((i.amount for i in group if i.record.direction is Direction.INCOMING), Decimal(0))
        outgoing = sum((i.amount for i in group if i.record.direction is Direction.OUTGOING), Decimal(0))
        partners.append(
            {
                "name": _most_common(i.record.partner_name for i in group),
                "tax_id": _most_common(i.record.partner_tax_id for i in group),
                "incoming": int(incoming),
                "outgoing": int(outgoing),
                "count": len(group),
            }
        )
    partners.sort(key=lambda p: (-(p["incoming"] + p["outgoing"]), p["name"] or ""))
    return {
        "partners": partners,
        "total_incoming": sum(p["incoming"] for p in partners),
        "total_outgoing": sum(p["outgoing"] for p in partners),
    }


def _most_common(values) -> str | None:
    counts = Counter(v for v in values if v)
    return counts.most_common(1)[0][0] if counts else None


class ReportService:
    def __init__(
        self,
        repository: InvoiceRepository,
        rates: RateService,
        report_currency: str,
        failure_log: FailedFileLog | None = None,
    ) -> None:
        self._repository = repository
        self._rates = rates
        self._report_currency = report_currency
        self._failure_log = failure_log or FailedFileLog()
        self._logged_problems: dict[int, str] = {}

    @property
    def currency(self) -> str:
        return self._report_currency

    async def convert_all(self) -> list[ConvertedInvoice]:
        records = await self._repository.list_all()
        failures = await self._rates.prepare(self._needs(records))
        items = [await self._convert(record, failures) for record in records]
        self._log_problems(items)
        return items

    def _log_problems(self, items: list[ConvertedInvoice]) -> None:
        """Az összesítésből (részben) kimaradó számlákat egyszer naplózza; a megszűnt vagy megváltozott hibát újra."""
        current = {item.record.id: item.problem for item in items if item.problem}
        for item in items:
            if item.problem and self._logged_problems.get(item.record.id) != item.problem:
                record = item.record
                self._failure_log.record(
                    SOURCE_REPORT,
                    file_name=record.filename,
                    error_code=item.problem,
                    content_hash=record.content_hash,
                    invoice_id=record.id,
                )
        self._logged_problems = current

    def _needs(self, records: list[InvoiceRecord]) -> dict[str, tuple[date, date]]:
        needs: dict[str, tuple[date, date]] = {}
        for record in records:
            day, currency = rate_date(record), record.fields.currency
            if day is None or not currency or record.fields.gross_total is None or currency == self._report_currency:
                continue
            first, last = needs.get(currency, (day, day))
            needs[currency] = (min(first, day), max(last, day))
        return needs

    async def _convert(self, record: InvoiceRecord, failures: dict[str, AppError]) -> ConvertedInvoice:
        fields = record.fields
        if fields.gross_total is None or not fields.currency:
            return ConvertedInvoice(record, None, None, PROBLEM_NO_AMOUNT)
        if fields.currency == self._report_currency:
            amount = fields.gross_total.quantize(Decimal(1), rounding=ROUND_HALF_UP)
            return ConvertedInvoice(record, None, amount, self._side_problem(record))
        day = rate_date(record)
        if day is None:
            return ConvertedInvoice(record, None, None, PROBLEM_NO_RATE_DATE)
        if fields.currency in failures:
            return ConvertedInvoice(record, None, None, failures[fields.currency].code)
        try:
            quote = await self._rates.rate_on(fields.currency, day)
        except AppError as exc:
            return ConvertedInvoice(record, None, None, exc.code)
        return ConvertedInvoice(record, quote, convert(fields.gross_total, quote), self._side_problem(record))

    @staticmethod
    def _side_problem(record: InvoiceRecord) -> str | None:
        if record.direction is None:
            return PROBLEM_NO_DIRECTION
        if record.fields.due_date is None:
            return PROBLEM_NO_DUE_DATE
        return None

    async def build(self) -> dict:
        items = await self.convert_all()
        return {
            "currency": self._report_currency,
            "months": monthly_series(items),
            **partner_summary(items),
            "invoices": [_invoice_row(item) for item in items],
        }


def _invoice_row(item: ConvertedInvoice) -> dict:
    record, fields = item.record, item.record.fields
    return {
        "id": record.id,
        "invoice_number": fields.invoice_number,
        "direction": record.direction.value if record.direction else None,
        "partner": {"name": record.partner_name, "tax_id": record.partner_tax_id},
        "issue_date": fields.issue_date.isoformat() if fields.issue_date else None,
        "performance_date": fields.performance_date.isoformat() if fields.performance_date else None,
        "due_date": fields.due_date.isoformat() if fields.due_date else None,
        "currency": fields.currency,
        "gross_total": str(fields.gross_total) if fields.gross_total is not None else None,
        "rate": str(item.quote.rate) if item.quote else None,
        "rate_unit": item.quote.unit if item.quote else None,
        "rate_date": item.quote.rate_date.isoformat() if item.quote else None,
        "amount": int(item.amount) if item.amount is not None else None,
        "problem": item.problem,
        "status": record.status,
    }
