from fastapi import APIRouter, Depends, Query

from app.api.dependencies import get_invoice_repository, get_jobs, get_report_service
from app.core.errors import ConflictError
from app.repositories.invoice_repository import InvoiceGroup, InvoiceRepository
from app.services.job_service import JobTracker
from app.services.report_service import MAX_PAGE_SIZE, ReportService

router = APIRouter(prefix="/api/invoices")

DEFAULT_PAGE_SIZE = 50


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
async def list_invoices(
    group: InvoiceGroup = InvoiceGroup.ALL,
    offset: int = Query(0, ge=0),
    limit: int = Query(DEFAULT_PAGE_SIZE, ge=1, le=MAX_PAGE_SIZE),
    service: ReportService = Depends(get_report_service),
) -> dict:
    """A számlalista egy oldala (a csoport szerint szűrve), a tárolt átváltással és a csoportok elemszámával."""
    return await service.invoice_page(group, offset, limit)
