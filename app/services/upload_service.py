import asyncio
import hashlib
import logging
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import date
from enum import StrEnum
from pathlib import PurePath

from app.extraction.validation import FUTURE_PERFORMANCE
from app.repositories.invoice_repository import InvoiceRecord, InvoiceRepository
from app.services.job_service import AddOutcome, JobTracker
from app.services.processing_service import DocumentProcessor

logger = logging.getLogger(__name__)

MAX_NAME_LENGTH = 255


def hash_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class UploadOutcome(StrEnum):
    ACCEPTED = "accepted"
    DUPLICATE = "duplicate"
    REJECTED = "rejected"
    UPDATED = "updated"


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

    Már tárolt tartalomnál nincs új feldolgozás (LLM-hívás sem); ha a számla jövőbeli teljesítés miatt maradt ki
    a kimutatásból, és a teljesítés napja azóta elérkezett, a jelölés lekerül (`UPDATED`).
    """

    def __init__(
        self,
        jobs: JobTracker,
        invoices: InvoiceRepository,
        processor: DocumentProcessor,
        max_bytes: int,
        *,
        today: Callable[[], date] = date.today,
    ) -> None:
        self._jobs = jobs
        self._invoices = invoices
        self._processor = processor
        self._max_bytes = max_bytes
        self._today = today

    def _reject(
        self, name: str, size: int, code: str, upload_id: str | None, content_hash: str | None = None
    ) -> UploadResult:
        self._jobs.record_rejection(name, size, code, upload_id, content_hash)
        return UploadResult(name, UploadOutcome.REJECTED, error_code=code)

    def _duplicate(
        self, name: str, size: int, upload_id: str | None, content_hash: str, job_id: int | None = None
    ) -> UploadResult:
        self._jobs.record_rejection(name, size, "duplicate_file", upload_id, content_hash)
        return UploadResult(name, UploadOutcome.DUPLICATE, job_id)

    async def ingest(self, filename: str, data: bytes, upload_id: str | None = None) -> UploadResult:
        name = _display_name(filename)
        if not data:
            return self._reject(name, 0, "empty_file", upload_id)
        if len(data) > self._max_bytes:
            return self._reject(name, len(data), "file_too_large", upload_id)
        content_hash = hash_bytes(data)
        # Előbb a folyamatban lévők közé vesszük fel (szinkron), így a DB-vizsgálat alatt sem csúszhat át duplikátum.
        job, outcome = self._jobs.add(content_hash, name, len(data), upload_id)
        if outcome is AddOutcome.DUPLICATE:
            return self._duplicate(name, len(data), upload_id, content_hash, job.id)
        try:
            stored = await self._invoices.find(content_hash)
            if stored is not None:
                self._jobs.finish(job.id)
                if await self._reevaluate(stored):
                    return UploadResult(name, UploadOutcome.UPDATED)
                return self._duplicate(name, len(data), upload_id, content_hash)
            # Tele sornál itt vár; így a kérés válasza, és vele a böngésző következő csomagja is késik.
            await self._processor.enqueue(job.id, data)
        except asyncio.CancelledError:
            # Megszakított kérés után ne maradjon örökké „sorban álló” bejegyzés, ami a sorba sosem került be.
            self._jobs.finish(job.id)
            raise
        except Exception:
            logger.exception("upload failed", extra={"upload_name": name})
            self._jobs.finish(job.id)
            return self._reject(name, len(data), "unexpected_error", upload_id, content_hash)
        return UploadResult(name, UploadOutcome.ACCEPTED, job.id)

    async def _reevaluate(self, stored: InvoiceRecord) -> bool:
        """A tárolt adatokból: ha a jövőbeli teljesítés napja már elérkezett, a jelölés lekerül."""
        if not stored.has_issue(FUTURE_PERFORMANCE) or stored.fields.performance_date > self._today():
            return False
        removed = await self._invoices.remove_issue(stored.content_hash, FUTURE_PERFORMANCE)
        if removed:
            logger.info("future performance flag cleared", extra={"invoice_id": stored.id})
        return removed

    async def ingest_many(
        self, files: Iterable[tuple[str, bytes]], upload_id: str | None = None
    ) -> list[UploadResult]:
        return [await self.ingest(name, data, upload_id) for name, data in files]
