from typing import Any

from app.core.config import Settings

# A tesztek kötelező beállításai: az alkalmazásnak nincs alapértelmezett cége és LLM-kulcsa, ezért minden
# tesztbeállítás ezeket kapja. A kulcs kitalált; a tesztek a HTTP-t mockolják, valódi LLM-et nem hívnak.
REQUIRED_SETTINGS: dict[str, Any] = {
    "openrouter_api_key": "test-key",
    "company_name": "Nordwind Solutions Kft.",
    "company_tax_number": "26481937-2-43",
    "company_eu_vat": "HU26481937",
    "report_currency": "HUF",
}


def make_settings(**overrides: Any) -> Settings:
    """Tesztbeállítás `.env` fájl nélkül, a kötelező adatokkal kitöltve."""
    return Settings(_env_file=None, **{**REQUIRED_SETTINGS, **overrides})
