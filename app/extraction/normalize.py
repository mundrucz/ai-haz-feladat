"""Nyelv- és pénznemfüggetlen normalizálás: összegek, dátumok, pénznemek, kulcsok.

Az LLM válaszának értelmezéséhez és a válasz szövegbeli ellenőrzéséhez (grounding, validáció) kellenek.
A függvények a szöveg *szerkezetéből* dolgoznak (elválasztók, számjegyek,
sorrend), nem nyelvi feltételezésekből. A kétértelmű bemenetre nem tippelnek:
`None`-t adnak. A hónapnevek és pénznemek adatfájlokban vannak
(`lexicon`), új nyelv vagy deviza felvétele nem igényel kódmódosítást.
"""

import re
import unicodedata
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation
from functools import lru_cache
from typing import Iterable

from app.extraction.lexicon import Currency, Language, currencies, languages

DateOrder = str  # "DMY" | "MDY"


# --- szöveg és kulcsok -------------------------------------------------------


def normalize_text(value: str) -> str:
    """NFKC, kisbetű, ékezetmentesítés (összehasonlításhoz)."""
    decomposed = unicodedata.normalize("NFKD", unicodedata.normalize("NFKC", value).casefold())
    stripped = "".join(ch for ch in decomposed if not unicodedata.combining(ch))
    return stripped.replace("\u2019", "'").replace("\u2018", "'")


def normalize_key(value: str | None) -> str:
    """Azonosításra használt kulcs: ékezetmentes, nagybetűs, csak betűk és számjegyek."""
    if not value:
        return ""
    return "".join(ch for ch in normalize_text(value) if ch.isalnum()).upper()


# --- összegek ------------------------------------------------------------------

_AMOUNT_TOKEN = re.compile(
    r"\d{1,2}(?:,\d{2})+,\d{3}(?:\.\d+)?"  # indiai csoportosítás: 1,23,456.78
    r"|\d{1,3}(?:[ .,'’]\d{3})+(?:[.,]\d+)?"  # csoportosított: 1 234,56 / 1.234,56 / 1,234.56
    r"|\d+(?:[.,]\d+)?"  # egyszerű: 1234,56
)

_AMBIGUOUS = object()


def _amount_tokens(raw: str) -> tuple[str, list[re.Match[str]]]:
    text = unicodedata.normalize("NFKC", raw).replace("\u2212", "-").replace("\u2019", "'")
    return text, list(_AMOUNT_TOKEN.finditer(text))


def _decimal_separator(token: str, hint: str | None = None) -> object:
    """A tizedeselválasztó a token szerkezetéből: karakter, "" (nincs) vagy `_AMBIGUOUS`."""
    marks = {ch for ch in token if ch in ".,"}
    has_group_char = any(not ch.isdigit() and ch not in ".," for ch in token)
    if len(marks) == 2:
        separator = next(ch for ch in reversed(token) if ch in ".,")
        if token.count(separator) != 1:
            raise ValueError("több tizedeselválasztó")
        return separator
    if not marks:
        return ""
    mark = next(iter(marks))
    if token.count(mark) > 1:
        return ""
    integer_part, fraction = token.split(mark)
    if len(fraction) != 3 or has_group_char or integer_part == "0":
        return mark
    if hint in (".", ","):
        return mark if hint == mark else ""
    return _AMBIGUOUS


def _decimal_vote(token: str) -> str | None:
    """Biztos jelzés a tizedeselválasztóra (hint-képzéshez), egyébként None."""
    try:
        result = _decimal_separator(token)
    except ValueError:
        return None
    if isinstance(result, str) and result:
        return result
    marks = {ch for ch in token if ch in ".,"}
    if len(marks) == 1 and token.count(next(iter(marks))) > 1:
        return "," if "." in marks else "."
    return None


def _is_negative(prefix: str, suffix: str) -> bool:
    before, after = prefix.strip(), suffix.strip()
    if before.endswith("-") or after.startswith("-"):
        return True
    if len(before) >= 2 and before[-2] == "-" and not before[-1].isalnum():  # -€ 5
        return True
    return "(" in before and after.startswith(")")


