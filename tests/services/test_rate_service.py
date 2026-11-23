import asyncio
from datetime import date
from decimal import Decimal

import pytest

from app.db import Database
from app.mnb import MnbError, MnbNoRateError, RateQuote
from app.rates import RateRepository, RateService, convert, merge_ranges, missing_ranges

TODAY = date(2026, 6, 15)


def d(day: int, month: int = 1) -> date:
    return date(2026, month, day)


class FakeSource:
    """Hétköznapokra ad árfolyamot, a hívásokat rögzíti."""

    def __init__(self, unit: int = 1, rate: str = "400", delay: float = 0, fail: Exception | None = None) -> None:
        self.calls: list[tuple[str, date, date]] = []
        self.unit, self.rate, self.delay, self.fail = unit, Decimal(rate), delay, fail

    async def fetch_rates(self, currency: str, start: date, end: date) -> list[RateQuote]:
        self.calls.append((currency, start, end))
        await asyncio.sleep(self.delay)
        if self.fail:
            raise self.fail
        quotes, day = [], start
        while day <= end:
            if day.weekday() < 5:
                quotes.append(RateQuote(currency, day, self.unit, self.rate + day.day))
            day = date.fromordinal(day.toordinal() + 1)
        return quotes


@pytest.fixture
async def repository(tmp_path):
    db = Database(tmp_path / "rates.db")
    await db.init()
    return RateRepository(db)


def service(repository, source, **kwargs) -> RateService:
    return RateService(repository, source, today=lambda: TODAY, **kwargs)


def test_merge_ranges_joins_overlapping_and_adjacent():
    assert merge_ranges([(d(10), d(12)), (d(1), d(5)), (d(6), d(8)), (d(11), d(20))]) == [(d(1), d(8)), (d(10), d(20))]


def test_missing_ranges_returns_only_uncovered_parts():
    covered = [(d(5), d(10)), (d(20), d(25))]
    assert missing_ranges(d(1), d(30), covered) == [(d(1), d(4)), (d(11), d(19)), (d(26), d(30))]
    assert missing_ranges(d(6), d(9), covered) == []
    assert missing_ranges(d(1), d(3), covered) == [(d(1), d(3))]


def test_conversion_respects_unit_and_rounds_half_up():
    assert convert(Decimal("100"), RateQuote("JPY", d(2), 100, Decimal("2.455"))) == Decimal("2")
    assert convert(Decimal("1000"), RateQuote("JPY", d(2), 100, Decimal("2.455"))) == Decimal("25")
    assert convert(Decimal("1"), RateQuote("EUR", d(2), 1, Decimal("400.5"))) == Decimal("401")
    assert convert(Decimal("1"), RateQuote("EUR", d(2), 1, Decimal("400.4999"))) == Decimal("400")


async def test_weekend_uses_previous_banking_day_rate(repository):
    rates = service(repository, FakeSource())
    await rates.prepare({"EUR": (d(14, 3), d(14, 3))})

    saturday = d(14, 3)
    quote = await rates.rate_on("EUR", saturday)

    assert quote.rate_date == d(13, 3)
    assert quote.rate == Decimal("413")


async def test_no_rate_within_lookback_is_reported(repository):
    rates = service(repository, FakeSource())
    await rates.prepare({"EUR": (d(10, 3), d(10, 3))})
    with pytest.raises(MnbNoRateError):
        await rates.rate_on("EUR", d(10, 8))


async def test_second_request_for_covered_period_does_not_call_mnb(repository):
    source = FakeSource()
    rates = service(repository, source)
    await rates.prepare({"EUR": (d(10, 3), d(20, 3))})
    calls = len(source.calls)

    assert await rates.prepare({"EUR": (d(12, 3), d(18, 3))}) == {}
    assert len(source.calls) == calls


async def test_only_the_missing_period_is_fetched(repository):
    source = FakeSource()
    rates = service(repository, source, lookback_days=10)
    await rates.prepare({"EUR": (d(10, 3), d(20, 3))})
    source.calls.clear()

    await rates.prepare({"EUR": (d(10, 3), d(30, 3))})

    assert source.calls == [("EUR", d(21, 3), d(30, 3))]


async def test_parallel_requests_for_one_currency_fetch_once(repository):
    source = FakeSource(delay=0.05)
    rates = service(repository, source)
    await asyncio.gather(*(rates.prepare({"EUR": (d(10, 3), d(20, 3))}) for _ in range(5)))
    assert len(source.calls) == 1


async def test_currencies_are_fetched_separately_and_failures_are_isolated(repository):
    class Mixed(FakeSource):
        async def fetch_rates(self, currency, start, end):
            if currency == "CZK":
                raise MnbError("down")
            return await super().fetch_rates(currency, start, end)

    rates = service(repository, Mixed())
    failures = await rates.prepare({"EUR": (d(10, 3), d(10, 3)), "CZK": (d(10, 3), d(10, 3))})

    assert set(failures) == {"CZK"}
    assert (await rates.rate_on("EUR", d(10, 3))).rate == Decimal("410")
    with pytest.raises(MnbNoRateError):
        await rates.rate_on("CZK", d(10, 3))


async def test_failed_fetch_is_not_remembered_as_covered(repository):
    source = FakeSource(fail=MnbError("down"))
    rates = service(repository, source)
    assert "EUR" in await rates.prepare({"EUR": (d(10, 3), d(20, 3))})

    source.fail = None
    assert await rates.prepare({"EUR": (d(10, 3), d(20, 3))}) == {}
    assert (await rates.rate_on("EUR", d(20, 3))).rate_date == d(20, 3)


async def test_today_is_not_marked_covered_so_it_can_be_refetched(repository):
    source = FakeSource()
    rates = service(repository, source)
    await rates.prepare({"EUR": (TODAY, TODAY)})
    first = len(source.calls)

    await rates.prepare({"EUR": (TODAY, TODAY)})

    assert len(source.calls) == first + 1
    assert source.calls[-1][1:] == (TODAY, TODAY)


async def test_future_dates_are_not_requested(repository):
    source = FakeSource()
    rates = service(repository, source)
    await rates.prepare({"EUR": (date(2027, 1, 1), date(2027, 1, 31))})
    assert source.calls == []


async def test_unit_is_kept_with_stored_rate(repository):
    rates = service(repository, FakeSource(unit=100, rate="2.5"))
    await rates.prepare({"JPY": (d(10, 3), d(10, 3))})
    quote = await rates.rate_on("JPY", d(10, 3))
    assert (quote.unit, quote.rate) == (100, Decimal("12.5"))
