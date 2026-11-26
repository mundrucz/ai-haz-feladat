"""Kinyert számlák tárolása: a tárolt rekord, az irány és a duplikáció-szűrő azonosító kulcs."""

import logging
from dataclasses import dataclass
from enum import StrEnum

from sqlalchemy import delete, select
from sqlalchemy.dialects.sqlite import insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import ProcessingError
from app.db.database import Database
from app.db.tables import InvoiceRow
from app.extraction.models import InvoiceFields
from app.extraction.normalize import normalize_key

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


@dataclass(frozen=True, slots=True)
class Classification:
    direction: Direction | None
    partner_name: str | None
    partner_tax_id: str | None


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

    def has_issue(self, code: str) -> bool:
        return any(issue.get("code") == code for issue in self.issues)


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
        }
        statement = insert(InvoiceRow).values(content_hash=content_hash, **values)
        statement = statement.on_conflict_do_update(
            index_elements=[InvoiceRow.content_hash],
            set_={name: statement.excluded[name] for name in values},
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
            rows = await session.scalars(
                select(InvoiceRow).order_by(InvoiceRow.issue_date, InvoiceRow.invoice_number, InvoiceRow.id)
            )
            return [_to_record(row) for row in rows]
