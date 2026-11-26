"""Sorok és címke-szótár a validáció szövegbeli ellenőrzéseihez (pl. az ÁFA-kulcs sorának megkeresése)."""

from dataclasses import dataclass
from functools import lru_cache
from typing import Iterable, Mapping

from app.extraction.lexicon import Language, languages
from app.extraction.normalize import normalize_text


@dataclass(frozen=True, slots=True)
class Line:
    raw: str
    norm: str

    @property
    def has_digit(self) -> bool:
        return any(ch.isdigit() for ch in self.raw)


def make_lines(text: str) -> list[Line]:
    lines: list[Line] = []
    for raw in text.replace("\u00a0", " ").splitlines():
        raw = raw.strip()
        if raw:
            lines.append(Line(raw, normalize_text(raw)))
    return lines


class Labels:
    """Címke-szótár: mezőnként kifejezések, ékezet- és kisbetű-függetlenül, szóhatáron egyezve."""

    def __init__(self, data: Mapping[str, Iterable[str]]) -> None:
        self._phrases: dict[str, tuple[str, ...]] = {
            field: tuple(normalize_text(value) for value in values) for field, values in data.items()
        }

    @classmethod
    def from_languages(cls, langs: Iterable[Language]) -> "Labels":
        """A nyelvi fájlok címkéi egy közös szótárban: a keresés nem tételez fel nyelvet."""
        merged: dict[str, list[str]] = {}
        for language in langs:
            for field, values in language.labels.items():
                merged.setdefault(field, []).extend(values)
        return cls(merged)

    @classmethod
    @lru_cache(maxsize=1)
    def default(cls) -> "Labels":
        return cls.from_languages(languages())

    def contains(self, line: Line, field: str) -> bool:
        """Szerepel-e a sorban a mező valamelyik címkéje (szóhatáron)."""
        text = line.norm
        for phrase in self._phrases.get(field, ()):
            position = text.find(phrase)
            while position != -1:
                end = position + len(phrase)
                if (position == 0 or not text[position - 1].isalnum()) and (end == len(text) or not text[end].isalnum()):
                    return True
                position = text.find(phrase, position + 1)
        return False
