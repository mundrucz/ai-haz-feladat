import pytest
from pdfs import make_blank_pdf, make_encrypted_pdf, make_pdf

from app.services.pdf_service import (
    CorruptPdfError,
    NoTextError,
    NotAPdfError,
    PasswordProtectedError,
    extract_text,
)


async def test_extracts_text_from_every_page():
    result = await extract_text(make_pdf("első oldal", "második oldal"))

    assert len(result.pages) == 2
    assert "első oldal" in result.pages[0]
    assert "második oldal" in result.pages[1]
    assert "második oldal" in result.text


@pytest.mark.parametrize(
    "text",
    [
        "Számla – árvíztűrő tükörfúrógép ŐŰ",
        "Счёт № 15 на сумму 1 250,00 ₽",
        "請求書 合計 ¥12,500",
        "Rechnung Nr. 2024/17 – 1.234,56 CHF",
    ],
)
async def test_unicode_and_non_latin_text_is_preserved(text):
    result = await extract_text(make_pdf(text))

    assert text in result.text


async def test_rejects_non_pdf_content():
    with pytest.raises(NotAPdfError):
        await extract_text(b"ez csak egy sima szoveg, nem PDF")


async def test_rejects_corrupt_pdf():
    data = b"%PDF-1.7\n" + b"\x00\x01garbage" * 50
    with pytest.raises(CorruptPdfError):
        await extract_text(data)


async def test_rejects_password_protected_pdf():
    with pytest.raises(PasswordProtectedError):
        await extract_text(make_encrypted_pdf())


async def test_rejects_pdf_without_text_layer():
    with pytest.raises(NoTextError):
        await extract_text(make_blank_pdf())
