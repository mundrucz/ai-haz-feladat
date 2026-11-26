import asyncio

from pdfs import make_pdf

from app.core.errors import ProcessingError
from app.services.job_service import JobStatus, JobTracker
from app.services.pdf_service import PdfText
from app.services.processing_service import DocumentProcessor


def add_job(jobs: JobTracker, name: str):
    data = make_pdf(name)
    job, _ = jobs.add(name * 8, name, len(data))
    return job, data


async def noop_analysis(content_hash: str, filename: str, text: str) -> None:
    return None


async def test_unexpected_extractor_error_fails_only_that_job_and_finished_job_disappears():
    jobs = JobTracker()
    good, good_data = add_job(jobs, "jo")
    bad, bad_data = add_job(jobs, "rossz")

    async def extractor(data: bytes) -> PdfText:
        if data == bad_data:
            raise RuntimeError("váratlan")
        return PdfText(pages=["szöveg"])

    processor = DocumentProcessor(jobs, extractor, noop_analysis, concurrency=2)
    await processor.start()
    await processor.enqueue(good.id, good_data)
    await processor.enqueue(bad.id, bad_data)
    await asyncio.wait_for(processor.join(), 5)
    await processor.stop()

    assert jobs.get(good.id) is None
    failed = jobs.get(bad.id)
    assert failed.status is JobStatus.FAILED
    assert failed.error_code == "unexpected_error"


async def test_app_error_keeps_its_code_and_fields():
    jobs = JobTracker()
    job, data = add_job(jobs, "x")

    async def extractor(data: bytes) -> PdfText:
        return PdfText(pages=["x"])

    async def analysis(content_hash: str, filename: str, text: str) -> None:
        raise ProcessingError("hiányos", code="incomplete_data", fields=("due_date",))

    await DocumentProcessor(jobs, extractor, analysis).process(job.id, data)

    failed = jobs.get(job.id)
    assert (failed.error_code, failed.error_fields) == ("incomplete_data", ("due_date",))


async def test_analysis_receives_hash_filename_and_extracted_text():
    jobs = JobTracker()
    job, data = add_job(jobs, "abc")
    seen = []

    async def extractor(data: bytes) -> PdfText:
        return PdfText(pages=["egy", "kettő"])

    async def analysis(content_hash: str, filename: str, text: str) -> None:
        seen.append((content_hash, filename, text))

    await DocumentProcessor(jobs, extractor, analysis).process(job.id, data)

    assert seen == [(job.content_hash, "abc", PdfText(pages=["egy", "kettő"]).text)]


async def test_processing_the_same_job_twice_runs_once():
    jobs = JobTracker()
    job, data = add_job(jobs, "egyszer")
    calls = 0

    async def extractor(data: bytes) -> PdfText:
        nonlocal calls
        calls += 1
        await asyncio.sleep(0.05)
        return PdfText(pages=["x"])

    processor = DocumentProcessor(jobs, extractor, noop_analysis)
    await asyncio.gather(processor.process(job.id, data), processor.process(job.id, data))

    assert calls == 1
