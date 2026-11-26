"""Az ellenőrzések adatfájljai, kódmódosítás nélkül bővíthetők.

- `resources/languages/<nyelv>.json`: egy nyelv hónapnevei (12 elem, mindegyik a hónap alakjainak listája;
  a hónapneves dátumok szövegbeli ellenőrzéséhez) és az ÁFA-címkéi (`labels.vat_total`, az ÁFA-kulcs
  ellenőrzéséhez). Új nyelv = új fájl.
- `resources/currencies.json`: devizánként (ISO 4217 kód) a jelek és a nevek (az LLM által adott deviza
  szövegbeli ellenőrzéséhez). Új deviza = új bejegyzés. Ha egy jel több devizánál is szerepel (pl. `$`), többértelmű.

Ami nincs felvéve, az nem akadály: a fel nem vett hónapnévnél csak a napot és az évet, a fel nem vett devizánál
csak a kódot ellenőrizzük a szövegben, ÁFA-címke nélkül pedig az ÁFA-kulcs ellenőrzése marad ki.
"""

import json
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

_RESOURCES = Path(__file__).parent / "resources"


@dataclass(frozen=True, slots=True)
class Language:
    code: str
    labels: dict[str, tuple[str, ...]]
    months: tuple[tuple[str, ...], ...]


@dataclass(frozen=True, slots=True)
class Currency:
    code: str
    symbols: tuple[str, ...]
    names: tuple[str, ...]


def load_language(path: Path) -> Language:
    data = json.loads(path.read_text(encoding="utf-8"))
    months = tuple(tuple(forms) for forms in data["months"])
    if len(months) != 12 or not all(months):
        raise ValueError(f"{path.name}: a months listának 12 nem üres eleme kell legyen")
    labels = {field: tuple(values) for field, values in data["labels"].items()}
    return Language(path.stem, labels, months)


def load_currencies(path: Path) -> tuple[Currency, ...]:
    data = json.loads(path.read_text(encoding="utf-8"))
    currencies: list[Currency] = []
    for code, entry in data.items():
        if len(code) != 3 or not code.isalpha() or not code.isupper():
            raise ValueError(f"{path.name}: érvénytelen ISO 4217 kód: {code!r}")
        currencies.append(Currency(code, tuple(entry.get("symbols", ())), tuple(entry.get("names", ()))))
    return tuple(currencies)


@lru_cache(maxsize=1)
def languages() -> tuple[Language, ...]:
    return tuple(load_language(path) for path in sorted((_RESOURCES / "languages").glob("*.json")))


@lru_cache(maxsize=1)
def currencies() -> tuple[Currency, ...]:
    return load_currencies(_RESOURCES / "currencies.json")
