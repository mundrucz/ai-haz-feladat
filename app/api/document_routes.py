from collections import Counter

from fastapi import APIRouter, Depends, File, Form, Path, Query, UploadFile

from app.api.dependencies import get_jobs, get_settings, get_translator, get_upload_service
from app.core.config import Settings
from app.core.errors import InvalidInputError
from app.core.i18n import Translator
from app.services.job_service import Job, JobStatus, JobTracker
from app.services.upload_service import UploadResult, UploadService

router = APIRouter(prefix="/api/documents")

MAX_UPLOAD_ID_LENGTH = 64


def _error(translator: Translator, code: str | None, message: str | None = None, fields=()) -> dict | None:
    if code is None:
        return None
    return {
        "code": code,
        "message": translator.get(f"error.{code}") or message or code,
        "fields": list(fields),
    }


def _job_view(job: Job, translator: Translator) -> dict:
    return {
        "id": job.id,
        "filename": job.filename,
        "size_bytes": job.size_bytes,
        "status": job.status.value,
        "error": _error(translator, job.error_code, job.error_message, job.error_fields),
        "uploaded_at": job.created_at,
    }


def _result_view(result: UploadResult, translator: Translator) -> dict:
    return {
        "filename": result.filename,
        "outcome": result.outcome.value,
        "document_id": result.job_id,
        "error": _error(translator, result.error_code),
    }


def _parse_ids(raw: str) -> list[int]:
    try:
        return [int(part) for part in raw.split(",") if part.strip()]
    except ValueError as exc:
        raise InvalidInputError("ids must be a comma separated list of integers", code="invalid_ids") from exc


@router.post("")
async def upload_documents(
    files: list[UploadFile] | None = File(default=None),
    upload_id: str | None = Form(default=None, max_length=MAX_UPLOAD_ID_LENGTH),
    settings: Settings = Depends(get_settings),
    service: UploadService = Depends(get_upload_service),
    translator: Translator = Depends(get_translator),
) -> dict:
    if not files:
        raise InvalidInputError("no files were uploaded", code="no_files")
    items = [(file.filename or "", await file.read(settings.max_upload_bytes + 1)) for file in files]
    results = await service.ingest_many(items, upload_id or None)
    return {"results": [_result_view(result, translator) for result in results]}


@router.post("/uploads/{upload_id}/cancel")
async def cancel_upload(
    upload_id: str = Path(max_length=MAX_UPLOAD_ID_LENGTH),
    jobs: JobTracker = Depends(get_jobs),
) -> dict:
    """Leállítás: a feltöltés még sorban álló fájljai megszakadnak, a már futók befejeződnek."""
    return {"cancelled": jobs.cancel_pending(upload_id)}


@router.delete("/failed")
async def clear_failed(jobs: JobTracker = Depends(get_jobs)) -> dict:
    """Új feltöltés előtt kiüríti a hibás fájlok listáját."""
    return {"cleared": jobs.clear_failed()}


@router.get("")
async def list_documents(
    ids: str | None = Query(default=None, description="vesszővel elválasztott azonosítók"),
    jobs: JobTracker = Depends(get_jobs),
    translator: Translator = Depends(get_translator),
) -> dict:
    """A folyamatban lévő és a hibás feldolgozások; a sikeres már a számlák között látszik."""
    listed = jobs.list_all(_parse_ids(ids) if ids is not None else None)
    counts = Counter(job.status for job in listed)
    return {
        "documents": [_job_view(job, translator) for job in listed],
        "counts": {status.value: counts.get(status, 0) for status in JobStatus},
    }
