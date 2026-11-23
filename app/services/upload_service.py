import hashlib
import logging
from collections.abc import Iterable
from dataclasses import dataclass
from enum import StrEnum
from pathlib import PurePath

from app.invoices import InvoiceRepository
from app.jobs import AddOutcome, JobTracker
from app.processing import DocumentProcessor

logger = logging.getLogger(__name__)

MAX_NAME_LENGTH = 255


def hash_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class UploadOutcome(StrEnum):
    ACCEPTED = "accepted"
    DUPLICATE = "duplicate"
    REJECTED = "rejected"


@dataclass(frozen=True)
class UploadResult:
    filename: str
    outcome: UploadOutcome
    job_id: int | None = None
    error_code: str | None = None


def _display_name(filename: str) -> str:
    name = PurePath(filename.replace("\\", "/")).name.strip()
    return (name or "unnamed")[:MAX_NAME_LENGTH]


class UploadService:
    """Fogadja a feltöltött fájlokat: duplikáció-szűrés, majd a feldolgozás sorba állítása.

    A PDF-et nem tároljuk: csak a tartalom hash-e és a megjelenítési fájlnév marad meg a kinyert adat mellett.
    """

    def __init__(
        self,
        jobs: JobTracker,
        invoices: InvoiceRepository,
        processor: DocumentProcessor,
        max_bytes: int,
    ) -> None:
        self._jobs = jobs
        self._invoices = invoices
        self._processor = processor
        self._max_bytes = max_bytes

    async def ingest(self, filename: str, data: bytes) -> UploadResult:
        name = _display_name(filename)
        if not data:
            return UploadResult(name, UploadOutcome.REJECTED, error_code="empty_file")
        if len(data) > self._max_bytes:
            return UploadResult(name, UploadOutcome.REJECTED, error_code="file_too_large")
        content_hash = hash_bytes(data)
        # Előbb a folyamatban lévők közé vesszük fel (szinkron), így a DB-vizsgálat alatt sem csúszhat át duplikátum.
        job, outcome = self._jobs.add(content_hash, name, len(data))
        if outcome is AddOutcome.DUPLICATE:
            return UploadResult(name, UploadOutcome.DUPLICATE, job.id)
        try:
            stored = await self._invoices.exists(content_hash)
        except Exception:
            logger.exception("upload failed", extra={"upload_name": name})
            self._jobs.finish(job.id)
            return UploadResult(name, UploadOutcome.REJECTED, error_code="unexpected_error")
        if stored:
            self._jobs.finish(job.id)
            return UploadResult(name, UploadOutcome.DUPLICATE)
        self._processor.enqueue(job.id, data)
        return UploadResult(name, UploadOutcome.ACCEPTED, job.id)

    async def ingest_many(self, files: Iterable[tuple[str, bytes]]) -> list[UploadResult]:
        return [await self.ingest(name, data) for name, data in files]
