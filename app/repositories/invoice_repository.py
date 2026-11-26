"""Kinyert számlák tárolása: a tárolt rekord, az irány, a duplikáció-szűrő azonosító kulcs és a tárolt átváltás."""

import logging
from collections.abc import Collection, Sequence
from dataclasses import dataclass
from enum import StrEnum

from sqlalchemy import ColumnElement, Float, String, and_, cast, delete, func, or_, select, true, type_coerce, update
from sqlalchemy.dialects.sqlite import insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.clients.mnb_client import RateQuote
from app.core.errors import ProcessingError
from app.db.database import Database
from app.db.tables import InvoiceRow
from app.extraction.models import InvoiceFields
from app.extraction.normalize import normalize_key
from app.extraction.validation import FUTURE_PERFORMANCE, InvoiceStatus

logger = logging.getLogger(__name__)

_FIELD_COLUMNS = (
    "invoice_number",
    "seller_name",
    "seller_tax_id",
    "buyer_name",
    "buyer_tax_id",
    "currency",
    "issue_date",
    "performance_date",
    "due_date",
    "net_total",
    "vat_total",
    "gross_total",
)


class Direction(StrEnum):
    OUTGOING = "outgoing"
    INCOMING = "incoming"


class InvoiceGroup(StrEnum):
    """A számlalista csoportjai (szűrői)."""

    ALL = "all"
    STORNO = "storno"
    FUTURE = "future"
    NEEDS_REVIEW = "needs_review"


@dataclass(frozen=True, slots=True)
class Classification:
    direction: Direction | None
    partner_name: str | None
    partner_tax_id: str | None


@dataclass(frozen=True, slots=True)
class Conversion:
    """A kimutatás pénznemére váltás eredménye: egész összeg, vagy a hiány oka; `exclusion`: miért marad ki."""

    currency: str
    quote: RateQuote | None
    amount: int | None
    problem: str | None
    exclusion: str | None


@dataclass(frozen=True, slots=True)
class InvoiceRecord:
    id: int
    filename: str
    fields: InvoiceFields
    direction: Direction | None
    partner_name: str | None
    partner_tax_id: str | None
    status: str
    issues: tuple[dict, ...]
    content_hash: str = ""
    revision: int = 0
    conversion: Conversion | None = None

    def has_issue(self, code: str) -> bool:
        return any(issue.get("code") == code for issue in self.issues)


@dataclass(frozen=True, slots=True)
class MonthTotal:
    month: str
    direction: Direction
    amount: int


@dataclass(frozen=True, slots=True)
class PartnerTotal:
    tax_id: str | None
    name: str | None
    direction: Direction
    amount: int
    count: int


@dataclass(frozen=True, slots=True)
class ExclusionCount:
    exclusion: str | None
    count: int
    storno: int


_STORNO = cast(InvoiceRow.gross_total, Float) < 0
_FUTURE = type_coerce(InvoiceRow.issues, String).like(f'%"{FUTURE_PERFORMANCE}"%')
_NEEDS_REVIEW = InvoiceRow.status == InvoiceStatus.NEEDS_REVIEW.value
_GROUP_FILTERS: dict[InvoiceGroup, ColumnElement[bool]] = {
    InvoiceGroup.ALL: true(),
    InvoiceGroup.STORNO: _STORNO,
    InvoiceGroup.FUTURE: _FUTURE,
    InvoiceGroup.NEEDS_REVIEW: _NEEDS_REVIEW,
}
_ORDER = (InvoiceRow.issue_date, InvoiceRow.invoice_number, InvoiceRow.id)
_UNCONVERTED = {
    "report_currency": None,
    "rate": None,
    "rate_unit": None,
    "rate_date": None,
    "report_amount": None,
    "report_problem": None,
    "report_exclusion": None,
}


def _conversion_values(conversion: Conversion) -> dict:
    quote = conversion.quote
    return {
        "report_currency": conversion.currency,
        "rate": quote.rate if quote else None,
        "rate_unit": quote.unit if quote else None,
        "rate_date": quote.rate_date if quote else None,
        "report_amount": conversion.amount,
        "report_problem": conversion.problem,
        "report_exclusion": conversion.exclusion,
    }


def _to_conversion(row: InvoiceRow) -> Conversion | None:
    if row.report_currency is None:
        return None
    quote = RateQuote(row.currency, row.rate_date, row.rate_unit, row.rate) if row.rate is not None else None
    return Conversion(row.report_currency, quote, row.report_amount, row.report_problem, row.report_exclusion)


def _to_record(row: InvoiceRow) -> InvoiceRecord:
    return InvoiceRecord(
        id=row.id,
        filename=row.filename,
        fields=InvoiceFields(**{name: getattr(row, name) for name in _FIELD_COLUMNS}),
        direction=Direction(row.direction) if row.direction else None,
        partner_name=row.partner_name,
        partner_tax_id=row.partner_tax_id,
        status=row.status,
        issues=tuple(row.issues),
        content_hash=row.content_hash,
        revision=row.revision,
        conversion=_to_conversion(row),
    )


