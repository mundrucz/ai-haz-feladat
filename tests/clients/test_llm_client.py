import asyncio
import json

import httpx
import pytest
from app_settings import make_settings
from fastapi.testclient import TestClient
from pdfs import make_pdf

from app.clients.llm_client import (
    LlmAuthError,
    LlmClient,
    LlmError,
    LlmModelError,
    LlmQuotaError,
    LlmRateLimitedError,
    LlmResponseError,
    LlmUnavailableError,
)
from app.main import create_app

GOOD_ANSWER = {
    "invoice_number": "NW-2026-0001",
    "seller_name": "Nordwind Solutions Kft.",
    "seller_tax_id": "26481937-2-43",
    "buyer_name": "Balaton Tech Kft.",
    "buyer_tax_id": "12345678-2-42",
    "issue_date": "2026-01-12",
    "performance_date": "2026-01-11",
    "due_date": "2026-02-11",
    "currency": "HUF",
    "net_total": "1000.00",
    "vat_total": 270,
    "gross_total": "1270.00",
}
UNKNOWN_FORMAT_TEXT = (
    "Rechnung NW-2026-0001\n"
    "Aussteller: Nordwind Solutions Kft., USt-IdNr. 26481937-2-43\n"
    "Kunde: Balaton Tech Kft., Steuernr. 12345678-2-42\n"
    "Ausgestellt am 12. Januar 2026\n"
    "Leistung 11.01.2026, zahlbar bis 11.02.2026\n"
    "Netto 1 000,00 HUF\nMwSt 270,00 HUF\nGesamt 1270 HUF"
)


def chat_body(content) -> dict:
    return {"choices": [{"message": {"content": content if isinstance(content, str) else json.dumps(content)}}]}


def make_client(handler, **options) -> LlmClient:
    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    defaults = dict(api_key="test-key", model="m", base_url="https://llm.test/v1", timeout=5, retry_delay=0)
    return LlmClient(http, **{**defaults, **options})


# --- LlmClient ---------------------------------------------------------------


async def test_client_sends_json_chat_request_and_parses_answer():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["auth"] = request.headers["authorization"]
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json=chat_body("```json\n" + json.dumps({"a": 1}) + "\n```"))

    answer = await make_client(handler).complete_json("rendszer", "felhasználó")

    assert answer == {"a": 1}
    assert seen["url"] == "https://llm.test/v1/chat/completions"
    assert seen["auth"] == "Bearer test-key"
    assert seen["body"]["model"] == "m"
    assert seen["body"]["response_format"] == {"type": "json_object"}
    assert [m["role"] for m in seen["body"]["messages"]] == ["system", "user"]


@pytest.mark.parametrize(
    ("status", "error"),
    [
        (401, LlmAuthError),
        (403, LlmAuthError),
        (402, LlmQuotaError),
        (404, LlmModelError),
        (400, LlmError),
        (501, LlmUnavailableError),
    ],
)
async def test_client_maps_http_errors_without_retrying_permanent_ones(status, error):
    calls = []

    def handler(request):
        calls.append(1)
        return httpx.Response(status, json={"error": {"message": "nope"}})

    with pytest.raises(error) as raised:
        await make_client(handler).complete_json("s", "u")

    assert type(raised.value) is error
    assert len(calls) == 1


@pytest.mark.parametrize(
    ("status", "error"), [(408, LlmUnavailableError), (429, LlmRateLimitedError), (503, LlmUnavailableError)]
)
async def test_client_retries_transient_errors_then_gives_up(status, error):
    calls = []

    def handler(request):
        calls.append(1)
        return httpx.Response(status, json={})

    with pytest.raises(error):
        await make_client(handler, max_retries=2).complete_json("s", "u")

    assert len(calls) == 3


async def test_client_recovers_when_a_retry_succeeds():
    responses = [httpx.Response(429, json={}), httpx.Response(200, json=chat_body({"ok": True}))]

    answer = await make_client(lambda request: responses.pop(0)).complete_json("s", "u")

    assert answer == {"ok": True}


async def test_client_maps_timeout_and_connection_errors():
    def handler(request):
        raise httpx.ReadTimeout("slow")

    with pytest.raises(LlmUnavailableError):
        await make_client(handler, max_retries=0).complete_json("s", "u")


async def test_client_sends_separate_connect_and_read_timeouts():
    seen = {}

    def handler(request):
        seen.update(request.extensions["timeout"])
        return httpx.Response(200, json=chat_body({"ok": True}))

    await make_client(handler, timeout=15, connect_timeout=5).complete_json("s", "u")

    assert (seen["connect"], seen["read"]) == (5, 15)


async def test_client_does_not_retry_a_misconfigured_url():
    calls = []

    def handler(request):
        calls.append(1)
        raise httpx.UnsupportedProtocol("unknown scheme")

    with pytest.raises(LlmUnavailableError):
        await make_client(handler, max_retries=2).complete_json("s", "u")

    assert len(calls) == 1


async def test_client_treats_error_object_in_200_response_as_error():
    def handler(request):
        return httpx.Response(200, json={"error": {"code": 429, "message": "slow down"}})

    with pytest.raises(LlmRateLimitedError):
        await make_client(handler, max_retries=0).complete_json("s", "u")


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(200, text="<html>"),
        httpx.Response(200, json={"choices": []}),
        httpx.Response(200, json=chat_body("ez nem json")),
        httpx.Response(200, json=chat_body("[1, 2]")),
    ],
)
async def test_client_rejects_unusable_answers(response):
    with pytest.raises(LlmResponseError):
        await make_client(lambda request: response).complete_json("s", "u")


async def test_client_limits_concurrency():
    active = peak = 0

    async def handler(request):
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        await asyncio.sleep(0.01)
        active -= 1
        return httpx.Response(200, json=chat_body({}))

    client = make_client(handler, concurrency=2)
    await asyncio.gather(*(client.complete_json("s", "u") for _ in range(6)))

    assert peak == 2


# --- végponttól végpontig ----------------------------------------------------


def llm_app(tmp_path, handler) -> TestClient:
    settings = make_settings(database_path=tmp_path / "e2e.db")
    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return create_app(settings, http_client=http)


def upload_and_wait(client: TestClient, app, text: str) -> None:
    payload = [("files", ("ismeretlen.pdf", make_pdf(text), "application/pdf"))]
    assert client.post("/api/documents", files=payload).status_code == 200
    client.portal.call(asyncio.wait_for, app.state.processor.join(), 10)


def test_unknown_format_invoice_is_extracted_by_the_llm_and_listed(tmp_path):
    app = llm_app(tmp_path, lambda request: httpx.Response(200, json=chat_body(GOOD_ANSWER)))
    with TestClient(app) as client:
        upload_and_wait(client, app, UNKNOWN_FORMAT_TEXT)

        (invoice,) = client.get("/api/invoices").json()["invoices"]
        documents = client.get("/api/documents").json()

    assert invoice["gross_total"] == "1270.00"
    assert documents["documents"] == []


def test_file_the_llm_cannot_process_is_listed_with_its_error(tmp_path):
    app = llm_app(tmp_path, lambda request: httpx.Response(401, json={"error": {"message": "bad key"}}))
    with TestClient(app) as client:
        upload_and_wait(client, app, UNKNOWN_FORMAT_TEXT)

        invoices = client.get("/api/invoices").json()["invoices"]
        (failed,) = client.get("/api/documents").json()["documents"]

    assert invoices == []
    assert failed["filename"] == "ismeretlen.pdf"
    assert failed["status"] == "failed"
    assert failed["error"]["code"] == "llm_auth"
