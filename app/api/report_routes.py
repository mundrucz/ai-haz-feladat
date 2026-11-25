from fastapi import APIRouter, Depends

from app.api.dependencies import get_report_service
from app.services.report_service import ReportService

router = APIRouter(prefix="/api/report")


@router.get("")
async def get_report(service: ReportService = Depends(get_report_service)) -> dict:
    return await service.build()
