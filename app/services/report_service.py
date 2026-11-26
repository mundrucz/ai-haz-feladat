"""Kimutatás: a számlák átváltása a kimutatás pénznemére (egyszer, tárolva), havi cash-flow, cégenkénti összesítés
és a lapozott számlalista.

Az átváltás csak az új, a módosult és az árfolyam nélkül maradt számlákat érinti, egy kötegben; az összesítéseket
az adatbázis számolja a tárolt összegekből.
"""

import asyncio
import logging
from collections import Counter, defaultdict
from datetime import date
from decimal import ROUND_HALF_UP, Decimal
from enum import StrEnum

from app.clients.mnb_client import RateQuote
from app.core.errors import AppError
from app.extraction.normalize import normalize_key
from app.extraction.validation import FUTURE_PERFORMANCE, InvoiceStatus
from app.repositories.invoice_repository import (
    Conversion,
    Direction,
    ExclusionCount,
    InvoiceGroup,
    InvoiceRecord,
    InvoiceRepository,
    MonthTotal,
    PartnerTotal,
)
from app.services.failed_file_service import SOURCE_REPORT, FailedFileLog
from app.services.rate_service import RateService, convert

logger = logging.getLogger(__name__)

PROBLEM_NO_DIRECTION = "no_direction"
MAX_PAGE_SIZE = 200
MAX_PROBLEMS = 500
# Ezek a hiányok nem múlnak el magától, így az átváltást nem próbáljuk újra (a jelölés levétele új átváltást kér).
_FINAL_PROBLEMS = (FUTURE_PERFORMANCE,)
# Nem hiba, csak még nem esedékes: a számlalista „jövőbeli” csoportjában látszik, a hibák között nem.
_UNLISTED_PROBLEMS = (FUTURE_PERFORMANCE,)

_HU_PREFIX = "HU"
_HU_TAX_NUMBER_LENGTH = 11
_HU_TAX_BASE_LENGTH = 8


class Exclusion(StrEnum):
    """Miért marad ki egy számla a diagramból és a cégenkénti összesítőből."""

    NEEDS_REVIEW = "needs_review"
    FUTURE_PERFORMANCE = "future_performance"
    NO_RATE = "no_rate"
    NO_DIRECTION = "no_direction"


def exclusion_of(record: InvoiceRecord, amount: Decimal | None) -> Exclusion | None:
    if record.status == InvoiceStatus.NEEDS_REVIEW:
        return Exclusion.NEEDS_REVIEW
    if record.has_issue(FUTURE_PERFORMANCE):
        return Exclusion.FUTURE_PERFORMANCE
    if amount is None:
        return Exclusion.NO_RATE
    if record.direction is None:
        return Exclusion.NO_DIRECTION
    return None


def partner_key(tax_id: str | None) -> str | None:
    """Ugyanazt a partnert azonosító kulcs: a normalizált adószám, országelőtag és csoportazonosító nélkül."""
    key = normalize_key(tax_id)
    if key.startswith(_HU_PREFIX) and key[len(_HU_PREFIX) :].isdigit():
        key = key[len(_HU_PREFIX) :]
    if key.isdigit() and len(key) == _HU_TAX_NUMBER_LENGTH:
        key = key[:_HU_TAX_BASE_LENGTH]
    return key or None


def rate_date(record: InvoiceRecord) -> date | None:
    """Az árfolyam napja: a teljesítés dátuma (nincs tartalék)."""
    return record.fields.performance_date


def month_range(first: str, last: str) -> list[str]:
    year, month = int(first[:4]), int(first[5:])
    end_year, end_month = int(last[:4]), int(last[5:])
    months = []
    while (year, month) <= (end_year, end_month):
        months.append(f"{year:04d}-{month:02d}")
        year, month = (year + 1, 1) if month == 12 else (year, month + 1)
    return months


def monthly_series(totals: list[MonthTotal]) -> list[dict]:
    """Havi bejövő/kimenő összeg a fizetési határidő szerint; a hónapok között nincs kihagyás."""
    by_month: dict[str, dict[Direction, int]] = defaultdict(lambda: defaultdict(int))
    for total in totals:
        by_month[total.month][total.direction] += total.amount
    if not by_month:
        return []
    return [
        {
            "month": month,
            "incoming": by_month[month][Direction.INCOMING],
            "outgoing": by_month[month][Direction.OUTGOING],
        }
        for month in month_range(min(by_month), max(by_month))
    ]


