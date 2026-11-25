"""Az LLM által visszaadott mezők ellenőrzése: minden értéknek szerepelnie kell a számla szövegében."""

import re
import unicodedata
from datetime import date
from decimal import Decimal

from app.extraction.models import InvoiceFields
from app.extraction.normalize import (
    currency_candidates,
    find_amounts,
    normalize_key,
    normalize_text,
    parse_date,
)

_DATE_FIELDS = ("issue_date", "performance_date", "due_date")
_AMOUNT_FIELDS = ("net_total", "vat_total", "gross_total")
_NAME_FIELDS = ("seller_name", "buyer_name")
_TAX_ID_FIELDS = ("seller_tax_id", "buyer_tax_id")

_NUMERIC_DATE = re.compile(r"(?<!\d)(\d{1,4})\s?([./-])\s?(\d{1,2})\s?\2\s?(\d{1,4})(?!\d)")
_COMPACT_OR_CJK_DATE = re.compile(r"(?<!\d)\d{8}(?!\d)|\d{4}\s*[年년]\s*\d{1,2}\s*[月월]\s*\d{1,2}")
_WORD_OR_NUMBER = re.compile(r"[^\W\d_]+|\d+")
_CURRENCY_TOKEN = re.compile(r"[^\W\d_]+|[^\w\s]")
_NAMED_DATE_WINDOWS = (3, 4, 5)


def ungrounded_fields(fields: InvoiceFields, text: str) -> tuple[str, ...]:
    """Azon kitöltött mezők nevei, amelyek értéke nem található meg a számla szövegében."""
    text = unicodedata.normalize("NFKC", text)
    wrong: set[str] = set()

    keys = normalize_key(text)
    number = fields.invoice_number
    if number and normalize_key(number) not in keys:
        wrong.add("invoice_number")

    names = _NameIndex(text, keys)
    for name in _NAME_FIELDS:
        value = getattr(fields, name)
        if value and not names.contains(value):
            wrong.add(name)

    for name in _TAX_ID_FIELDS:
        value = getattr(fields, name)
        if value and not _tax_id_in(value, keys):
            wrong.add(name)

    if any(getattr(fields, name) for name in _DATE_FIELDS):
        dates = _dates_in(text)
        for name in _DATE_FIELDS:
            value = getattr(fields, name)
            if value is not None and value not in dates:
                wrong.add(name)

    if any(getattr(fields, name) for name in _AMOUNT_FIELDS):
        amounts = _amounts_in(text)
        for name in _AMOUNT_FIELDS:
            value = getattr(fields, name)
            if value is None or (name == "vat_total" and value == 0):
                continue
            if abs(value) not in amounts:
                wrong.add(name)

    if fields.currency and not _currency_in(fields.currency, text):
        wrong.add("currency")

    return tuple(name for name in InvoiceFields.names() if name in wrong)


class _NameIndex:
    def __init__(self, text: str, key: str) -> None:
        self._key = key
        self._words = set(_WORD_OR_NUMBER.findall(normalize_text(text)))

    def contains(self, name: str) -> bool:
        if normalize_key(name) in self._key:
            return True
        words = _WORD_OR_NUMBER.findall(normalize_text(name))
        return bool(words) and all(word in self._words for word in words)


def _tax_id_in(value: str, text_key: str) -> bool:
    key = normalize_key(value)
    if not key:
        return False
    if key in text_key:
        return True
    # Az országelőtag (HU, DE…) a szövegben vagy a válaszban hiányozhat.
    bare = key[2:] if key[:2].isalpha() else key
    return len(bare) >= 6 and bare in text_key


def _dates_in(text: str) -> set[date]:
    found: set[date] = set()
    for match in _NUMERIC_DATE.finditer(text):
        first, separator, second, third = match.groups()
        raws = [match.group()]
        if len(third) == 2 and len(first) <= 2:
            raws.append(f"{first}{separator}{second}{separator}20{third}")
        for raw in raws:
            for order in (None, "DMY", "MDY"):
                if (parsed := parse_date(raw, order)) is not None:
                    found.add(parsed)
    for match in _COMPACT_OR_CJK_DATE.finditer(text):
        if (parsed := parse_date(match.group())) is not None:
            found.add(parsed)
    tokens = _WORD_OR_NUMBER.findall(normalize_text(text))
    for size in _NAMED_DATE_WINDOWS:
        for start in range(len(tokens) - size + 1):
            if (parsed := parse_date(" ".join(tokens[start : start + size]))) is not None:
                found.add(parsed)
    return found


def _amounts_in(text: str) -> set[Decimal]:
    found: set[Decimal] = set()
    for hint in (None, ",", "."):
        found.update(abs(match.value) for match in find_amounts(text, hint))
    return found


def _currency_in(code: str, text: str) -> bool:
    return any(code in currency_candidates(token) for token in set(_CURRENCY_TOKEN.findall(text)))
