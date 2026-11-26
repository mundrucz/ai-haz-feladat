import asyncio
from datetime import date
from decimal import Decimal

import pytest

from app.db.database import Database
from app.extraction.models import InvoiceFields
from app.repositories.invoice_repository import Classification, InvoiceRepository
from app.services.job_service import JobTracker
from app.services.pdf_service import PdfText
from app.services.processing_service import DocumentProcessor
from app.services.upload_service import UploadOutcome, UploadService, hash_bytes


class NoInvoices:
    async def find(self, content_hash: str):
        return None


async def text_of(data: bytes) -> PdfText:
    return PdfText(pages=["x"])


async def noop_analysis(content_hash: str, filename: str, text: str) -> None:
    return None


def service_with(processor: DocumentProcessor, jobs: JobTracker) -> UploadService:
    return UploadService(jobs, NoInvoices(), processor, max_bytes=1000)


async def test_full_queue_makes_ingest_wait_until_a_worker_frees_a_slot():
    jobs = JobTracker()
    processor = DocumentProcessor(jobs, text_of, noop_analysis, concurrency=1, queue_size=1)
    service = service_with(processor, jobs)

    first = await asyncio.wait_for(service.ingest("a.pdf", b"a"), 1)
    second = asyncio.create_task(service.ingest("b.pdf", b"b"))
    await asyncio.sleep(0.05)

    assert first.outcome is UploadOutcome.ACCEPTED
    assert not second.done()

    await processor.start()
    result = await asyncio.wait_for(second, 1)
    await asyncio.wait_for(processor.join(), 1)
    await processor.stop()

    assert result.outcome is UploadOutcome.ACCEPTED
    assert jobs.list_all() == []


async def test_cancelled_waiting_ingest_leaves_no_stuck_pending_job():
    jobs = JobTracker()
    processor = DocumentProcessor(jobs, text_of, noop_analysis, concurrency=1, queue_size=1)
    service = service_with(processor, jobs)
    await service.ingest("a.pdf", b"a")

    waiting = asyncio.create_task(service.ingest("b.pdf", b"b"))
    await asyncio.sleep(0.05)
    waiting.cancel()
    await asyncio.gather(waiting, return_exceptions=True)

    assert [job.filename for job in jobs.list_all()] == ["a.pdf"]


async def test_stopped_upload_leaves_no_trace_even_for_the_waiting_request(failed_log):
    jobs = JobTracker()
    processor = DocumentProcessor(jobs, text_of, noop_analysis, concurrency=1, queue_size=1)
    service = service_with(processor, jobs)
    await service.ingest("a.pdf", b"a", upload_id="u1")

    waiting = asyncio.create_task(service.ingest("b.pdf", b"b", upload_id="u1"))
    await asyncio.sleep(0.05)
    assert jobs.cancel_pending("u1") == 2
    waiting.cancel()
    await asyncio.gather(waiting, return_exceptions=True)

    assert jobs.list_all() == []
    assert list(failed_log) == []


PDF = b"%PDF stored invoice"
PERFORMANCE = date(2026, 12, 1)
FLAG = {"code": "future_performance", "field": "performance_date", "severity": "info"}


class NoProcessing:
    async def enqueue(self, job_id: int, data: bytes) -> None:
        raise AssertionError("a stored file must not be processed (or sent to the LLM) again")


@pytest.fixture
async def stored(tmp_path):
    db = Database(tmp_path / "u.db")
    await db.init()
    repository = InvoiceRepository(db)
    fields = InvoiceFields(
        invoice_number="F-1",
        seller_name="A Kft.",
        seller_tax_id="1",
        buyer_name="B Kft.",
        buyer_tax_id="2",
        issue_date=date(2026, 11, 20),
        performance_date=PERFORMANCE,
        due_date=date(2026, 12, 20),
        currency="EUR",
        net_total=None,
        vat_total=None,
        gross_total=Decimal("100"),
    )
    await repository.save(hash_bytes(PDF), "f.pdf", fields, Classification(None, None, None), "ok", [FLAG])
    return repository


def reupload_service(repository: InvoiceRepository, today: date) -> UploadService:
    return UploadService(JobTracker(), repository, NoProcessing(), max_bytes=1000, today=lambda: today)


async def test_reupload_before_the_performance_day_keeps_the_invoice_out(stored):
    result = await reupload_service(stored, date(2026, 11, 30)).ingest("f.pdf", PDF)

    assert result.outcome is UploadOutcome.DUPLICATE
    assert (await stored.find(hash_bytes(PDF))).has_issue("future_performance")


async def test_reupload_from_the_performance_day_clears_the_flag_without_reprocessing(stored):
    result = await reupload_service(stored, PERFORMANCE).ingest("f.pdf", PDF)

    assert result.outcome is UploadOutcome.UPDATED
    record = await stored.find(hash_bytes(PDF))
    assert not record.has_issue("future_performance")
    assert record.status == "ok"

    again = await reupload_service(stored, PERFORMANCE).ingest("f.pdf", PDF)
    assert again.outcome is UploadOutcome.DUPLICATE
