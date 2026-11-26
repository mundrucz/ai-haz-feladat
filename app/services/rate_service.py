"""Árfolyam-kiválasztás: a lekért napokat megjegyezzük, és csak a hiányzó időszakot kérdezzük le."""

import asyncio
import logging
import time
from collections.abc import Callable, Iterable, Mapping, Sequence
from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal
from typing import Protocol

from app.clients.mnb_client import MnbNoRateError, MnbRateNotYetError, MnbRejectedError, RateQuote
from app.core.errors import AppError
from app.repositories.rate_repository import RateRepository, SupportedCurrencies, merge_ranges

logger = logging.getLogger(__name__)

_DAY = timedelta(days=1)


class RateSource(Protocol):
    async def fetch_rates(self, currencies: Sequence[str], start: date, end: date) -> list[RateQuote]: ...

    async def fetch_currencies(self) -> frozenset[str]: ...


def convert(amount: Decimal, quote: RateQuote) -> Decimal:
    """Forintösszeg: egész forintra kerekítve (0,5-től felfelé), az `unit` figyelembevételével."""
    return (amount * quote.rate / quote.unit).quantize(Decimal(1), rounding=ROUND_HALF_UP)


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
        max_request_days: int = 366,
        currencies_ttl_days: int = 7,
        retry_seconds: float = 3600.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._repository = repository
        self._source = source
        self._today = today
        self._lookback = timedelta(days=lookback_days)
        self._max_request = timedelta(days=max(1, max_request_days))
        self._currencies_ttl = timedelta(days=currencies_ttl_days)
        self._retry = retry_seconds
        self._clock = clock
        self._currencies_failed_at: float | None = None
        self._rates_failed_at: float | None = None
        self._rates_error: AppError | None = None
        self._currencies_lock = asyncio.Lock()
        self._fetch_lock = asyncio.Lock()

    async def is_quoted(self, currency: str) -> bool | None:
        """Jegyzi-e az MNB a devizát (GetCurrencies); None, ha a lista nem érhető el (nem tudjuk)."""
        supported = await self._supported_currencies()
        return None if supported is None else currency in supported

    async def _supported_currencies(self) -> frozenset[str] | None:
        stored = await self._repository.supported_currencies()
        if self._fresh(stored):
            return stored.codes
        # Egyszerre egy lekérés fut; a közelmúltban sikertelen lekérést nem ismételjük minden számlánál.
        async with self._currencies_lock:
            stored = await self._repository.supported_currencies()
            if self._fresh(stored):
                return stored.codes
            fallback = stored.codes if stored else None
            if self._recently(self._currencies_failed_at):
                return fallback
            try:
                codes = await self._source.fetch_currencies()
            except AppError as exc:
                age = (self._today() - stored.fetched_on).days if stored else None
                logger.warning(
                    "mnb currency list unavailable",
                    extra={"code": exc.code, "stored_list_age_days": age},
                )
                self._currencies_failed_at = self._clock()
                return fallback
            self._currencies_failed_at = None
            await self._repository.store_supported_currencies(codes, self._today())
            logger.info("mnb currency list refreshed", extra={"currencies": len(codes)})
            return codes

    def _fresh(self, stored: SupportedCurrencies | None) -> bool:
        return stored is not None and self._today() - stored.fetched_on < self._currencies_ttl

    def _recently(self, failed_at: float | None) -> bool:
        return failed_at is not None and self._clock() - failed_at < self._retry

    async def prepare(self, needs: Mapping[str, tuple[date, date]]) -> dict[str, AppError]:
        """Egyetlen kéréssel lekéri a hiányzó devizák hiányzó időszakát; a sikertelen devizák hibáit adja vissza."""
        failures: dict[str, AppError] = {}
        today = self._today()
        wanted = {
            currency: (first - self._lookback, min(last, today))
            for currency, (first, last) in needs.items()
            if first - self._lookback <= min(last, today)
        }
        if not wanted:
            return failures
        # Egyszerre egy lekérés fut; a várakozó hívás a zár után újra megnézi, mi hiányzik, így nem duplikál.
        async with self._fetch_lock:
            gaps: dict[str, list[tuple[date, date]]] = {}
            for currency, (start, end) in wanted.items():
                if missing := missing_ranges(start, end, await self._repository.coverage(currency)):
                    gaps[currency] = missing
            if gaps:
                start = min(found[0][0] for found in gaps.values())
                end = max(found[-1][1] for found in gaps.values())
                # A tartományt legfeljebb `max_request_days` hosszú darabokra bontjuk; a hiány nélküli darabot kihagyjuk.
                chunk_start = start
                while chunk_start <= end:
                    chunk_end = min(end, chunk_start + self._max_request - _DAY)
                    needed = sorted(
                        currency
                        for currency, found in gaps.items()
                        if currency not in failures and any(s <= chunk_end and e >= chunk_start for s, e in found)
                    )
                    if needed:
                        await self._fetch(needed, chunk_start, chunk_end, failures)
                    chunk_start = chunk_end + _DAY
        return failures

    async def rate_on(self, currency: str, day: date) -> RateQuote:
        earliest = day - self._lookback
        if earliest > self._today():
            raise MnbRateNotYetError(f"no MNB rate yet for {currency} on {day.isoformat()}: the date is in the future")
        quote = await self._repository.latest_on_or_before(currency, day, earliest)
        if quote is None:
            raise MnbNoRateError(f"no MNB rate for {currency} on or before {day.isoformat()}")
        return quote

    async def stored_rate_on(self, currency: str, day: date) -> RateQuote | None:
        """A tárolt árfolyam, ha a jegyzés napjától `day`-ig minden nap le van kérve (így MNB nélkül is biztos)."""
        earliest = day - self._lookback
        quote = await self._repository.latest_on_or_before(currency, day, earliest)
        start = quote.rate_date if quote else earliest
        if missing_ranges(start, day, await self._repository.coverage(currency)):
            return None
        return quote

    async def _fetch(self, currencies: list[str], start: date, end: date, failures: dict[str, AppError]) -> None:
        # Elérhetetlen MNB-t csak `retry_seconds` után próbálunk újra, addig a legutóbbi hibát adjuk vissza.
        if self._rates_error is not None and self._recently(self._rates_failed_at):
            self._record_failure(currencies, self._rates_error, failures)
            return
        try:
            quotes = await self._source.fetch_rates(currencies, start, end)
        except MnbRejectedError as exc:
            if len(currencies) == 1:
                self._record_failure(currencies, exc, failures)
                return
            # Egy elutasított deviza ne vigye magával a többit: külön kérésekkel kiderül, melyik a hibás.
            for currency in currencies:
                await self._fetch([currency], start, end, failures)
            return
        except AppError as exc:
            self._rates_failed_at, self._rates_error = self._clock(), exc
            self._record_failure(currencies, exc, failures)
            return
        self._rates_failed_at = self._rates_error = None
        by_currency: dict[str, list[RateQuote]] = {currency: [] for currency in currencies}
        for quote in quotes:
            if quote.currency in by_currency:
                by_currency[quote.currency].append(quote)
        # A mai nap árfolyama napközben még hiányozhat, ezért legfeljebb tegnapig jegyezzük lekértnek.
        covered_end = min(end, self._today() - _DAY)
        for currency, found in by_currency.items():
            await self._repository.store(currency, start, covered_end, found)

    @staticmethod
    def _record_failure(currencies: list[str], exc: AppError, failures: dict[str, AppError]) -> None:
        logger.warning("rate fetch failed", extra={"currencies": ",".join(currencies), "code": exc.code})
        for currency in currencies:
            failures[currency] = exc