def parse_amount(raw: str | None, decimal_hint: str | None = None) -> Decimal | None:
    """Összeg értelmezése; `decimal_hint` ("." vagy ",") csak a kétértelmű `1.234` alakot dönti el.

    Alapértelmezésben az egyetlen elválasztó + pontosan 3 számjegy ezres tagolásnak
    számít, hint nélkül is, hogy a nagy összeg ne csússzon el ezerszeresére.
    """
    if not raw:
        return None
    text, tokens = _amount_tokens(raw)
    if len(tokens) != 1:
        return None
    match = tokens[0]
    token = match.group()
    prefix, suffix = text[: match.start()], text[match.end() :]
    negative = _is_negative(prefix, suffix)
    try:
        separator = _decimal_separator(token, decimal_hint)
    except ValueError:
        return None
    if separator is _AMBIGUOUS:
        separator = ""
    assert isinstance(separator, str)
    if separator:
        integer_part, fraction = token.rsplit(separator, 1)
    else:
        integer_part, fraction = token, ""
    digits = "".join(ch for ch in integer_part if ch.isdigit())
    try:
        value = Decimal(f"{digits}.{fraction}" if fraction else digits)
    except InvalidOperation:
        return None
    return -value if negative else value


@dataclass(frozen=True, slots=True)
class AmountMatch:
    value: Decimal
    raw: str
    start: int
    end: int


def find_amounts(text: str, decimal_hint: str | None = None) -> list[AmountMatch]:
    """A szövegben talált összeg-szerű számok; dátumok és azonosítók darabjait kihagyja."""
    found: list[AmountMatch] = []
    for match in _AMOUNT_TOKEN.finditer(text):
        start, end = match.span()
        before = text[start - 1] if start else ""
        after = text[end] if end < len(text) else ""
        after_digit = text[end + 1 : end + 2].isdigit()
        if before.isalnum() or after.isalnum() or before in (".", "/") or after == "/" or (after in ".-" and after_digit):
            continue
        value = parse_amount(match.group(), decimal_hint)
        if value is None:
            continue
        if _is_negative(text[:start], text[end:]):
            value = -abs(value)
        found.append(AmountMatch(value, match.group(), start, end))
    return found


def infer_decimal_separator(raws: Iterable[str]) -> str | None:
    """Egy dokumentum összegeiből kikövetkeztetett tizedeselválasztó, ha egyértelmű."""
    votes: set[str] = set()
    for raw in raws:
        _, tokens = _amount_tokens(raw)
        if len(tokens) == 1 and (vote := _decimal_vote(tokens[0].group())):
            votes.add(vote)
    return next(iter(votes)) if len(votes) == 1 else None


# --- dátumok -------------------------------------------------------------------

_CJK_DATE = re.compile(r"(\d{4})\s*[年년]\s*(\d{1,2})\s*[月월]\s*(\d{1,2})\s*[日일]?")
_COMPACT_DATE = re.compile(r"^\s*(\d{4})(\d{2})(\d{2})\s*$")
_NUMERIC_DATE = re.compile(
    r"^\D*?(\d{1,4})\s*([./-])\s*(\d{1,2})\s*\2\s*(\d{1,4})(?!\d)(?:\s*[T ]\s*\d{1,2}:\d{2}.*|\D*)$"
)
_WORD_OR_NUMBER = re.compile(r"[^\W\d_]+|\d+")


def build_month_table(langs: Iterable[Language]) -> dict[str, int]:
    """A nyelvek hónapnevei egy közös táblában; az egyértelmű (legalább 3 betűs) előtag rövidítésként is érvényes."""
    full: dict[str, int] = {}
    for language in langs:
        for month, forms in enumerate(language.months, 1):
            full.update({normalize_text(form): month for form in forms})
    prefixes: dict[str, set[int]] = {}
    for name, month in full.items():
        for length in range(3, len(name)):
            prefixes.setdefault(name[:length], set()).add(month)
    table = dict(full)
    for prefix, months in prefixes.items():
        if prefix not in table and len(months) == 1:
            table[prefix] = next(iter(months))
    return table


@lru_cache(maxsize=1)
def _month_table() -> dict[str, int]:
    return build_month_table(languages())


def _make_date(year: int, month: int, day: int) -> date | None:
    try:
        return date(year, month, day)
    except ValueError:
        return None


def _numeric_parts(text: str) -> tuple[str, str, str, str] | None:
    """(első, második, harmadik, elválasztó) a `a.b.c`, `a/b/c`, `a-b-c` alakú dátumokhoz."""
    match = _NUMERIC_DATE.match(text)
    if not match:
        return None
    first, separator, second, third = match.groups()
    return first, second, third, separator


