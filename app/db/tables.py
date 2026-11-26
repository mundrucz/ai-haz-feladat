"""Az adatbázis táblái ORM-modellként. A séma változását Alembic-revízió követi (`app/db/migrations`)."""

from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import JSON, CheckConstraint, Dialect, MetaData, String, func
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy.types import TypeDecorator


class DecimalText(TypeDecorator[Decimal]):
    """Pontos `Decimal` szövegként tárolva: az SQLite a `Numeric`-et lebegőpontosan tárolná."""

    impl = String
    cache_ok = True

    def process_bind_param(self, value: Decimal | None, dialect: Dialect) -> str | None:
        return None if value is None else str(value)

    def process_result_value(self, value: str | None, dialect: Dialect) -> Decimal | None:
        return None if value is None else Decimal(value)


class Base(DeclarativeBase):
    # Kiszámítható kényszernevek, hogy az Alembic a későbbi módosításoknál név szerint hivatkozhasson rájuk.
    metadata = MetaData(
        naming_convention={
            "ix": "ix_%(table_name)s_%(column_0_name)s",
            "uq": "uq_%(table_name)s_%(column_0_name)s",
            "ck": "ck_%(table_name)s_%(constraint_name)s",
            "pk": "pk_%(table_name)s",
        }
    )
    type_annotation_map = {Decimal: DecimalText()}


class InvoiceRow(Base):
    """Egy teljesen kinyert számla. A PDF-et nem tároljuk, csak a tartalom-hash-t a duplikáció-szűréshez."""

    __tablename__ = "invoices"
    __table_args__ = (
        CheckConstraint("direction IN ('outgoing', 'incoming')", name="direction"),
        CheckConstraint("status IN ('ok', 'needs_review')", name="status"),
        {"sqlite_autoincrement": True},
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    content_hash: Mapped[str] = mapped_column(unique=True)
    filename: Mapped[str]
    invoice_number: Mapped[str]
    seller_name: Mapped[str]
    seller_tax_id: Mapped[str]
    buyer_name: Mapped[str]
    buyer_tax_id: Mapped[str]
    issue_date: Mapped[date]
    performance_date: Mapped[date]
    due_date: Mapped[date]
    currency: Mapped[str]
    net_total: Mapped[Decimal | None]
    vat_total: Mapped[Decimal | None]
    gross_total: Mapped[Decimal]
    direction: Mapped[str | None]
    partner_name: Mapped[str | None]
    partner_tax_id: Mapped[str | None]
    status: Mapped[str] = mapped_column(index=True)
    issues: Mapped[list[dict]] = mapped_column(JSON, server_default="[]")
    # Kiállító adószáma + számlaszám (normalizálva): ugyanaz a számla más fájlból sem tárolható kétszer.
    identity_key: Mapped[str | None] = mapped_column(unique=True)
    created_at: Mapped[datetime] = mapped_column(server_default=func.current_timestamp())


class ExchangeRateRow(Base):
    """MNB napi árfolyam; egy deviza-nap egyszer kerül lekérésre."""

    __tablename__ = "exchange_rates"
    __table_args__ = (CheckConstraint("unit > 0", name="unit_positive"),)

    currency: Mapped[str] = mapped_column(primary_key=True)
    rate_date: Mapped[date] = mapped_column(primary_key=True)
    unit: Mapped[int]
    rate: Mapped[Decimal]


class RateCoverageRow(Base):
    """A már lekérdezett időszakok (a jegyzés nélküli napokkal együtt), hogy ne kérdezzük őket újra."""

    __tablename__ = "rate_coverage"

    currency: Mapped[str] = mapped_column(primary_key=True)
    start_date: Mapped[date] = mapped_column(primary_key=True)
    end_date: Mapped[date]


class MnbCurrencyRow(Base):
    """Az MNB által jegyzett devizák (GetCurrencies) gyorsítótára; a `fetched_on` a lekérés napja."""

    __tablename__ = "mnb_currencies"

    code: Mapped[str] = mapped_column(primary_key=True)
    fetched_on: Mapped[date]
