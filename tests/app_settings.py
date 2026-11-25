from typing import Any

from app.core.config import Settings

# A tesztek saját cége; az alkalmazásnak nincs alapértelmezett cége, ezért minden tesztbeállítás ezt kapja.
COMPANY_SETTINGS: dict[str, Any] = {
    "company_name": "Nordwind Solutions Kft.",
    "company_tax_number": "26481937-2-43",
    "company_eu_vat": "HU26481937",
    "report_currency": "HUF",
}


def make_settings(**overrides: Any) -> Settings:
    """Tesztbeállítás `.env` fájl nélkül, a kötelező üzleti adatokkal kitöltve."""
    return Settings(_env_file=None, **{**COMPANY_SETTINGS, **overrides})
