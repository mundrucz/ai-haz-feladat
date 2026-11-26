"""MNB árfolyam-webszolgáltatás (SOAP) kliense: bármely deviza, bármely időszak."""

import asyncio
import logging
import re
import xml.etree.ElementTree as ET
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation

import httpx

from app.core.errors import AppError

logger = logging.getLogger(__name__)

_NAMESPACE = "http://www.mnb.hu/webservices/"
_CURRENCY = re.compile(r"[A-Z]{3}")
# Átmeneti állapotok: időtúllépés, rate limit, túlterhelt vagy épp nem elérhető szolgáltató.
# A többi hiba (pl. 400, 404, 501) ismétléskor is ugyanaz maradna.
_RETRYABLE_STATUS = frozenset({408, 425, 429, 500, 502, 503, 504})


class MnbError(AppError):
    """Az MNB-től nem sikerült árfolyamot kérni; az érintett számlánál jelenik meg."""

    code = "mnb_unavailable"
    status_code = 502


class MnbRejectedError(MnbError):
    """Az MNB érdemben elutasította a kérést (SOAP-hiba, 4xx, érvénytelen devizakód); újrapróbálni nem érdemes."""

    code = "mnb_rejected"

class MnbNoRateError(MnbRejectedError):
    code = "mnb_no_rate"


@dataclass(frozen=True, slots=True)
class RateQuote:
    """`unit` egység `currency` ára forintban az adott napon."""

    currency: str
    rate_date: date
    unit: int
    rate: Decimal


class MnbClient:
    """Nyers SOAP-kliens; a HTTP-klienst kívülről kapja (tesztben mockolható)."""

    def __init__(
        self,
        http: httpx.AsyncClient,
        *,
        url: str,
        timeout: float = 5.0,
        connect_timeout: float = 5.0,
        concurrency: int = 2,
        max_retries: int = 2,
        retry_delay: float = 1.0,
    ) -> None:
        self._http = http
        self._url = url
        self._timeout = httpx.Timeout(timeout, connect=connect_timeout)
        self._slots = asyncio.Semaphore(max(1, concurrency))
        self._max_retries = max_retries
        self._retry_delay = retry_delay

    async def fetch_rates(self, currencies: Sequence[str], start: date, end: date) -> list[RateQuote]:
        """A devizák napi árfolyamai az időszakban, egyetlen kéréssel; csak a jegyzési napok szerepelnek az eredményben."""
        names = list(dict.fromkeys(currencies))
        for currency in names:
            if not _CURRENCY.fullmatch(currency):
                raise MnbNoRateError(f"invalid currency code: {currency!r}")
        if not names:
            return []
        envelope = _envelope(names, start, end)
        async with self._slots:
            body = await self._post_with_retry(envelope, "GetExchangeRates")
        return _parse_rates(body, frozenset(names))

    async def fetch_currencies(self) -> frozenset[str]:
        """Az MNB által jegyzett devizák kódjai (GetCurrencies)."""
        async with self._slots:
            body = await self._post_with_retry(_currencies_envelope(), "GetCurrencies")
        return _parse_currencies(body)

    async def _post_with_retry(self, envelope: str, action: str) -> str:
        attempt = 0
        while True:
            try:
                return await self._post(envelope, action)
            except _Transient as exc:
                if attempt >= self._max_retries:
                    raise MnbError(str(exc)) from exc
                delay = self._retry_delay * 2**attempt
                attempt += 1
                logger.warning("mnb retry", extra={"attempt": attempt, "delay": delay})
                await asyncio.sleep(delay)

    async def _post(self, envelope: str, action: str) -> str:
        try:
            response = await self._http.post(
                self._url,
                content=envelope.encode("utf-8"),
                headers={
                    "Content-Type": "text/xml; charset=utf-8",
                    "SOAPAction": f'"{_NAMESPACE}MNBArfolyamServiceSoap/{action}"',
                },
                timeout=self._timeout,
            )
        except httpx.TimeoutException as exc:
            raise _Transient("MNB request timed out") from exc
        except (httpx.NetworkError, httpx.RemoteProtocolError) as exc:
            raise _Transient(f"MNB request failed: {type(exc).__name__}") from exc
        except httpx.HTTPError as exc:
            # Pl. nem támogatott protokoll a beállított URL-ben: ismétléssel sem javul.
            raise MnbError(f"MNB request failed: {type(exc).__name__}") from exc
        if fault := _soap_fault(response.text):
            raise MnbRejectedError(f"MNB error: {fault}")
        if response.status_code in _RETRYABLE_STATUS:
            raise _Transient(f"MNB request failed ({response.status_code})")
        if response.status_code >= 500:
            raise MnbError(f"MNB request failed ({response.status_code})")
        if response.status_code >= 400:
            raise MnbRejectedError(f"MNB request failed ({response.status_code})")
        return response.text


