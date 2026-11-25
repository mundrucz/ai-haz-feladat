"""Árfolyamok tárolása: napi árfolyamok, a már lekért időszakok és az MNB által jegyzett devizák listája."""

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal

from app.clients.mnb_client import RateQuote
from app.db.database import Database

_DAY = timedelta(days=1)


@dataclass(frozen=True, slots=True)
class SupportedCurrencies:
    codes: frozenset[str]
    fetched_on: date


class RateRepository:
    def __init__(self, db: Database) -> None:
        self._db = db

    async def coverage(self, currency: str) -> list[tuple[date, date]]:
        async with self._db.connect() as conn:
            async with conn.execute(
                "SELECT start_date, end_date FROM rate_coverage WHERE currency = ? ORDER BY start_date", (currency,)
            ) as cursor:
                return [(date.fromisoformat(r["start_date"]), date.fromisoformat(r["end_date"])) async for r in cursor]

    async def store(
        self, currency: str, start: date, end: date | None, quotes: Iterable[RateQuote]
    ) -> None:
        """Elmenti az árfolyamokat, és (ha `end` van) a [start, end] időszakot lekértként jegyzi."""
        async with self._db.connect() as conn:
            await conn.execute("BEGIN IMMEDIATE")
            await conn.executemany(
                "INSERT INTO exchange_rates (currency, rate_date, unit, rate) VALUES (?, ?, ?, ?) "
                "ON CONFLICT (currency, rate_date) DO UPDATE SET unit = excluded.unit, rate = excluded.rate",
                [(q.currency, q.rate_date.isoformat(), q.unit, str(q.rate)) for q in quotes],
            )
            if end is not None and end >= start:
                async with conn.execute(
                    "SELECT start_date, end_date FROM rate_coverage WHERE currency = ?", (currency,)
                ) as cursor:
                    ranges = [(date.fromisoformat(r["start_date"]), date.fromisoformat(r["end_date"])) async for r in cursor]
                merged = merge_ranges([*ranges, (start, end)])
                await conn.execute("DELETE FROM rate_coverage WHERE currency = ?", (currency,))
                await conn.executemany(
                    "INSERT INTO rate_coverage (currency, start_date, end_date) VALUES (?, ?, ?)",
                    [(currency, s.isoformat(), e.isoformat()) for s, e in merged],
                )
            await conn.commit()

    async def supported_currencies(self) -> SupportedCurrencies | None:
        async with self._db.connect() as conn:
            async with conn.execute("SELECT code, fetched_on FROM mnb_currencies") as cursor:
                rows = await cursor.fetchall()
        if not rows:
            return None
        return SupportedCurrencies(frozenset(r["code"] for r in rows), date.fromisoformat(rows[0]["fetched_on"]))

    async def store_supported_currencies(self, codes: Iterable[str], fetched_on: date) -> None:
        async with self._db.connect() as conn:
            await conn.execute("BEGIN IMMEDIATE")
            await conn.execute("DELETE FROM mnb_currencies")
            await conn.executemany(
                "INSERT INTO mnb_currencies (code, fetched_on) VALUES (?, ?)",
                [(code, fetched_on.isoformat()) for code in codes],
            )
            await conn.commit()

    async def latest_on_or_before(self, currency: str, day: date, earliest: date) -> RateQuote | None:
        async with self._db.connect() as conn:
            async with conn.execute(
                "SELECT rate_date, unit, rate FROM exchange_rates "
                "WHERE currency = ? AND rate_date <= ? AND rate_date >= ? ORDER BY rate_date DESC LIMIT 1",
                (currency, day.isoformat(), earliest.isoformat()),
            ) as cursor:
                row = await cursor.fetchone()
        if row is None:
            return None
        return RateQuote(currency, date.fromisoformat(row["rate_date"]), row["unit"], Decimal(row["rate"]))


def merge_ranges(ranges: Iterable[tuple[date, date]]) -> list[tuple[date, date]]:
    """Az átfedő és egymás utáni (szomszédos napos) időszakok összevonása."""
    merged: list[tuple[date, date]] = []
    for start, end in sorted(ranges):
        if merged and start <= merged[-1][1] + _DAY:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return merged
