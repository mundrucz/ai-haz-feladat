import json
from collections.abc import Iterable
from pathlib import Path


def available_locales(locales_dir: Path) -> list[str]:
    """A mappában lévő nyelvi fájlok (`<locale>.json`) nyelvkódjai."""
    return sorted(path.stem for path in locales_dir.glob("*.json"))


class Translator:
    """Nyelvi fájlokból (`<locale>.json`) fordít; hiányzó kulcsnál az alapnyelvre, végül a kulcsra esik vissza."""

    def __init__(self, locales_dir: Path, default_locale: str) -> None:
        self.default_locale = default_locale
        self._catalogs: dict[str, dict[str, str]] = {
            locale: json.loads((locales_dir / f"{locale}.json").read_text(encoding="utf-8"))
            for locale in available_locales(locales_dir)
        }

    @property
    def locales(self) -> list[str]:
        return list(self._catalogs)

    def get(self, key: str, locale: str | None = None) -> str | None:
        for candidate in (locale, self.default_locale):
            text = self._catalogs.get(candidate or "", {}).get(key)
            if text is not None:
                return text
        return None

    def translate(self, key: str, locale: str | None = None, **params: object) -> str:
        text = self.get(key, locale)
        if text is None:
            return key
        return text.format(**params) if params else text

    def catalog(self, prefixes: Iterable[str], locale: str | None = None) -> dict[str, str]:
        """A megadott előtagú kulcsok szövegei (az alapnyelvvel kiegészítve), pl. a böngészőoldali kódnak."""
        keys = {
            key
            for catalog in self._catalogs.values()
            for key in catalog
            if any(key.startswith(prefix) for prefix in prefixes)
        }
        return {key: self.get(key, locale) or key for key in sorted(keys)}
