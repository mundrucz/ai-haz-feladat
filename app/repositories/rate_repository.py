"""Árfolyamok tárolása: napi árfolyamok, a már lekért időszakok és az MNB által jegyzett devizák listája."""

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date, timedelta

from sqlalchemy import delete, select
from sqlalchemy.dialects.sqlite import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.clients.mnb_client import RateQuote
from app.db.database import Database
from app.db.tables import ExchangeRateRow, MnbCurrencyRow, RateCoverageRow

_DAY = timedelta(days=1)


@dataclass(frozen=True, slots=True)
class SupportedCurrencies:
    codes: frozenset[str]
    fetched_on: date


class RateRepository:
    def __init__(self, db: Database) -> None:
        self._db = db

    async def coverage(self, currency: str) -> list[tuple[date, date]]:
        async with self._db.session() as session:
            return await self._coverage(session, currency)

    @staticmethod
    async def _coverage(session: AsyncSession, currency: str) -> list[tuple[date, date]]:
        rows = await session.execute(
            select(RateCoverageRow.start_date, RateCoverageRow.end_date)
            .where(RateCoverageRow.currency == currency)
            .order_by(RateCoverageRow.start_date)
        )
        return [(start, end) for start, end in rows]

    async def store(
        self, currency: str, start: date, end: date | None, quotes: Iterable[RateQuote]
    ) -> None:
        """Elmenti az árfolyamokat, és (ha `end` van) a [start, end] időszakot lekértként jegyzi."""
        rates = [
            {"currency": q.currency, "rate_date": q.rate_date, "unit": q.unit, "rate": q.rate} for q in quotes
        ]
        async with self._db.transaction(immediate=True) as session:
            if rates:
                statement = insert(ExchangeRateRow).values(rates)
                await session.execute(
                    statement.on_conflict_do_update(
                        index_elements=[ExchangeRateRow.currency, ExchangeRateRow.rate_date],
                        set_={"unit": statement.excluded.unit, "rate": statement.excluded.rate},
                    )
                )
            if end is not None and end >= start:
                merged = merge_ranges([*await self._coverage(session, currency), (start, end)])
                await session.execute(delete(RateCoverageRow).where(RateCoverageRow.currency == currency))
                session.add_all(RateCoverageRow(currency=currency, start_date=s, end_date=e) for s, e in merged)

    async def supported_currencies(self) -> SupportedCurrencies | None:
        async with self._db.session() as session:
            rows = (await session.scalars(select(MnbCurrencyRow))).all()
        if not rows:
            return None
        return SupportedCurrencies(frozenset(row.code for row in rows), rows[0].fetched_on)

    async def store_supported_currencies(self, codes: Iterable[str], fetched_on: date) -> None:
        async with self._db.transaction(immediate=True) as session:
            await session.execute(delete(MnbCurrencyRow))
            session.add_all(MnbCurrencyRow(code=code, fetched_on=fetched_on) for code in codes)

    async def latest_on_or_before(self, currency: str, day: date, earliest: date) -> RateQuote | None:
        async with self._db.session() as session:
            row = await session.scalar(
                select(ExchangeRateRow)
                .where(
                    ExchangeRateRow.currency == currency,
                    ExchangeRateRow.rate_date <= day,
                    ExchangeRateRow.rate_date >= earliest,
                )
                .order_by(ExchangeRateRow.rate_date.desc())
                .limit(1)
            )
        if row is None:
            return None
        return RateQuote(row.currency, row.rate_date, row.unit, row.rate)


def merge_ranges(ranges: Iterable[tuple[date, date]]) -> list[tuple[date, date]]:
    """Az átfedő és egymás utáni (szomszédos napos) időszakok összevonása."""
    merged: list[tuple[date, date]] = []
    for start, end in sorted(ranges):
        if merged and start <= merged[-1][1] + _DAY:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return merged