def partner_summary(totals: list[PartnerTotal]) -> dict:
    """Cégenkénti bejövő/kimenő összesítés (az adószám-változatok összevonva), az összes partner összegével."""
    groups: dict[str, list[PartnerTotal]] = defaultdict(list)
    for total in totals:
        groups[partner_key(total.tax_id) or f"#{total.tax_id}|{total.name}"].append(total)
    partners = []
    for group in groups.values():
        partners.append(
            {
                "name": _most_common((t.name, t.count) for t in group),
                "tax_id": _most_common((t.tax_id, t.count) for t in group),
                "incoming": sum(t.amount for t in group if t.direction is Direction.INCOMING),
                "outgoing": sum(t.amount for t in group if t.direction is Direction.OUTGOING),
                "count": sum(t.count for t in group),
            }
        )
    partners.sort(key=lambda p: (-(p["incoming"] + p["outgoing"]), p["name"] or ""))
    return {
        "partners": partners,
        "total_incoming": sum(p["incoming"] for p in partners),
        "total_outgoing": sum(p["outgoing"] for p in partners),
    }


def _most_common(weighted) -> str | None:
    counts: Counter[str] = Counter()
    for value, count in weighted:
        if value:
            counts[value] += count
    # Egyenlőségnél a betűrend dönt, hogy az eredmény ne függjön a lekérdezés sorrendjétől.
    return min(counts, key=lambda value: (-counts[value], value)) if counts else None


def coverage(counts: list[ExclusionCount]) -> dict:
    """Mit tartalmaz a diagram és az összesítő: a beszámított számlák (ebből sztornó) és a kimaradók oka szerint."""
    included = [count for count in counts if count.exclusion is None]
    excluded = {count.exclusion: count.count for count in counts if count.exclusion is not None}
    return {
        "included": sum(count.count for count in included),
        "storno": sum(count.storno for count in included),
        "excluded": {reason.value: excluded.get(reason.value, 0) for reason in Exclusion},
    }


