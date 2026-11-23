import pytest
from app_settings import COMPANY_SETTINGS, make_settings
from fastapi.testclient import TestClient

from app.core.config import ConfigError, load_settings, resolve_locale
from app.main import create_app

REQUIRED_VARIABLES = ("COMPANY_NAME", "COMPANY_TAX_NUMBER", "COMPANY_EU_VAT")


@pytest.fixture
def no_env_file(monkeypatch, tmp_path):
    """A `load_settings` a munkakönyvtár `.env` fájlját olvasná; egy üres könyvtárban fut, hogy az ne zavarjon."""
    monkeypatch.chdir(tmp_path)
    for name in (*REQUIRED_VARIABLES, "REPORT_CURRENCY"):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def company_env(no_env_file, monkeypatch):
    for key, value in COMPANY_SETTINGS.items():
        monkeypatch.setenv(key.upper(), value)


def test_company_settings_have_no_defaults(no_env_file):
    with pytest.raises(ConfigError) as raised:
        load_settings()

    for name in REQUIRED_VARIABLES:
        assert name in raised.value.message
    assert "REPORT_CURRENCY" not in raised.value.message
    assert ".env.example" in raised.value.message


def test_report_currency_and_locale_have_defaults(company_env, monkeypatch):
    monkeypatch.delenv("REPORT_CURRENCY")
    monkeypatch.delenv("DEFAULT_LOCALE", raising=False)

    settings, fallbacks = load_settings()

    assert (settings.report_currency, settings.default_locale, fallbacks) == ("HUF", "hu", [])


def test_business_settings_come_from_the_environment(company_env, monkeypatch):
    monkeypatch.setenv("COMPANY_NAME", "Other Company Ltd.")
    monkeypatch.setenv("REPORT_CURRENCY", " eur ")

    settings, fallbacks = load_settings()

    assert settings.company_name == "Other Company Ltd."
    assert settings.report_currency == "EUR"
    assert fallbacks == []


@pytest.mark.parametrize(
    "name, value",
    [
        ("COMPANY_NAME", "  "),
        ("COMPANY_TAX_NUMBER", "no digits"),
        ("COMPANY_EU_VAT", "26481937"),
    ],
)
def test_invalid_business_setting_stops_the_application(company_env, monkeypatch, name, value):
    monkeypatch.setenv(name, value)

    with pytest.raises(ConfigError) as raised:
        load_settings()

    assert name in raised.value.message


def test_eu_vat_may_contain_separators(company_env, monkeypatch):
    monkeypatch.setenv("COMPANY_EU_VAT", "hu 2648-1937")

    assert load_settings()[0].company_eu_vat == "hu 2648-1937"


@pytest.mark.parametrize(
    "name, value, field, default",
    [
        ("LLM_TIMEOUT_SECONDS", "abc", "llm_timeout_seconds", 60.0),
        ("LLM_CONCURRENCY", "0", "llm_concurrency", 4),
        ("PROCESSING_CONCURRENCY", "-2", "processing_concurrency", 4),
        ("PROCESSING_QUEUE_SIZE", "0", "processing_queue_size", 8),
        ("MAX_UPLOAD_MB", "0", "max_upload_mb", 20),
        ("MNB_LOOKBACK_DAYS", "-1", "mnb_lookback_days", 31),
        ("MNB_URL", "not a url", "mnb_url", "http://www.mnb.hu/arfolyamok.asmx"),
        ("DEFAULT_LOCALE", "hungarian!", "default_locale", "hu"),
        ("REPORT_CURRENCY", "EURO", "report_currency", "HUF"),
        ("OPENROUTER_MODEL", " ", "openrouter_model", "openai/gpt-4o-mini"),
    ],
)
def test_invalid_technical_setting_falls_back_to_default(company_env, monkeypatch, name, value, field, default):
    monkeypatch.setenv(name, value)

    settings, fallbacks = load_settings()

    assert getattr(settings, field) == default
    assert [(f.variable, f.value, f.default) for f in fallbacks] == [(name, value, str(default))]


def test_several_invalid_settings_fall_back_together(company_env, monkeypatch):
    monkeypatch.setenv("LLM_CONCURRENCY", "x")
    monkeypatch.setenv("MNB_TIMEOUT_SECONDS", "-5")

    settings, fallbacks = load_settings()

    assert (settings.llm_concurrency, settings.mnb_timeout_seconds) == (4, 20.0)
    assert {f.variable for f in fallbacks} == {"LLM_CONCURRENCY", "MNB_TIMEOUT_SECONDS"}


def test_invalid_business_setting_is_reported_even_with_technical_fallbacks(company_env, monkeypatch):
    monkeypatch.setenv("LLM_CONCURRENCY", "x")
    monkeypatch.delenv("COMPANY_NAME")

    with pytest.raises(ConfigError) as raised:
        load_settings()

    assert "COMPANY_NAME" in raised.value.message


def test_values_are_normalized(company_env, monkeypatch):
    monkeypatch.setenv("REPORT_CURRENCY", " chf ")
    monkeypatch.setenv("DEFAULT_LOCALE", "EN")

    settings, fallbacks = load_settings()

    assert (settings.report_currency, settings.default_locale, fallbacks) == ("CHF", "en", [])


def test_fallback_is_logged_at_startup(company_env, monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "app.db"))
    monkeypatch.setenv("LLM_CONCURRENCY", "lots")

    with TestClient(create_app()):
        pass

    output = capsys.readouterr().out
    assert "invalid setting LLM_CONCURRENCY='lots'" in output
    assert "using default '4'" in output


def test_environment_overrides_defaults(monkeypatch):
    monkeypatch.setenv("OPENROUTER_MODEL", "some/model")

    assert make_settings().openrouter_model == "some/model"


def test_llm_is_unavailable_without_key(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "secret")

    assert make_settings().llm_available is True
    assert make_settings(openrouter_api_key=None).llm_available is False


def test_api_key_is_not_leaked_in_repr(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "super-secret-key")

    assert "super-secret-key" not in repr(make_settings())

def test_existing_locale_is_kept():
    settings = make_settings(default_locale="en")

    assert resolve_locale(settings, ["en", "hu"]) == (settings, None)


def test_locale_without_file_falls_back_to_default():
    resolved, fallback = resolve_locale(make_settings(default_locale="xx"), ["hu", "ja"])

    assert resolved.default_locale == "hu"
    assert (fallback.variable, fallback.value, fallback.default) == ("DEFAULT_LOCALE", "xx", "hu")
    assert "hu, ja" in fallback.reason


def test_missing_default_locale_file_stops_the_application():
    with pytest.raises(ConfigError) as raised:
        resolve_locale(make_settings(default_locale="xx"), ["ja"])

    assert "DEFAULT_LOCALE='xx'" in raised.value.message


def test_unknown_locale_is_logged_and_replaced_at_startup(tmp_path, capsys):
    app = create_app(make_settings(database_path=tmp_path / "app.db", default_locale="xx"))

    with TestClient(app) as client:
        page = client.get("/")

    assert app.state.settings.default_locale == "hu"
    assert '<html lang="hu">' in page.text
    assert "invalid setting DEFAULT_LOCALE='xx'" in capsys.readouterr().out