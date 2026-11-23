from collections import Counter

from fastapi import APIRouter, Depends, File, Query, UploadFile

from app.api.deps import get_repository, get_settings, get_translator, get_upload_service
from app.config import Settings
from app.documents import Document, DocumentRepository, DocumentStatus
from app.errors import InvalidInputError
from app.i18n import Translator
from app.upload import UploadResult, UploadService

router = APIRouter(prefix="/api/documents")


def _error(translator: Translator, code: str | None, fallback: str | None = None) -> dict | None:
    if code is None:
        return None
    message = translator.get(f"error.{code}") or fallback or code
    return {"code": code, "message": message}


def _document_view(document: Document, translator: Translator) -> dict:
    return {
        "id": document.id,
        "filename": document.original_name,
        "size_bytes": document.size_bytes,
        "status": document.status.value,
        "page_count": document.page_count,
        "text_chars": document.text_chars,
        "error": _error(translator, document.error_code, document.error_message),
        "uploaded_at": document.uploaded_at,
        "processed_at": document.processed_at,
    }


def _result_view(result: UploadResult, translator: Translator) -> dict:
    return {
        "filename": result.filename,
        "outcome": result.outcome.value,
        "document_id": result.document_id,
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
    settings: Settings = Depends(get_settings),
    service: UploadService = Depends(get_upload_service),
    translator: Translator = Depends(get_translator),
) -> dict:
    if not files:
        raise InvalidInputError("no files were uploaded", code="no_files")
    items = [(file.filename or "", await file.read(settings.max_upload_bytes + 1)) for file in files]
    results = await service.ingest_many(items)
    return {"results": [_result_view(result, translator) for result in results]}


@router.get("")
async def list_documents(
    ids: str | None = Query(default=None, description="vesszővel elválasztott azonosítók"),
    repository: DocumentRepository = Depends(get_repository),
    translator: Translator = Depends(get_translator),
) -> dict:
    documents = await repository.list_all(_parse_ids(ids) if ids is not None else None)
    counts = Counter(document.status for document in documents)
    return {
        "documents": [_document_view(document, translator) for document in documents],
        "counts": {status.value: counts.get(status, 0) for status in DocumentStatus},
    }