def invoice_view(record: InvoiceRecord) -> dict:
    """A számlalista egy sora: a kinyert mezők és a tárolt átváltás."""
    fields, conversion = record.fields, record.conversion
    quote = conversion.quote if conversion else None
    return {
        "id": record.id,
        "filename": record.filename,
        "invoice_number": fields.invoice_number,
        "seller": {"name": fields.seller_name, "tax_id": fields.seller_tax_id},
        "buyer": {"name": fields.buyer_name, "tax_id": fields.buyer_tax_id},
        "issue_date": fields.issue_date.isoformat() if fields.issue_date else None,
        "performance_date": fields.performance_date.isoformat() if fields.performance_date else None,
        "due_date": fields.due_date.isoformat() if fields.due_date else None,
        "currency": fields.currency,
        # Az összegek szövegként mennek ki, hogy a JSON-szám ne veszítsen pontosságot.
        "net_total": str(fields.net_total) if fields.net_total is not None else None,
        "vat_total": str(fields.vat_total) if fields.vat_total is not None else None,
        "gross_total": str(fields.gross_total) if fields.gross_total is not None else None,
        "direction": record.direction.value if record.direction else None,
        "partner": {"name": record.partner_name, "tax_id": record.partner_tax_id},
        "status": record.status,
        "issues": list(record.issues),
        "rate": str(quote.rate) if quote else None,
        "rate_unit": quote.unit if quote else None,
        "rate_date": quote.rate_date.isoformat() if quote else None,
        "amount": conversion.amount if conversion else None,
        "problem": conversion.problem if conversion else None,
        "excluded": conversion.exclusion if conversion else None,
        "storno": fields.gross_total is not None and fields.gross_total < 0,
        "future": record.has_issue(FUTURE_PERFORMANCE),
    }


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
        self._convert_lock = asyncio.Lock()

    @property
    def currency(self) -> str:
        return self._report_currency

    async def ensure_converted(self) -> int:
        """Átváltja és tárolja a még át nem váltott (vagy árfolyam nélkül maradt) számlákat; az átváltottak száma.

        Egyszerre egy átváltás fut, így a párhuzamos lekérdezések nem végzik el kétszer ugyanazt.
        """
        async with self._convert_lock:
            pending = await self._repository.pending_conversion(self._report_currency, _FINAL_PROBLEMS)
            if not pending:
                return 0
            failures = await self._rates.prepare(self._needs(pending))
            converted = [(record, await self._convert(record, failures)) for record in pending]
            stored = await self._repository.store_conversions(converted)
            self._log_problems(converted)
            logger.info("invoices converted", extra={"pending": len(pending), "stored": stored})
            return stored

    def _log_problems(self, converted: list[tuple[InvoiceRecord, Conversion]]) -> None:
        """Az összesítésből (részben) kimaradó számlát egyszer naplózza; a megváltozott okot újra."""
        for record, conversion in converted:
            previous = record.conversion
            known = previous.problem if previous and previous.currency == conversion.currency else None
            if conversion.problem and conversion.problem != known:
                self._failure_log.record(
                    SOURCE_REPORT,
                    file_name=record.filename,
                    error_code=conversion.problem,
                    content_hash=record.content_hash,
                    invoice_id=record.id,
                )

    def _needs(self, records: list[InvoiceRecord]) -> dict[str, tuple[date, date]]:
        needs: dict[str, tuple[date, date]] = {}
        for record in records:
            day, currency = rate_date(record), record.fields.currency
            if currency == self._report_currency or record.has_issue(FUTURE_PERFORMANCE):
                continue
            first, last = needs.get(currency, (day, day))
            needs[currency] = (min(first, day), max(last, day))
        return needs

    async def _convert(self, record: InvoiceRecord, failures: dict[str, AppError]) -> Conversion:
        quote, amount, problem = await self._rate_amount(record, failures)
        if problem is None and record.direction is None:
            problem = PROBLEM_NO_DIRECTION
        exclusion = exclusion_of(record, amount)
        return Conversion(
            self._report_currency,
            quote,
            int(amount) if amount is not None else None,
            problem,
            exclusion.value if exclusion else None,
        )

    async def _rate_amount(
        self, record: InvoiceRecord, failures: dict[str, AppError]
    ) -> tuple[RateQuote | None, Decimal | None, str | None]:
        fields = record.fields
        day = rate_date(record)
        if record.has_issue(FUTURE_PERFORMANCE):
            return None, None, FUTURE_PERFORMANCE
        if fields.currency == self._report_currency:
            return None, fields.gross_total.quantize(Decimal(1), rounding=ROUND_HALF_UP), None
        if fields.currency in failures:
            # A sikertelen lekérés csak azt a számlát érinti, amelynek az árfolyama nincs biztosan tárolva.
            quote = await self._rates.stored_rate_on(fields.currency, day)
            if quote is None:
                return None, None, failures[fields.currency].code
        else:
            try:
                quote = await self._rates.rate_on(fields.currency, day)
            except AppError as exc:
                return None, None, exc.code
        return quote, convert(fields.gross_total, quote), None

    async def build(self) -> dict:
        """A diagram, a cégenkénti összesítő, a lefedettség és a kimaradó számlák (legfeljebb `MAX_PROBLEMS`)."""
        await self.ensure_converted()
        currency = self._report_currency
        months, partners, exclusions, (problems, problem_count) = await asyncio.gather(
            self._repository.monthly_totals(currency),
            self._repository.partner_totals(currency),
            self._repository.exclusion_counts(currency),
            self._repository.problems(currency, MAX_PROBLEMS, _UNLISTED_PROBLEMS),
        )
        return {
            "currency": currency,
            "months": monthly_series(months),
            **partner_summary(partners),
            "coverage": coverage(exclusions),
            "problems": [invoice_view(record) for record in problems],
            "problem_count": problem_count,
        }

    async def invoice_page(self, group: InvoiceGroup, offset: int, limit: int) -> dict:
        """A számlalista egy oldala a csoport szerint, a csoportok elemszámával."""
        await self.ensure_converted()
        (records, total), groups = await asyncio.gather(
            self._repository.list_page(group, offset, limit), self._repository.group_counts()
        )
        return {
            "invoices": [invoice_view(record) for record in records],
            "group": group.value,
            "offset": offset,
            "limit": limit,
            "total": total,
            "groups": {name.value: count for name, count in groups.items()},
            "counts": {
                "ok": groups[InvoiceGroup.ALL] - groups[InvoiceGroup.NEEDS_REVIEW],
                "needs_review": groups[InvoiceGroup.NEEDS_REVIEW],
            },
        }
