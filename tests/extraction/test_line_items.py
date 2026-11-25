from decimal import Decimal

import pytest

from app.extraction.line_items import check_line_items

D = Decimal

ROW_WISE_HU = """\
Megnevezés Menny. Egységár Érték
Tanácsadás 2 db 500,00 1 000,00
Licenc 3 db 100,00 300,00
Utazás 1 db 200,00 200,00
Nettó összesen 1 500,00 Ft
ÁFA 27% 405,00 Ft
Fizetendő 1 905,00 Ft
"""

ROW_WISE_EN_COMMA = """\
Description Qty Unit price Amount
Consulting 2 pcs 1,250.50 2,501.00
Hosting 12 pcs 10.00 120.00
Total 2,621.00 EUR
"""

COLUMN_WISE_DE = """\
Pos.
Menge
2
4
Einzelpreis
10,50
20,00
Gesamt
21,00
80,00
Summe 101,00 EUR
"""

NON_LATIN = """\
Товар 5 шт 20.00 100.00
Услуга 2 шт 50.00 100.00
Итого 200.00 RUB
"""

ROW_WISE_VAT_COLUMN = """\
Item 2 pcs 100.00 27% 200.00
Item 1 pcs 50.00 27% 50.00
Net 250.00 USD
"""


@pytest.mark.parametrize(
    ("text", "targets"),
    [
        (ROW_WISE_HU, [D("1500.00"), D("1905.00")]),
        (ROW_WISE_EN_COMMA, [D("2621.00")]),
        (COLUMN_WISE_DE, [D("101.00")]),
        (NON_LATIN, [D("200.00")]),
        (ROW_WISE_VAT_COLUMN, [D("250.00")]),
    ],
)
def test_matching_line_items_are_consistent(text, targets):
    result = check_line_items(text, targets)
    assert result.consistent is True
    assert result.row_count >= 2


def test_gross_target_also_matches_for_gross_priced_lines():
    assert check_line_items(ROW_WISE_HU, [D("999.00"), D("1500.00")]).consistent is True


@pytest.mark.parametrize(
    ("text", "targets"),
    [
        (ROW_WISE_HU, [D("1700.00"), D("2159.00")]),
        (ROW_WISE_EN_COMMA, [D("2700.00")]),
        (COLUMN_WISE_DE, [D("150.00")]),
    ],
)
def test_wrong_totals_are_inconsistent(text, targets):
    result = check_line_items(text, targets)
    assert result.consistent is False


def test_no_line_items_means_no_verdict():
    text = "Szolgáltatás díja 1 000,00 Ft\nNettó 1 000,00\nÁFA 270,00\nBruttó 1 270,00"
    assert check_line_items(text, [D("1270.00")]).consistent is None


def test_a_single_row_is_not_enough():
    assert check_line_items("Munka 2 db 500,00 1 000,00\nÖsszesen 1 000,00", [D("1000.00")]).consistent is None


def test_without_targets_there_is_no_verdict():
    assert check_line_items(ROW_WISE_HU, []).consistent is None


def test_unrecognised_extra_row_explained_by_a_listed_amount_is_tolerated():
    text = "A 2 pcs 100,00 200,00\nB 3 pcs 50,00 150,00\nKedvezmény 10,00\nÖsszesen 340,00"
    assert check_line_items(text, [D("340.00")]).consistent is True


def test_rounding_of_line_totals_is_tolerated():
    text = "A 3 pcs 3,333 10,00\nB 3 pcs 3,333 10,00\nÖsszesen 20,01"
    assert check_line_items(text, [D("20.01")]).consistent is True


def test_row_of_totals_summary_is_not_mistaken_for_line_items():
    text = "Nettó 1 000,00\nÁFA 270,00\nBruttó 1 270,00"
    assert check_line_items(text, [D("1270.00")]).consistent is None