class _Transient(Exception):
    pass


def _envelope(currencies: Sequence[str], start: date, end: date) -> str:
    return (
        '<?xml version="1.0" encoding="utf-8"?>'
        '<soap:Envelope xmlns:soap="http://schemas.xmlsoap.org/soap/envelope/" '
        f'xmlns:web="{_NAMESPACE}"><soap:Body><web:GetExchangeRates>'
        f"<web:startDate>{start.isoformat()}</web:startDate>"
        f"<web:endDate>{end.isoformat()}</web:endDate>"
        f"<web:currencyNames>{','.join(currencies)}</web:currencyNames>"
        "</web:GetExchangeRates></soap:Body></soap:Envelope>"
    )


def _currencies_envelope() -> str:
    return (
        '<?xml version="1.0" encoding="utf-8"?>'
        '<soap:Envelope xmlns:soap="http://schemas.xmlsoap.org/soap/envelope/" '
        f'xmlns:web="{_NAMESPACE}"><soap:Body><web:GetCurrencies/></soap:Body></soap:Envelope>'
    )


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _parse_xml(text: str) -> ET.Element:
    try:
        return ET.fromstring(text)
    except ET.ParseError as exc:
        raise MnbError("MNB response is not valid XML") from exc


def _soap_fault(text: str) -> str | None:
    if "faultstring" not in text:
        return None
    try:
        root = ET.fromstring(text)
    except ET.ParseError:
        return None
    for element in root.iter():
        if _local(element.tag) == "faultstring":
            return (element.text or "unknown fault").strip()[:500]
    return None


def _parse_currencies(body: str) -> frozenset[str]:
    envelope = _parse_xml(body)
    result = next((e for e in envelope.iter() if _local(e.tag) == "GetCurrenciesResult"), None)
    if result is None:
        raise MnbError("MNB response has no GetCurrenciesResult")
    inner_text = (result.text or "").strip()
    codes = (
        frozenset(
            code
            for element in _parse_xml(inner_text).iter()
            if _local(element.tag) == "Curr" and _CURRENCY.fullmatch(code := (element.text or "").strip())
        )
        if inner_text
        else frozenset()
    )
    if not codes:
        raise MnbError("MNB response lists no currencies")
    return codes


def _parse_rates(body: str, currencies: frozenset[str]) -> list[RateQuote]:
    envelope = _parse_xml(body)
    result = next((e for e in envelope.iter() if _local(e.tag) == "GetExchangeRatesResult"), None)
    if result is None:
        raise MnbError("MNB response has no GetExchangeRatesResult")
    inner_text = (result.text or "").strip()
    if not inner_text:
        return []
    quotes: list[RateQuote] = []
    for day in _parse_xml(inner_text).iter():
        if _local(day.tag) != "Day":
            continue
        try:
            rate_date = date.fromisoformat(day.get("date", ""))
        except ValueError:
            continue
        for rate in day:
            currency = rate.get("curr", "")
            if _local(rate.tag) != "Rate" or currency not in currencies or not (rate.text or "").strip():
                continue
            try:
                value = Decimal(rate.text.strip().replace(",", "."))
                unit = int(rate.get("unit", "1"))
            except (InvalidOperation, ValueError):
                continue
            if unit > 0 and value > 0:
                quotes.append(RateQuote(currency, rate_date, unit, value))
    return quotes