def parse_date(raw: str | None, order: DateOrder | None = None) -> date | None:
    """Dátum értelmezése; a kétértelmű `03/04/2026` csak `order` ("DMY"/"MDY") hintre dől el.

    A pontokkal tagolt, évvel záródó alak (`09.03.2026`) nap-hó-év sorrendű.
    """
    if not raw:
        return None
    text = unicodedata.normalize("NFKC", raw).strip()

    if match := _CJK_DATE.search(text):
        return _make_date(*(int(g) for g in match.groups()))
    if match := _COMPACT_DATE.match(text):
        return _make_date(*(int(g) for g in match.groups()))

    if parts := _numeric_parts(text):
        first, second, third, separator = parts
        if len(first) == 4 and len(third) <= 2:
            return _make_date(int(first), int(second), int(third))
        if len(third) == 4 and len(first) <= 2:
            a, b, year = int(first), int(second), int(third)
            if a > 12 >= b:
                return _make_date(year, b, a)
            if b > 12 >= a:
                return _make_date(year, a, b)
            if a <= 12 and b <= 12:
                if a == b or order == "DMY" or (order is None and separator == "."):
                    return _make_date(year, b, a)
                if order == "MDY":
                    return _make_date(year, a, b)
        return None

    return _parse_named_month_date(text)


def _parse_named_month_date(text: str) -> date | None:
    """Az első olyan szakasz, amelyben pontosan egy hónapnév, egy nap és egy évszám áll.

    A dátum után következő számok (`Dec 19, 2026 (Net 30)`) így nem rontják el az értelmezést.
    """
    table = _month_table()
    tokens = _WORD_OR_NUMBER.findall(normalize_text(text))
    for start in range(len(tokens)):
        months: list[int] = []
        numbers: list[str] = []
        for token in tokens[start:]:
            if token.isdigit():
                numbers.append(token)
            elif token in table:
                months.append(table[token])
            if len(numbers) == 2 and months:
                break
        if len(months) != 1 or len(numbers) != 2:
            continue
        years = [n for n in numbers if len(n) == 4]
        days = [n for n in numbers if len(n) <= 2]
        if len(years) == 1 and len(days) == 1 and (parsed := _make_date(int(years[0]), months[0], int(days[0]))):
            return parsed
    return None


# --- pénznem -------------------------------------------------------------------


_CURRENCY_CODE = re.compile(r"[A-Z]{3}")


def build_currency_marks(items: Iterable[Currency]) -> dict[str, tuple[str, ...]]:
    """Jelölés (jel vagy név, normalizálva) -> lehetséges kódok; a több devizánál szereplő jel többértelmű."""
    marks: dict[str, list[str]] = {}
    for currency in items:
        for mark in (*currency.symbols, *currency.names):
            codes = marks.setdefault(normalize_text(mark), [])
            if currency.code not in codes:
                codes.append(currency.code)
    return {mark: tuple(codes) for mark, codes in marks.items()}


@lru_cache(maxsize=1)
def _currency_data() -> tuple[frozenset[str], dict[str, tuple[str, ...]]]:
    return frozenset(currency.code for currency in currencies()), build_currency_marks(currencies())


def is_currency_code(code: str | None) -> bool:
    """ISO 4217 formátumú kód (3 nagybetű); hogy létező és jegyzett-e, azt az árfolyamforrás dönti el."""
    return bool(code) and _CURRENCY_CODE.fullmatch(code) is not None


def currency_candidates(raw: str | None) -> tuple[str, ...]:
    """A jelölés lehetséges ISO-kódjai a felvett devizák közül: üres, ha ismeretlen; több elem, ha többértelmű."""
    if not raw:
        return ()
    codes, marks = _currency_data()
    text = unicodedata.normalize("NFKC", raw).strip()
    # Csak nagybetűs kód fogadható el, a kisbetűs 3 betűs szavak (all, try, top) hétköznapi szavak.
    if text in codes:
        return (text,)
    return marks.get(normalize_text(text).rstrip("."), ())


def normalize_currency(raw: str | None) -> str | None:
    """ISO-kód, ha a jelölés egyértelmű; többértelmű vagy ismeretlen esetén None."""
    candidates = currency_candidates(raw)
    return candidates[0] if len(candidates) == 1 else None
