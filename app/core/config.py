import re
from collections.abc import Collection
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Any

from pydantic import (
    AfterValidator,
    BeforeValidator,
    NonNegativeInt,
    PositiveFloat,
    PositiveInt,
    SecretStr,
    StringConstraints,
    ValidationError,
)
from pydantic_settings import BaseSettings, SettingsConfigDict

from app.core.errors import AppError


def _strip_upper(value: object) -> object:
    return value.strip().upper() if isinstance(value, str) else value


def _strip_lower(value: object) -> object:
    return value.strip().lower() if isinstance(value, str) else value


def _has_digit(value: str) -> str:
    if not any(char.isdigit() for char in value):
        raise ValueError("must contain at least one digit")
    return value


def _eu_vat_format(value: str) -> str:
    if not re.fullmatch(r"[A-Z]{2}[0-9A-Z]{2,13}", re.sub(r"[\s.\-]", "", value.upper())):
        raise ValueError("must be a country code followed by the VAT number, e.g. HU12345678")
    return value


def _non_blank_secret(value: SecretStr) -> SecretStr:
    stripped = value.get_secret_value().strip()
    if not stripped:
        raise ValueError("must not be empty")
    return SecretStr(stripped)


Text = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
ApiKey = Annotated[SecretStr, AfterValidator(_non_blank_secret)]
HttpUrl = Annotated[str, StringConstraints(strip_whitespace=True, pattern=r"^https?://\S+$")]
CurrencyCode = Annotated[str, BeforeValidator(_strip_upper), StringConstraints(pattern=r"^[A-Z]{3}$")]
TaxNumber = Annotated[Text, AfterValidator(_has_digit)]
EuVatNumber = Annotated[Text, AfterValidator(_eu_vat_format)]
Locale = Annotated[str, BeforeValidator(_strip_lower), StringConstraints(pattern=r"^[a-z]{2,3}([-_][a-z0-9]+)?$")]


class ConfigError(AppError):
    code = "config_error"


@dataclass(frozen=True)
class ConfigFallback:
    """Egy érvénytelen beállítás, amely helyett az alapértéket használjuk."""

    variable: str
    value: str
    reason: str
    default: str


class Settings(BaseSettings):
    """Az alkalmazás teljes konfigurációja környezeti változókból.

    A saját cég adatainak szándékosan nincs alapértéke: egy másik cég
    adataival csendben rossz irányt kapna minden számla, ezért ezek hiányában az alkalmazás nem indul el.
    Az LLM-kulcs is kötelező: nélküle egyetlen számla sem dolgozható fel.
    A többi mező érvénytelen értéke helyett a `load_settings` az alapértéket használja.
    """

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    openrouter_api_key: ApiKey
    openrouter_model: Text = "openai/gpt-4o-mini"
    openrouter_base_url: HttpUrl = "https://openrouter.ai/api/v1"
    llm_timeout_seconds: PositiveFloat = 60.0
    llm_concurrency: PositiveInt = 4

    company_name: Text
    company_tax_number: TaxNumber
    company_eu_vat: EuVatNumber

    mnb_url: HttpUrl = "http://www.mnb.hu/arfolyamok.asmx"
    mnb_timeout_seconds: PositiveFloat = 20.0
    mnb_concurrency: PositiveInt = 2
    # Ennyi nappal korábbi jegyzésig keresünk árfolyamot (hétvége, ünnepnap miatt); 0: csak a pontos nap
    mnb_lookback_days: NonNegativeInt = 31
    # Ennyi napig használjuk a tárolt, MNB által jegyzett devizalistát, utána újra lekérjük
    mnb_currencies_ttl_days: PositiveInt = 7

    report_currency: CurrencyCode = "HUF"
    default_locale: Locale = "hu"

    database_path: Path = Path("data/app.db")
    max_upload_mb: PositiveInt = 20
    processing_concurrency: PositiveInt = 4
    processing_queue_size: PositiveInt = 8

    @property
    def failed_files_log_path(self) -> Path:
        """A hibás fájlok naplója az adatbázis mellett, így Dockerben is a volume-on marad."""
        return self.database_path.parent / "logs" / "failed_files.log"

    @property
    def max_upload_bytes(self) -> int:
        return self.max_upload_mb * 1024 * 1024


def _shown_value(value: Any) -> str:
    text = str(value)
    return text if len(text) <= 80 else text[:77] + "..."


def load_settings() -> tuple[Settings, list[ConfigFallback]]:
    """Betölti a beállításokat a környezetből és a `.env`-ből.

    Érvénytelen érték esetén az alapértéket használja, és visszaadja, mit cserélt le (a hívó naplózza,
    mert a betöltéskor a naplózás még nincs beállítva). Ha egy alapérték nélküli (kötelező) mező hiányzik
    vagy érvénytelen, `ConfigError`-t dob a környezeti változók nevével.
    """
    fallbacks: list[ConfigFallback] = []
    forced: dict[str, Any] = {}
    while True:
        try:
            return Settings(**forced), fallbacks
        except ValidationError as exc:
            missing: list[str] = []
            for error in exc.errors():
                field = str(error["loc"][0])
                info = Settings.model_fields.get(field)
                if info is None or info.is_required() or field in forced:
                    missing.append(f"{field.upper()} ({error['msg']})")
                    continue
                default = info.get_default(call_default_factory=True)
                forced[field] = default
                fallbacks.append(
                    ConfigFallback(field.upper(), _shown_value(error["input"]), error["msg"], str(default))
                )
            if missing:
                raise ConfigError(
                    f"invalid or missing settings: {', '.join(missing)}; "
                    "copy .env.example to .env and fill in the values"
                ) from exc


def resolve_locale(settings: Settings, available: Collection[str]) -> tuple[Settings, ConfigFallback | None]:
    """Ha a `DEFAULT_LOCALE`-hoz nincs nyelvi fájl, az alapnyelvre vált (különben a felületen nyers kulcsok látszanának).

    Ha az alapnyelv fájlja is hiányzik, `ConfigError`-t dob.
    """
    if settings.default_locale in available:
        return settings, None
    default = Settings.model_fields["default_locale"].get_default()
    listed = ", ".join(sorted(available)) or "none"
    if default not in available:
        raise ConfigError(f"no locale file for DEFAULT_LOCALE={settings.default_locale!r}; available: {listed}")
    fallback = ConfigFallback(
        "DEFAULT_LOCALE", settings.default_locale, f"no locale file, available: {listed}", default
    )
    return settings.model_copy(update={"default_locale": default}), fallback
