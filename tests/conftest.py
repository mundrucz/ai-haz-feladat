import logging
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from app_settings import make_settings
from app.core.config import Settings
from app.main import create_app


@pytest.fixture
def failed_log():
    """A hibás fájlok naplójának rekordjai (a `caplog` nem érhető el, mert a pytest naplózó bővítménye ki van kapcsolva)."""
    logger = logging.getLogger("app.failed_files")
    records: list[logging.LogRecord] = []

    class Collector(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            records.append(record)

    handler, level = Collector(), logger.level
    logger.setLevel(logging.INFO)
    logger.addHandler(handler)
    yield records
    logger.removeHandler(handler)
    logger.setLevel(level)


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return make_settings(database_path=tmp_path / "test.db")


def _rejecting_http(request: httpx.Request) -> httpx.Response:
    return httpx.Response(401, json={"error": {"message": "tests never call a real service"}})


@pytest.fixture
def app(settings: Settings):
    """Hálózat nélkül: minden külső hívás (LLM, MNB) 401-et kap, így a szöveges PDF-ek `llm_auth` hibát kapnak."""
    return create_app(settings, http_client=httpx.AsyncClient(transport=httpx.MockTransport(_rejecting_http)))


@pytest.fixture
def client(app):
    with TestClient(app, raise_server_exceptions=False) as test_client:
        yield test_client
