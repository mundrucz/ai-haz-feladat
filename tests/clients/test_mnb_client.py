from datetime import date
from decimal import Decimal
from xml.sax.saxutils import escape

import httpx
import pytest

from app.mnb import MnbClient, MnbError, MnbNoRateError


def soap_response(days: dict[str, list[tuple[str, int, str]]]) -> str:
    """Az MNB válaszához hasonló SOAP-boríték: az eredmény escape-elt XML-szöveg."""
    inner = "".join(
        f'<Day date="{day}">'
        + "".join(f'<Rate unit="{unit}" curr="{curr}">{value}</Rate>' for curr, unit, value in rates)
        + "</Day>"
        for day, rates in days.items()
    )
    result = f"<MNBExchangeRates>{inner}</MNBExchangeRates>" if days else "<MNBExchangeRates />"
    return (
        '<?xml version="1.0" encoding="utf-8"?>'
        '<soap:Envelope xmlns:soap="http://schemas.xmlsoap.org/soap/envelope/"><soap:Body>'
        '<GetExchangeRatesResponse xmlns="http://www.mnb.hu/webservices/">'
        f"<GetExchangeRatesResult>{escape(result)}</GetExchangeRatesResult>"
        "</GetExchangeRatesResponse></soap:Body></soap:Envelope>"
    )


def client_for(handler, **kwargs) -> MnbClient:
    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return MnbClient(http, url="http://mnb.test/arfolyamok.asmx", retry_delay=0, **kwargs)


async def test_parses_decimal_comma_unit_and_skips_other_currencies():
    body = soap_response(
        {
            "2026-01-02": [("EUR", 1, "390,52"), ("JPY", 100, "245,1")],
            "2026-01-05": [("EUR", 1, "391,00")],
        }
    )
    client = client_for(lambda request: httpx.Response(200, text=body))

    eur = await client.fetch_rates("EUR", date(2026, 1, 1), date(2026, 1, 5))
    jpy = await client.fetch_rates("JPY", date(2026, 1, 1), date(2026, 1, 5))

    assert [(q.rate_date, q.unit, q.rate) for q in eur] == [
        (date(2026, 1, 2), 1, Decimal("390.52")),
        (date(2026, 1, 5), 1, Decimal("391.00")),
    ]
    assert [(q.unit, q.rate) for q in jpy] == [(100, Decimal("245.1"))]


async def test_request_names_currency_and_period():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = request.content.decode()
        seen["action"] = request.headers["SOAPAction"]
        return httpx.Response(200, text=soap_response({}))

    client = client_for(handler)
    assert await client.fetch_rates("CHF", date(2026, 3, 1), date(2026, 3, 31)) == []
    assert "<web:currencyNames>CHF</web:currencyNames>" in seen["body"]
    assert "2026-03-01" in seen["body"] and "2026-03-31" in seen["body"]
    assert seen["action"].endswith('GetExchangeRates"')


async def test_invalid_currency_code_is_never_sent():
    calls = []
    client = client_for(lambda request: calls.append(request) or httpx.Response(200, text=""))
    with pytest.raises(MnbNoRateError):
        await client.fetch_rates("EUR</web:currencyNames>", date(2026, 1, 1), date(2026, 1, 2))
    assert calls == []


async def test_server_error_is_retried_then_succeeds():
    attempts = []

    def handler(request: httpx.Request) -> httpx.Response:
        attempts.append(1)
        if len(attempts) < 3:
            return httpx.Response(503, text="busy")
        return httpx.Response(200, text=soap_response({"2026-01-02": [("EUR", 1, "390,5")]}))

    client = client_for(handler)
    quotes = await client.fetch_rates("EUR", date(2026, 1, 1), date(2026, 1, 2))
    assert len(quotes) == 1 and len(attempts) == 3


async def test_persistent_failure_raises_after_retries():
    attempts = []

    def handler(request: httpx.Request) -> httpx.Response:
        attempts.append(1)
        raise httpx.ConnectTimeout("slow")

    client = client_for(handler, max_retries=2)
    with pytest.raises(MnbError):
        await client.fetch_rates("EUR", date(2026, 1, 1), date(2026, 1, 2))
    assert len(attempts) == 3


async def test_soap_fault_is_an_error_and_not_retried():
    attempts = []
    fault = (
        '<soap:Envelope xmlns:soap="http://schemas.xmlsoap.org/soap/envelope/"><soap:Body><soap:Fault>'
        "<faultstring>bad request</faultstring></soap:Fault></soap:Body></soap:Envelope>"
    )

    def handler(request: httpx.Request) -> httpx.Response:
        attempts.append(1)
        return httpx.Response(500, text=fault)

    client = client_for(handler)
    with pytest.raises(MnbError, match="bad request"):
        await client.fetch_rates("EUR", date(2026, 1, 1), date(2026, 1, 2))
    assert len(attempts) == 1


async def test_malformed_response_is_an_error():
    client = client_for(lambda request: httpx.Response(200, text="<html>nope"))
    with pytest.raises(MnbError):
        await client.fetch_rates("EUR", date(2026, 1, 1), date(2026, 1, 2))
