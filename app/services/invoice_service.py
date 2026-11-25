"""Számlaelemzés: osztályozás (irány, partner) és a feldolgozási láncba kötött elemző."""

from typing import Protocol

from app.core.errors import ProcessingError
from app.extraction.company import Company
from app.extraction.models import InvoiceFields
from app.extraction.validation import InvoiceStatus, Issue, Severity, ValidationResult, validate_invoice
from app.repositories.invoice_repository import Classification, Direction, InvoiceRepository

# A feladat nem kéri a nettót és az ÁFA-t; hiányuk nem hiba. A teljesítés dátuma kötelező (az MNB-árfolyam napja).
OPTIONAL_FIELDS = frozenset({"net_total", "vat_total"})


def classify(fields: InvoiceFields, company: Company) -> Classification:
    """Kimenő, ha a kiállító a saját cég; bejövő, ha a vevő. Bizonytalan (se egyik, se mindkettő) esetben nincs irány."""
    seller_is_us = company.is_party(fields.seller_name, fields.seller_tax_id)
    buyer_is_us = company.is_party(fields.buyer_name, fields.buyer_tax_id)
    if seller_is_us == buyer_is_us:
        return Classification(None, None, None)
    if seller_is_us:
        return Classification(Direction.OUTGOING, fields.buyer_name, fields.buyer_tax_id)
    return Classification(Direction.INCOMING, fields.seller_name, fields.seller_tax_id)


class Extractor(Protocol):
    async def extract(self, text: str) -> InvoiceFields: ...


class CurrencyCheck(Protocol):
    async def is_quoted(self, currency: str) -> bool | None: ...


class InvoiceAnalyzer:
    """Szövegből számlaadat: LLM-es kinyerés, validáció, osztályozás, mentés.

    Ha nincs beállított LLM-kulcs, minden számla `llm_not_configured` hibát kap. Ha az LLM válasza hiányos vagy
    érvénytelen, a feldolgozás hiba, és semmi nem tárolódik. Gyanús érték (pl. irány nélküli számla) esetén
    a számla ellenőrzendőként tárolódik.

    Ha van `currency_check`, a mentés előtt kiderül, hogy az MNB jegyzi-e a számla devizáját (a kimutatás
    pénzneme kivételével); ha nem, a számla ellenőrzendő.
    """

    def __init__(
        self,
        llm: Extractor | None,
        repository: InvoiceRepository,
        company: Company,
        currency_check: CurrencyCheck | None = None,
        report_currency: str | None = None,
    ) -> None:
        self._llm = llm
        self._repository = repository
        self._company = company
        self._currency_check = currency_check
        self._report_currency = report_currency

    async def analyze(self, content_hash: str, filename: str, text: str) -> None:
        if self._llm is None:
            raise ProcessingError("no LLM API key is set, invoices cannot be extracted", code="llm_not_configured")
        fields = await self._llm.extract(text)
        validation = self._assess(fields, text)
        validation = await self._with_currency_check(validation, fields.currency)
        await self._repository.save(
            content_hash,
            filename,
            fields,
            classify(fields, self._company),
            validation.status.value,
            [issue.as_dict() for issue in validation.issues],
        )

    def _assess(self, fields: InvoiceFields, text: str) -> ValidationResult:
        """A mentésre kerülő validációs eredmény; hiányzó kötelező mező vagy érvénytelen adat esetén `ProcessingError`."""
        missing = tuple(name for name in InvoiceFields.names() if getattr(fields, name) in (None, ""))
        if len(missing) == len(InvoiceFields.names()):
            raise ProcessingError("no invoice data found in the document", code="no_invoice_found")
        missing_required = tuple(name for name in missing if name not in OPTIONAL_FIELDS)
        if missing_required:
            raise ProcessingError(
                f"incomplete invoice data, missing: {', '.join(missing_required)}",
                code="incomplete_data",
                fields=missing_required,
            )
        validation = validate_invoice(fields, company=self._company, source_text=text)
        if validation.status is InvoiceStatus.INVALID:
            invalid = tuple(issue.field or issue.code for issue in validation.issues if issue.severity is Severity.ERROR)
            raise ProcessingError(f"invalid invoice data: {', '.join(invalid)}", code="invalid_data", fields=invalid)
        return self._with_direction_check(validation, fields)

    def _with_direction_check(self, validation: ValidationResult, fields: InvoiceFields) -> ValidationResult:
        """Irány nélkül (a saját cég se kiállító, se vevő, vagy mindkettő) a számla ellenőrzendő."""
        if classify(fields, self._company).direction is not None:
            return validation
        issue = Issue("no_direction", None, Severity.WARNING)
        return ValidationResult(InvoiceStatus.NEEDS_REVIEW, (*validation.issues, issue))

    async def _with_currency_check(self, validation: ValidationResult, currency: str | None) -> ValidationResult:
        if self._currency_check is None or not currency or currency == self._report_currency:
            return validation
        if await self._currency_check.is_quoted(currency) is not False:
            return validation
        issue = Issue("currency_not_quoted", "currency", Severity.WARNING)
        return ValidationResult(InvoiceStatus.NEEDS_REVIEW, (*validation.issues, issue))
