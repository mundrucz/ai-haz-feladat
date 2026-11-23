"""Árfolyamok tárolása és kiválasztása: a lekért napokat megjegyezzük, és csak a hiányzó időszakot kérdezzük le."""

import asyncio
import logging
from collections.abc import Callable, Iterable, Mapping
from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal
from typing import Protocol

from app.db import Database
from app.errors import AppError
from app.mnb import MnbNoRateError, RateQuote

logger = logging.getLogger(__name__)

_DAY = timedelta(days=1)


class RateSource(Protocol):
    async def fetch_rates(self, currency: str, start: date, end: date) -> list[RateQuote]: ...


def convert(amount: Decimal, quote: RateQuote) -> Decimal:
    """Forintösszeg: egész forintra kerekítve (0,5-től felfelé), az `unit` figyelembevételével."""
    return (amount * quote.rate / quote.unit).quantize(Decimal(1), rounding=ROUND_HALF_UP)


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


def missing_ranges(start: date, end: date, covered: Iterable[tuple[date, date]]) -> list[tuple[date, date]]:
    """A [start, end] azon részei, amelyeket a (összevont) `covered` időszakok nem fednek."""
    gaps: list[tuple[date, date]] = []
    cursor = start
    for covered_start, covered_end in merge_ranges(covered):
        if covered_end < cursor:
            continue
        if covered_start > end:
            break
        if covered_start > cursor:
            gaps.append((cursor, covered_start - _DAY))
        cursor = max(cursor, covered_end + _DAY)
    if cursor <= end:
        gaps.append((cursor, end))
    return gaps


class RateService:
    """Árfolyam-kiválasztás: a teljesítés napján érvényes, vagy az azt megelőző utolsó jegyzés."""

    def __init__(
        self,
        repository: RateRepository,
        source: RateSource,
        *,
        today: Callable[[], date] = date.today,
        lookback_days: int = 31,
    ) -> None:
        self._repository = repository
        self._source = source
        self._today = today
        self._lookback = timedelta(days=lookback_days)
        self._locks: dict[str, asyncio.Lock] = {}

    async def prepare(self, needs: Mapping[str, tuple[date, date]]) -> dict[str, AppError]:
        """Devizánként egyszer lekéri a hiányzó időszakot; a sikertelen devizák hibáit adja vissza."""
        failures: dict[str, AppError] = {}

        async def one(currency: str, first: date, last: date) -> None:
            try:
                await self._ensure(currency, first, last)
            except AppError as exc:
                logger.warning("rate fetch failed", extra={"currency": currency, "code": exc.code})
                failures[currency] = exc

        await asyncio.gather(*(one(currency, *span) for currency, span in needs.items()))
        return failures

    async def rate_on(self, currency: str, day: date) -> RateQuote:
        quote = await self._repository.latest_on_or_before(currency, day, day - self._lookback)
        if quote is None:
            raise MnbNoRateError(f"no MNB rate for {currency} on or before {day.isoformat()}")
        return quote

    async def _ensure(self, currency: str, first: date, last: date) -> None:
        today = self._today()
        start, end = first - self._lookback, min(last, today)
        if end < start:
            return
        # Devizánként egyszerre egy lekérés fut, így az azonos időszakot kérő párhuzamos hívások nem duplikálnak.
        async with self._locks.setdefault(currency, asyncio.Lock()):
            for gap_start, gap_end in missing_ranges(start, end, await self._repository.coverage(currency)):
                quotes = await self._source.fetch_rates(currency, gap_start, gap_end)
                # A mai nap árfolyama napközben még hiányozhat, ezért legfeljebb tegnapig jegyezzük lekértnek.
                covered_end = min(gap_end, today - _DAY)
                await self._repository.store(currency, gap_start, covered_end, quotes)
