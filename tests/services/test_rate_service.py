import asyncio
from datetime import date, timedelta
from decimal import Decimal

import pytest

from app.clients.mnb_client import MnbError, MnbNoRateError, MnbRateNotYetError, MnbRejectedError, RateQuote
from app.db.database import Database
from app.repositories.rate_repository import RateRepository, merge_ranges
from app.services.rate_service import RateService, convert, missing_ranges

TODAY = date(2026, 6, 15)


def d(day: int, month: int = 1) -> date:
    return date(2026, month, day)


class FakeSource:
    """Hétköznapokra ad árfolyamot, a hívásokat rögzíti."""

    def __init__(self, unit: int = 1, rate: str = "400", delay: float = 0, fail: Exception | None = None) -> None:
        self.calls: list[tuple[str, date, date]] = []
        self.currency_calls = 0
        self.currencies: frozenset[str] = frozenset({"HUF", "EUR", "USD"})
        self.currencies_fail: Exception | None = None
        self.unit, self.rate, self.delay, self.fail = unit, Decimal(rate), delay, fail

    async def fetch_currencies(self) -> frozenset[str]:
        self.currency_calls += 1
        if self.currencies_fail:
            raise self.currencies_fail
        return self.currencies

    async def fetch_rates(self, currencies, start: date, end: date) -> list[RateQuote]:
        self.calls.append((tuple(currencies), start, end))
        await asyncio.sleep(self.delay)
        if self.fail:
            raise self.fail
        quotes, day = [], start
        while day <= end:
            if day.weekday() < 5:
                quotes.extend(RateQuote(c, day, self.unit, self.rate + day.day) for c in currencies)
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


async def test_far_future_day_reports_rate_not_yet(repository):
    source = FakeSource()
    rates = service(repository, source)
    far_future = TODAY + timedelta(days=32)

    assert await rates.prepare({"EUR": (far_future, far_future)}) == {}
    with pytest.raises(MnbRateNotYetError):
        await rates.rate_on("EUR", far_future)
    assert source.calls == []


async def test_near_future_day_uses_latest_rate(repository):
    rates = service(repository, FakeSource())
    near_future = TODAY + timedelta(days=31)
    await rates.prepare({"EUR": (near_future, near_future)})

    quote = await rates.rate_on("EUR", near_future)

    assert quote.rate_date == d(15, 6)


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

    assert source.calls == [(("EUR",), d(21, 3), d(30, 3))]


async def test_parallel_requests_for_one_currency_fetch_once(repository):
    source = FakeSource(delay=0.05)
    rates = service(repository, source)
    await asyncio.gather(*(rates.prepare({"EUR": (d(10, 3), d(20, 3))}) for _ in range(5)))
    assert len(source.calls) == 1


async def test_long_period_is_split_into_requests_of_limited_length(repository):
    source = FakeSource()
    rates = service(repository, source, lookback_days=0, max_request_days=10)

    assert await rates.prepare({"EUR": (d(1, 3), d(25, 3))}) == {}

    assert source.calls == [
        (("EUR",), d(1, 3), d(10, 3)),
        (("EUR",), d(11, 3), d(20, 3)),
        (("EUR",), d(21, 3), d(25, 3)),
    ]
    assert (await rates.rate_on("EUR", d(25, 3))).rate_date == d(25, 3)


async def test_chunks_without_any_missing_day_are_skipped(repository):
    source = FakeSource()
    rates = service(repository, source, lookback_days=0, max_request_days=10)

    await rates.prepare({"EUR": (d(1, 3), d(3, 3)), "USD": (d(1, 5), d(3, 5))})

    assert source.calls == [(("EUR",), d(1, 3), d(10, 3)), (("USD",), d(30, 4), d(3, 5))]


async def test_failed_currency_is_not_requested_again_in_later_chunks(repository):
    source = FakeSource(fail=MnbError("down"))
    rates = service(repository, source, lookback_days=0, max_request_days=10)

    failures = await rates.prepare({"EUR": (d(1, 3), d(25, 3))})

    assert set(failures) == {"EUR"}
    assert len(source.calls) == 1


async def test_all_missing_currencies_are_fetched_with_one_request(repository):
    source = FakeSource()
    rates = service(repository, source, lookback_days=0)

    failures = await rates.prepare({"EUR": (d(10, 3), d(12, 3)), "USD": (d(11, 3), d(20, 3)), "JPY": (d(5, 3), d(6, 3))})

    assert failures == {}
    assert source.calls == [(("EUR", "JPY", "USD"), d(5, 3), d(20, 3))]
    assert (await rates.rate_on("JPY", d(5, 3))).rate == Decimal("405")
    assert (await rates.rate_on("USD", d(20, 3))).rate == Decimal("420")


async def test_already_stored_currencies_are_left_out_of_the_request(repository):
    source = FakeSource()
    rates = service(repository, source, lookback_days=0)
    await rates.prepare({"EUR": (d(10, 3), d(12, 3))})
    source.calls.clear()

    await rates.prepare({"EUR": (d(10, 3), d(12, 3)), "USD": (d(10, 3), d(12, 3))})

    assert source.calls == [(("USD",), d(10, 3), d(12, 3))]


async def test_parallel_requests_for_different_currencies_do_not_repeat_fetches(repository):
    source = FakeSource(delay=0.05)
    rates = service(repository, source)
    await asyncio.gather(
        rates.prepare({"EUR": (d(10, 3), d(20, 3)), "USD": (d(10, 3), d(20, 3))}),
        rates.prepare({"EUR": (d(10, 3), d(20, 3)), "USD": (d(10, 3), d(20, 3))}),
        rates.prepare({"USD": (d(12, 3), d(18, 3))}),
    )
    assert len(source.calls) == 1


