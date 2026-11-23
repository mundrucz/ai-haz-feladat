import asyncio
import logging
from collections.abc import Iterable
from dataclasses import dataclass
from enum import StrEnum
from pathlib import PurePath

from app.documents import AddOutcome, DocumentRepository
from app.processing import DocumentProcessor
from app.storage import FileStore, hash_bytes

logger = logging.getLogger(__name__)

MAX_NAME_LENGTH = 255
STORE_CONCURRENCY = 8


class UploadOutcome(StrEnum):
    ACCEPTED = "accepted"
    DUPLICATE = "duplicate"
    REJECTED = "rejected"


@dataclass(frozen=True)
class UploadResult:
    filename: str
    outcome: UploadOutcome
    document_id: int | None = None
    error_code: str | None = None


def _display_name(filename: str) -> str:
    name = PurePath(filename.replace("\\", "/")).name.strip()
    return (name or "unnamed")[:MAX_NAME_LENGTH]


class UploadService:
    """Fogadja a feltöltött fájlokat: mentés, duplikáció-szűrés, feldolgozás sorba állítása.

    A fájlnevet csak megjelenítésre tároljuk, a lemezre a tartalom hash-e kerül.
    """

    def __init__(
        self,
        repository: DocumentRepository,
        store: FileStore,
        processor: DocumentProcessor,
        max_bytes: int,
    ) -> None:
        self._repository = repository
        self._store = store
        self._processor = processor
        self._max_bytes = max_bytes

    async def ingest(self, filename: str, data: bytes) -> UploadResult:
        name = _display_name(filename)
        if not data:
            return UploadResult(name, UploadOutcome.REJECTED, error_code="empty_file")
        if len(data) > self._max_bytes:
            return UploadResult(name, UploadOutcome.REJECTED, error_code="file_too_large")
        try:
            content_hash = hash_bytes(data)
            if not await self._store.exists(content_hash):
                await self._store.save(content_hash, data)
            document, outcome = await self._repository.add(content_hash, name, len(data))
        except Exception:
            logger.exception("upload failed", extra={"upload_name": name})
            return UploadResult(name, UploadOutcome.REJECTED, error_code="storage_error")
        if outcome is AddOutcome.DUPLICATE:
            return UploadResult(name, UploadOutcome.DUPLICATE, document.id)
        self._processor.enqueue(document.id)
        return UploadResult(name, UploadOutcome.ACCEPTED, document.id)

    async def ingest_many(self, files: Iterable[tuple[str, bytes]]) -> list[UploadResult]:
        semaphore = asyncio.Semaphore(STORE_CONCURRENCY)

        async def run(filename: str, data: bytes) -> UploadResult:
            async with semaphore:
                return await self.ingest(filename, data)

        return list(await asyncio.gather(*(run(name, data) for name, data in files)))
