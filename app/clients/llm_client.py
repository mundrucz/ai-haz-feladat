"""OpenAI-kompatibilis (OpenRouter) LLM-kliens.

Aszinkron, korlátozott párhuzamosságú, csak átmeneti hibánál próbál újra, és a hibákat kódokra képezi le.
"""

import asyncio
import json
import logging
import re
from typing import Protocol

import httpx

from app.core.errors import AppError

logger = logging.getLogger(__name__)

# Átmeneti állapotok: időtúllépés, rate limit, túlterhelt vagy épp nem elérhető szolgáltató.
# A többi hiba (pl. 400, 401, 402, 404, 501) ismétléskor is ugyanaz maradna.
_RETRYABLE_STATUS = frozenset({408, 425, 429, 500, 502, 503, 504})
_FENCE = re.compile(r"^```(?:json)?\s*(.*?)\s*```$", re.DOTALL)


class LlmError(AppError):
    """Az LLM-hívás nem adott használható választ; a hiba a feldolgozott dokumentumnál jelenik meg."""

    code = "llm_error"
    status_code = 502

    def __init__(
        self, message: str, *, code: str | None = None, fields: tuple[str, ...] = (), retryable: bool = False
    ) -> None:
        super().__init__(message, code=code)
        self.fields = fields
        self.retryable = retryable


class LlmUnavailableError(LlmError):
    code = "llm_unavailable"


class LlmRateLimitedError(LlmError):
    code = "llm_rate_limited"


class LlmAuthError(LlmError):
    code = "llm_auth"


class LlmQuotaError(LlmError):
    code = "llm_quota"


class LlmModelError(LlmError):
    code = "llm_model_unavailable"


class LlmResponseError(LlmError):
    code = "llm_invalid_response"


class JsonChat(Protocol):
    async def complete_json(self, system: str, user: str) -> dict: ...


def _error_for_status(status: int, detail: str) -> LlmError:
    message = f"LLM request failed ({status}): {detail}" if detail else f"LLM request failed ({status})"
    retryable = status in _RETRYABLE_STATUS
    if status in (401, 403):
        return LlmAuthError(message)
    if status == 402:
        return LlmQuotaError(message)
    if status == 404:
        return LlmModelError(message)
    if status == 429:
        return LlmRateLimitedError(message, retryable=retryable)
    if status == 408 or status >= 500:
        return LlmUnavailableError(message, retryable=retryable)
    return LlmError(message, retryable=retryable)


def _error_detail(body: object) -> str:
    error = body.get("error") if isinstance(body, dict) else None
    if isinstance(error, dict):
        return str(error.get("message") or "")[:1000]
    return ""


class LlmClient:
    """Egy modellre kötött kliens; a HTTP-klienst kívülről kapja (tesztben mockolható)."""

    def __init__(
        self,
        http: httpx.AsyncClient,
        *,
        api_key: str,
        model: str,
        base_url: str,
        timeout: float = 20.0,
        concurrency: int = 4,
        max_retries: int = 2,
        retry_delay: float = 1.0,
    ) -> None:
        self._http = http
        self._api_key = api_key
        self._model = model
        self._url = f"{base_url.rstrip('/')}/chat/completions"
        self._timeout = timeout
        self._slots = asyncio.Semaphore(max(1, concurrency))
        self._max_retries = max_retries
        self._retry_delay = retry_delay

    async def complete_json(self, system: str, user: str) -> dict:
        payload = {
            "model": self._model,
            "temperature": 0,
            "response_format": {"type": "json_object"},
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
        }
        async with self._slots:
            body = await self._post_with_retry(payload)
        return _parse_content(body)

    async def _post_with_retry(self, payload: dict) -> dict:
        attempt = 0
        while True:
            try:
                return await self._post(payload)
            except LlmError as exc:
                if not exc.retryable or attempt >= self._max_retries:
                    raise
                delay = self._retry_delay * 2**attempt
                attempt += 1
                logger.warning("llm retry", extra={"attempt": attempt, "code": exc.code, "delay": delay})
                await asyncio.sleep(delay)

    async def _post(self, payload: dict) -> dict:
        try:
            response = await self._http.post(
                self._url,
                json=payload,
                headers={"Authorization": f"Bearer {self._api_key}"},
                timeout=self._timeout,
            )
        except httpx.TimeoutException as exc:
            raise LlmUnavailableError("LLM request timed out", retryable=True) from exc
        except (httpx.NetworkError, httpx.RemoteProtocolError) as exc:
            raise LlmUnavailableError(f"LLM request failed: {type(exc).__name__}", retryable=True) from exc
        except httpx.HTTPError as exc:
            # Pl. nem támogatott protokoll a beállított URL-ben: ismétléssel sem javul.
            raise LlmUnavailableError(f"LLM request failed: {type(exc).__name__}") from exc
        try:
            body = response.json()
        except ValueError:
            body = None
        if response.status_code >= 400:
            raise _error_for_status(response.status_code, _error_detail(body))
        if not isinstance(body, dict):
            raise LlmResponseError("LLM response is not JSON")
        if "error" in body:
            code = body["error"].get("code") if isinstance(body["error"], dict) else None
            raise _error_for_status(code if isinstance(code, int) else 502, _error_detail(body))
        return body


def _parse_content(body: dict) -> dict:
    try:
        content = body["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as exc:
        raise LlmResponseError("LLM response has no message content") from exc
    if not isinstance(content, str):
        raise LlmResponseError("LLM message content is not text")
    text = content.strip()
    if fenced := _FENCE.match(text):
        text = fenced.group(1)
    try:
        parsed = json.loads(text)
    except ValueError as exc:
        raise LlmResponseError("LLM answer is not valid JSON") from exc
    if not isinstance(parsed, dict):
        raise LlmResponseError("LLM answer is not a JSON object")
    return parsed
