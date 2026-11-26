from collections import Counter

from fastapi import APIRouter, Depends

from app.api.dependencies import get_invoice_repository, get_jobs
from app.core.errors import ConflictError
from app.extraction.validation import InvoiceStatus
from app.repositories.invoice_repository import InvoiceRecord, InvoiceRepository
from app.services.job_service import JobTracker

router = APIRouter(prefix="/api/invoices")

_STATUSES = (InvoiceStatus.OK.value, InvoiceStatus.NEEDS_REVIEW.value)


def _invoice_view(record: InvoiceRecord) -> dict:
    fields = record.fields
    return {
        "id": record.id,
        "filename": record.filename,
        "invoice_number": fields.invoice_number,
        "seller": {"name": fields.seller_name, "tax_id": fields.seller_tax_id},
        "buyer": {"name": fields.buyer_name, "tax_id": fields.buyer_tax_id},
        "issue_date": fields.issue_date.isoformat() if fields.issue_date else None,
        "performance_date": fields.performance_date.isoformat() if fields.performance_date else None,
        "due_date": fields.due_date.isoformat() if fields.due_date else None,
        "currency": fields.currency,
        # Az összegek szövegként mennek ki, hogy a JSON-szám ne veszítsen pontosságot.
        "net_total": str(fields.net_total) if fields.net_total is not None else None,
        "vat_total": str(fields.vat_total) if fields.vat_total is not None else None,
        "gross_total": str(fields.gross_total) if fields.gross_total is not None else None,
        "direction": record.direction.value if record.direction else None,
        "partner": {"name": record.partner_name, "tax_id": record.partner_tax_id},
        "status": record.status,
        "issues": list(record.issues),
    }


@router.delete("")
async def delete_invoices(
    repository: InvoiceRepository = Depends(get_invoice_repository),
    jobs: JobTracker = Depends(get_jobs),
) -> dict:
    """Az összes kinyert számla és a hibás fájlok listájának törlése; feldolgozás közben nem engedett."""
    if jobs.has_active():
        raise ConflictError("processing is still in progress", code="processing_active")
    deleted = await repository.delete_all()
    return {"deleted": deleted, "cleared_failed": jobs.clear_failed()}


@router.get("")
async def list_invoices(repository: InvoiceRepository = Depends(get_invoice_repository)) -> dict:
    records = await repository.list_all()
    counts = Counter(record.status for record in records)
    return {
        "invoices": [_invoice_view(record) for record in records],
        "counts": {status: counts.get(status, 0) for status in _STATUSES},
    }
