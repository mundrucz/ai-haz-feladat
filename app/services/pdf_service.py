import asyncio
from dataclasses import dataclass

import pymupdf

from app.core.errors import ProcessingError

PAGE_SEPARATOR = "\f"
_HEADER_SCAN_BYTES = 1024


class NotAPdfError(ProcessingError):
    code = "not_a_pdf"


class CorruptPdfError(ProcessingError):
    code = "corrupt_pdf"


class PasswordProtectedError(ProcessingError):
    code = "password_protected"


class NoTextError(ProcessingError):
    code = "no_text"


@dataclass(frozen=True)
class PdfText:
    pages: list[str]

    @property
    def text(self) -> str:
        return PAGE_SEPARATOR.join(self.pages)


def extract_text_sync(data: bytes) -> PdfText:
    """Oldalanként kinyeri a szöveget; nem nyelvfüggő, csak a PDF szöveges rétegét olvassa."""
    if b"%PDF-" not in data[:_HEADER_SCAN_BYTES]:
        raise NotAPdfError("the file is not a PDF document")
    try:
        document = pymupdf.open(stream=data, filetype="pdf")
    except Exception as exc:
        raise CorruptPdfError(f"the PDF cannot be opened: {exc}") from exc
    try:
        if document.needs_pass:
            raise PasswordProtectedError("the PDF is password protected")
        try:
            # A tartalomfolyam sorrendje megőrzi az oszlopos elrendezések blokkjait; a pozíció szerinti rendezés összekeveri őket.
            pages = [page.get_text("text").strip() for page in document]
        except Exception as exc:
            raise CorruptPdfError(f"the PDF text cannot be read: {exc}") from exc
    finally:
        document.close()
    if not any(pages):
        raise NoTextError("the PDF has no text layer (empty or scanned document)")
    return PdfText(pages=pages)


async def extract_text(data: bytes) -> PdfText:
    """A CPU-igényes feldolgozás szálkészletben fut, hogy ne blokkolja az eseményhurkot."""
    return await asyncio.to_thread(extract_text_sync, data)