def identity_key(fields: InvoiceFields) -> str | None:
    """A számla azonossága: kiállító adószáma + számlaszám, írásjel- és kisbetű-függetlenül."""
    tax_id, number = normalize_key(fields.seller_tax_id), normalize_key(fields.invoice_number)
    return f"{tax_id}|{number}" if tax_id and number else None


class DuplicateInvoiceError(ProcessingError):
    def __init__(self, existing_filename: str) -> None:
        super().__init__(f"the same invoice is already stored (file: {existing_filename})", code="duplicate_invoice")
        self.existing_filename = existing_filename


class InvoiceRepository:
    def __init__(self, db: Database) -> None:
        self._db = db

    async def exists(self, content_hash: str) -> bool:
        async with self._db.session() as session:
            found = await session.scalar(select(InvoiceRow.id).where(InvoiceRow.content_hash == content_hash))
        return found is not None

    async def find(self, content_hash: str) -> InvoiceRecord | None:
        async with self._db.session() as session:
            row = await session.scalar(select(InvoiceRow).where(InvoiceRow.content_hash == content_hash))
            return _to_record(row) if row is not None else None

    async def remove_issue(self, content_hash: str, code: str) -> bool:
        """Leveszi a tárolt számla adott kódú jelölését; igaz, ha volt mit levenni."""
        async with self._db.transaction(immediate=True) as session:
            row = await session.scalar(select(InvoiceRow).where(InvoiceRow.content_hash == content_hash))
            if row is None:
                return False
            kept = [issue for issue in row.issues if issue.get("code") != code]
            if len(kept) == len(row.issues):
                return False
            row.issues = kept
            row.revision += 1
            for name, value in _UNCONVERTED.items():
                setattr(row, name, value)
        return True

    async def save(
        self,
        content_hash: str,
        filename: str,
        fields: InvoiceFields,
        classification: Classification,
        status: str,
        issues: list[dict],
    ) -> None:
        """Tartalmanként egy számla; ugyanannak a tartalomnak az újrafeldolgozása felülírja az előző eredményt.

        Ha ugyanez a számla (kiállító adószáma + számlaszám) már egy másik fájlból tárolva van, `DuplicateInvoiceError`.
        """
        key = identity_key(fields)
        values = {name: getattr(fields, name) for name in _FIELD_COLUMNS} | {
            "filename": filename,
            "direction": classification.direction.value if classification.direction else None,
            "partner_name": classification.partner_name,
            "partner_tax_id": classification.partner_tax_id,
            "status": status,
            "issues": issues,
            "identity_key": key,
        } | _UNCONVERTED
        statement = insert(InvoiceRow).values(content_hash=content_hash, **values)
        statement = statement.on_conflict_do_update(
            index_elements=[InvoiceRow.content_hash],
            set_={name: statement.excluded[name] for name in values} | {"revision": InvoiceRow.revision + 1},
        )
        try:
            # Az író zárat az ellenőrzés előtt vesszük meg, hogy két párhuzamos feldolgozás ne mentse ugyanazt.
            async with self._db.transaction(immediate=True) as session:
                if key and (existing := await self._other_with_key(session, key, content_hash)):
                    raise DuplicateInvoiceError(existing)
                await session.execute(statement)
        except IntegrityError:
            # Az egyedi kulcs a végső védelem: ha mégis ütközés van, ugyanúgy duplikátumként kezeljük.
            async with self._db.session() as session:
                existing = await self._other_with_key(session, key, content_hash) if key else None
            if existing:
                raise DuplicateInvoiceError(existing) from None
            raise

    @staticmethod
    async def _other_with_key(session: AsyncSession, key: str, content_hash: str) -> str | None:
        return await session.scalar(
            select(InvoiceRow.filename)
            .where(InvoiceRow.identity_key == key, InvoiceRow.content_hash != content_hash)
            .limit(1)
        )

    async def delete_all(self) -> int:
        """Minden tárolt számla törlése (az árfolyam-gyorsítótár érintetlen); a törölt sorok száma."""
        async with self._db.transaction() as session:
            result = await session.execute(delete(InvoiceRow))
        return result.rowcount

    async def list_all(self) -> list[InvoiceRecord]:
        async with self._db.session() as session:
            rows = await session.scalars(select(InvoiceRow).order_by(*_ORDER))
            return [_to_record(row) for row in rows]

    async def list_page(self, group: InvoiceGroup, offset: int, limit: int) -> tuple[list[InvoiceRecord], int]:
        """A csoport egy oldala (kiállítás dátuma szerint rendezve) és a csoport elemszáma."""
        condition = _GROUP_FILTERS[group]
        async with self._db.session() as session:
            total = await session.scalar(select(func.count()).select_from(InvoiceRow).where(condition))
            rows = await session.scalars(select(InvoiceRow).where(condition).order_by(*_ORDER).offset(offset).limit(limit))
            return [_to_record(row) for row in rows], total or 0

    async def group_counts(self) -> dict[InvoiceGroup, int]:
        async with self._db.session() as session:
            row = (
                await session.execute(select(*(func.count().filter(_GROUP_FILTERS[group]) for group in InvoiceGroup)))
            ).one()
        return dict(zip(InvoiceGroup, row, strict=True))

    async def pending_conversion(self, currency: str, final_problems: Collection[str]) -> list[InvoiceRecord]:
        """Az átváltandó számlák: nincs (erre a pénznemre) átváltva, vagy nem volt hozzá árfolyam.

        A `final_problems` kódú hiány (pl. jövőbeli teljesítés) nem próbálkozik újra.
        """
        stale = or_(InvoiceRow.report_currency.is_(None), InvoiceRow.report_currency != currency)
        retry = and_(InvoiceRow.report_amount.is_(None), InvoiceRow.report_problem.not_in(list(final_problems)))
        async with self._db.session() as session:
            rows = await session.scalars(select(InvoiceRow).where(or_(stale, retry)).order_by(InvoiceRow.id))
            return [_to_record(row) for row in rows]

    async def store_conversions(self, items: Sequence[tuple[InvoiceRecord, Conversion]]) -> int:
        """Elmenti az átváltásokat; a közben módosult (más revíziójú) számlát kihagyja. A mentett sorok száma."""
        stored = 0
        async with self._db.transaction() as session:
            for record, conversion in items:
                result = await session.execute(
                    update(InvoiceRow)
                    .where(InvoiceRow.id == record.id, InvoiceRow.revision == record.revision)
                    .values(**_conversion_values(conversion))
                )
                stored += result.rowcount
        return stored

    async def problems(
        self, currency: str, limit: int, ignore: tuple[str, ...] = ()
    ) -> tuple[list[InvoiceRecord], int]:
        """Az átváltott, de (részben) kimaradó számlák, amelyeknek oka van (pl. nincs árfolyam); legfeljebb `limit`.

        Az `ignore` okú számlák nem szerepelnek (sem a listában, sem a darabszámban).
        """
        condition = and_(
            InvoiceRow.report_currency == currency,
            InvoiceRow.report_problem.is_not(None),
            InvoiceRow.report_problem.not_in(list(ignore)),
        )
        async with self._db.session() as session:
            total = await session.scalar(select(func.count()).select_from(InvoiceRow).where(condition))
            rows = await session.scalars(select(InvoiceRow).where(condition).order_by(*_ORDER).limit(limit))
            return [_to_record(row) for row in rows], total or 0

    async def monthly_totals(self, currency: str) -> list[MonthTotal]:
        """A beszámított számlák összege a fizetési határidő hónapja és iránya szerint."""
        month = func.substr(InvoiceRow.due_date, 1, 7, type_=String)
        statement = (
            select(month, InvoiceRow.direction, func.sum(InvoiceRow.report_amount))
            .where(*self._included(currency))
            .group_by(month, InvoiceRow.direction)
        )
        async with self._db.session() as session:
            rows = (await session.execute(statement)).all()
        return [MonthTotal(month, Direction(direction), amount) for month, direction, amount in rows]

    async def partner_totals(self, currency: str) -> list[PartnerTotal]:
        """A beszámított számlák összege és darabszáma partner-adószám, -név és irány szerint."""
        columns = (InvoiceRow.partner_tax_id, InvoiceRow.partner_name, InvoiceRow.direction)
        statement = (
            select(*columns, func.sum(InvoiceRow.report_amount), func.count())
            .where(*self._included(currency))
            .group_by(*columns)
        )
        async with self._db.session() as session:
            rows = (await session.execute(statement)).all()
        return [PartnerTotal(tax_id, name, Direction(direction), amount, count) for tax_id, name, direction, amount, count in rows]

    async def exclusion_counts(self, currency: str) -> list[ExclusionCount]:
        """Az átváltott számlák száma (és ebből a sztornóké) a kimaradás oka szerint; `None`: beszámított."""
        statement = (
            select(InvoiceRow.report_exclusion, func.count(), func.count().filter(_STORNO))
            .where(InvoiceRow.report_currency == currency)
            .group_by(InvoiceRow.report_exclusion)
        )
        async with self._db.session() as session:
            rows = (await session.execute(statement)).all()
        return [ExclusionCount(exclusion, count, storno) for exclusion, count, storno in rows]

    @staticmethod
    def _included(currency: str) -> tuple[ColumnElement[bool], ...]:
        return InvoiceRow.report_currency == currency, InvoiceRow.report_exclusion.is_(None)
