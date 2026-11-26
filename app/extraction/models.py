"""A számlákból kinyert mezők közös adatmodellje.

Minden mező opcionális: a kinyerő csak azt tölti ki, amit biztosan megtalált,
a hiányokat a validáció jelzi. Az értékek már normalizáltak (Decimal, date,
ISO pénznemkód), így a kinyerő módszerétől függetlenül ugyanúgy kezelhetők.
"""

from dataclasses import dataclass, fields
from datetime import date
from decimal import Decimal


@dataclass(frozen=True, slots=True)
class InvoiceFields:
    invoice_number: str | None = None
    seller_name: str | None = None
    seller_tax_id: str | None = None
    buyer_name: str | None = None
    buyer_tax_id: str | None = None
    issue_date: date | None = None
    performance_date: date | None = None
    due_date: date | None = None
    currency: str | None = None
    net_total: Decimal | None = None
    vat_total: Decimal | None = None
    gross_total: Decimal | None = None

    @classmethod
    def names(cls) -> tuple[str, ...]:
        return tuple(f.name for f in fields(cls))
