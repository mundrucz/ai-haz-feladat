import asyncio

from app.services.job_service import JobTracker
from app.services.pdf_service import PdfText
from app.services.processing_service import DocumentProcessor
from app.services.upload_service import UploadOutcome, UploadService


class NoInvoices:
    async def exists(self, content_hash: str) -> bool:
        return False


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