async def test_rejected_currency_does_not_take_the_others_down(repository):
    class Picky(FakeSource):
        async def fetch_rates(self, currencies, start, end):
            if "CZK" in currencies:
                raise MnbRejectedError("unknown currency")
            return await super().fetch_rates(currencies, start, end)

    source = Picky()
    rates = service(repository, source)
    failures = await rates.prepare({"EUR": (d(10, 3), d(10, 3)), "CZK": (d(10, 3), d(10, 3))})

    assert set(failures) == {"CZK"}
    assert failures["CZK"].code == "mnb_rejected"
    assert (await rates.rate_on("EUR", d(10, 3))).rate == Decimal("410")
    with pytest.raises(MnbNoRateError):
        await rates.rate_on("CZK", d(10, 3))


async def test_mnb_outage_is_one_request_and_fails_every_currency(repository):
    source = FakeSource(fail=MnbError("down"))
    rates = service(repository, source)

    failures = await rates.prepare({"EUR": (d(10, 3), d(10, 3)), "USD": (d(10, 3), d(10, 3))})

    assert set(failures) == {"EUR", "USD"}
    assert len(source.calls) == 1


async def test_quoted_currencies_come_from_the_mnb_list_and_are_cached(repository):
    source = FakeSource()
    rates = service(repository, source)

    assert await rates.is_quoted("EUR") is True
    assert await rates.is_quoted("XTS") is False
    assert await asyncio.gather(*(rates.is_quoted("USD") for _ in range(5))) == [True] * 5

    assert source.currency_calls == 1


async def test_currency_list_is_refreshed_after_it_expires(repository):
    source = FakeSource()
    await service(repository, source).is_quoted("EUR")
    source.currencies = frozenset({"HUF", "CZK"})
    later = RateService(repository, source, today=lambda: TODAY + timedelta(days=7))

    assert await later.is_quoted("CZK") is True
    assert await later.is_quoted("EUR") is False
    assert source.currency_calls == 2


async def test_stale_currency_list_is_used_when_mnb_is_down(repository):
    source = FakeSource()
    await service(repository, source).is_quoted("EUR")
    source.currencies_fail = MnbError("down")
    later = RateService(repository, source, today=lambda: TODAY + timedelta(days=30))

    assert await later.is_quoted("EUR") is True


async def test_unknown_support_when_mnb_is_down_and_nothing_is_stored(repository):
    source = FakeSource()
    source.currencies_fail = MnbError("down")
    now = [0.0]
    rates = service(repository, source, retry_seconds=3600, clock=lambda: now[0])

    assert await rates.is_quoted("EUR") is None
    assert await rates.is_quoted("EUR") is None
    assert source.currency_calls == 1  # a sikertelen lekérést nem ismételjük azonnal

    now[0] = 3601
    source.currencies_fail = None
    assert await rates.is_quoted("EUR") is True


async def test_failed_fetch_is_not_remembered_as_covered(repository):
    source = FakeSource(fail=MnbError("down"))
    now = [0.0]
    rates = service(repository, source, retry_seconds=3600, clock=lambda: now[0])
    assert "EUR" in await rates.prepare({"EUR": (d(10, 3), d(20, 3))})

    now[0] = 3601
    source.fail = None
    assert await rates.prepare({"EUR": (d(10, 3), d(20, 3))}) == {}
    assert (await rates.rate_on("EUR", d(20, 3))).rate_date == d(20, 3)


async def test_unreachable_mnb_is_retried_only_after_the_retry_interval(repository):
    source = FakeSource(fail=MnbError("down"))
    now = [0.0]
    rates = service(repository, source, retry_seconds=3600, clock=lambda: now[0])
    await rates.prepare({"EUR": (d(10, 3), d(20, 3))})

    now[0] = 3599
    failures = await rates.prepare({"EUR": (d(10, 3), d(20, 3)), "USD": (d(10, 3), d(20, 3))})

    assert set(failures) == {"EUR", "USD"}
    assert failures["EUR"].code == "mnb_unavailable"
    assert len(source.calls) == 1

    now[0] = 3600
    source.fail = None
    assert await rates.prepare({"EUR": (d(10, 3), d(20, 3))}) == {}
    assert len(source.calls) == 2


async def test_rejected_currency_does_not_pause_fetching(repository):
    source = FakeSource(fail=MnbRejectedError("unknown currency"))
    rates = service(repository, source, retry_seconds=3600, clock=lambda: 0.0)
    await rates.prepare({"CZK": (d(10, 3), d(10, 3))})

    source.fail = None
    assert await rates.prepare({"EUR": (d(10, 3), d(10, 3))}) == {}


async def test_stored_rate_is_trusted_only_when_every_day_up_to_the_date_was_fetched(repository):
    rates = service(repository, FakeSource(), lookback_days=0)
    await rates.prepare({"EUR": (d(9, 3), d(13, 3))})  # hétfő-péntek

    assert (await rates.stored_rate_on("EUR", d(13, 3))).rate_date == d(13, 3)
    assert await rates.stored_rate_on("EUR", d(16, 3)) is None  # 14-16. nincs lekérve


async def test_stored_rate_falls_back_to_the_last_quote_when_the_gap_days_were_fetched(repository):
    rates = service(repository, FakeSource())
    await rates.prepare({"EUR": (d(15, 3), d(15, 3))})  # vasárnap: a péntek 13-i jegyzés érvényes

    assert (await rates.stored_rate_on("EUR", d(15, 3))).rate_date == d(13, 3)


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
