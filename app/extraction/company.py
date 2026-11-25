"""A saját cég azonosítói, amikkel a dokumentumban megtalálható."""

from dataclasses import dataclass

from app.extraction.normalize import normalize_key

_MIN_KEY_LENGTH = 6


@dataclass(frozen=True, slots=True)
class Company:
    name: str = ""
    tax_number: str = ""
    eu_vat: str = ""

    def keys(self) -> tuple[str, ...]:
        keys = (normalize_key(self.tax_number), normalize_key(self.eu_vat), normalize_key(self.name))
        return tuple(key for key in keys if len(key) >= _MIN_KEY_LENGTH)

    def appears_in(self, text: str) -> bool:
        """Szerepel-e a cég adószáma, EU adószáma vagy neve a szövegben (tagolástól függetlenül)."""
        compact = normalize_key(text)
        return any(key in compact for key in self.keys())

    def is_party(self, name: str | None, tax_id: str | None) -> bool:
        """A kinyert fél a saját cég-e: az adószám vagy a név (tagolástól függetlenül) egyezik."""
        own = set(self.keys())
        return any(key in own for key in (normalize_key(tax_id), normalize_key(name)) if key)

