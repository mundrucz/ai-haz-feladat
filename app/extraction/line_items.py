"""Tételsorok összegének ellenőrzése a számla szövegéből, nyelvfüggetlenül.

A tételsort a szerkezetéről ismerjük fel: `mennyiség × egységár = sorösszeg` összefüggést kielégítő számhármasokról.
Két elrendezést kezelünk: soronként (a sor számai egymás után) és oszloponként (előbb az összes mennyiség,
majd az összes egységár, végül az összes sorösszeg). Ha nem ismerünk fel legalább két tételsort, nincs ítélet.
"""

import unicodedata
from dataclasses import dataclass
from decimal import Decimal

from app.extraction.labels import make_lines
from app.extraction.normalize import find_amounts, infer_decimal_separator

_MIN_ROWS = 2
_PRICE_OFFSETS = (1, 2)  # a mennyiség után közvetlenül, vagy egy közbeeső oszlop után áll az egységár
_TOTAL_GAP = 3  # az egységár és a sorösszeg között legfeljebb ennyi további szám (kedvezmény, ÁFA-kulcs)
_MAX_COLUMN_GAPS = 2  # az oszloponkénti elrendezésben legfeljebb ennyi köztes oszlop
_MAX_ROWS = 80
_PRODUCT_ABS_TOLERANCE = Decimal("0.02")
_PRODUCT_REL_TOLERANCE = Decimal("0.005")
_SUM_ABS_TOLERANCE = Decimal("0.02")
_SUM_ROW_TOLERANCE = Decimal("0.01")
_SUM_REL_TOLERANCE = Decimal("0.0005")
_EXPLAINED_TOLERANCE = Decimal("0.011")


@dataclass(frozen=True, slots=True)
class LineItemCheck:
    """`consistent` None, ha nem ismertünk fel tételsorokat; különben: a sorok összege egyezik-e a megadott összegek valamelyikével."""

    consistent: bool | None
    row_count: int = 0


@dataclass(frozen=True, slots=True)
class _Rows:
    totals: list[Decimal]
    used: set[int]  # a sorokban részt vevő számok helye (mennyiség, egységár, sorösszeg)


def check_line_items(text: str, targets: list[Decimal], ignore: list[Decimal] | None = None) -> LineItemCheck:
    """A tételsorok összege egyezzen valamelyik célösszeggel (nettó vagy bruttó, az árak jellegétől függően).

    Az `ignore` összegek (a számla végösszegei) nem magyarázhatják a különbséget.
    """
    targets = [target for target in targets if target > 0]
    values = _amounts(text)
    candidates = [rows for rows in (_row_wise(values), _column_wise(values)) if len(rows.totals) >= _MIN_ROWS]
    if not targets or not candidates:
        return LineItemCheck(None)
    skipped = {abs(value) for value in ignore or ()}
    for rows in candidates:
        known = {abs(value) for index, value in enumerate(values) if index not in rows.used and abs(value) not in skipped}
        if any(_matches(rows.totals, target, known) for target in targets):
            return LineItemCheck(True, len(rows.totals))
    return LineItemCheck(False, len(candidates[0].totals))

def _amounts(text: str) -> list[Decimal]:
    lines = make_lines(unicodedata.normalize("NFKC", text))
    hint = infer_decimal_separator(match.raw for line in lines for match in find_amounts(line.raw))
    return [match.value for line in lines for match in find_amounts(line.raw, hint)]


def _is_product(first: Decimal, second: Decimal, total: Decimal) -> bool:
    if first <= 0 or second <= 0 or total <= 0:
        return False
    return abs(first * second - total) <= max(_PRODUCT_ABS_TOLERANCE, total * _PRODUCT_REL_TOLERANCE)


def _row_wise(values: list[Decimal]) -> _Rows:
    rows = _Rows([], set())
    index = 0
    while index < len(values):
        found = _row_at(values, index)
        if found is None:
            index += 1
            continue
        price, total = found
        rows.totals.append(values[index + total])
        rows.used.update((index, index + price, index + total))
        index += total + 1
    return rows


def _row_at(values: list[Decimal], index: int) -> tuple[int, int] | None:
    """A `index`-nél kezdődő sor: az egységár és a sorösszeg eltolása."""
    for price in _PRICE_OFFSETS:
        if index + price >= len(values):
            break
        for total in range(price + 1, price + 1 + _TOTAL_GAP):
            if index + total < len(values) and _is_product(values[index], values[index + price], values[index + total]):
                return price, total
    return None


def _column_wise(values: list[Decimal]) -> _Rows:
    rows = _Rows([], set())
    start = 0
    while start < len(values):
        found = _columns_at(values, start)
        if found is None:
            start += 1
            continue
        count, gaps = found
        total_start = start + (2 + gaps) * count
        rows.totals.extend(values[total_start : total_start + count])
        rows.used.update(range(start, start + count * 2))
        rows.used.update(range(total_start, total_start + count))
        start = total_start + count
    return rows


def _columns_at(values: list[Decimal], start: int) -> tuple[int, int] | None:
    """A `start`-tól induló legnagyobb (mennyiség, egységár, [köztes oszlopok], sorösszeg) blokk-elrendezés: (sorok száma, köztes oszlopok)."""
    for gaps in range(_MAX_COLUMN_GAPS + 1):
        width = 2 + gaps
        for count in range(min(_MAX_ROWS, (len(values) - start) // (width + 1)), _MIN_ROWS - 1, -1):
            total_start = start + width * count
            if all(
                _is_product(values[start + i], values[start + count + i], values[total_start + i]) for i in range(count)
            ):
                return count, gaps
    return None

def _matches(rows: list[Decimal], target: Decimal, known: set[Decimal]) -> bool:
    total = sum(rows, Decimal(0))
    tolerance = _SUM_ABS_TOLERANCE + _SUM_ROW_TOLERANCE * len(rows) + target * _SUM_REL_TOLERANCE
    difference = abs(total - target)
    if difference <= tolerance:
        return True
    # Fel nem ismert sor (kedvezmény, szállítás, szorzással nem kifejezhető tétel) a számlán külön számként szerepel.
    return any(abs(amount - difference) <= _EXPLAINED_TOLERANCE for amount in known)
