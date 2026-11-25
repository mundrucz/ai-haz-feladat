from app.core.i18n import Translator
from app.main import WEB_DIR
from app.services.failed_file_service import FailedFileLog
from app.services.job_service import AddOutcome, JobStatus, JobTracker


def test_failures_are_logged_with_source_hash_upload_and_hungarian_reason(failed_log):
    jobs = JobTracker(FailedFileLog(Translator(WEB_DIR / "locales", "hu")))
    jobs.record_rejection("ures.pdf", 0, "empty_file", "u1")
    jobs.record_rejection("masolat.pdf", 5, "duplicate_file", "u1", "hash-dup")
    running, _ = jobs.add("hash-run", "futo.pdf", 7, upload_id="u1")
    jobs.claim(running.id)
    jobs.mark_failed(running.id, "incomplete_data", "missing", ("due_date",))
    queued, _ = jobs.add("hash-queued", "sorban.pdf", 9, upload_id="u2")
    jobs.cancel_pending("u2")

    seen = {r.file_name: r for r in failed_log}
    assert [r.file_name for r in failed_log] == ["ures.pdf", "masolat.pdf", "futo.pdf", "sorban.pdf"]
    assert (seen["ures.pdf"].source, seen["ures.pdf"].upload_id, seen["ures.pdf"].error_text) == (
        "intake",
        "u1",
        "A fájl üres.",
    )
    assert (seen["masolat.pdf"].content_hash, seen["masolat.pdf"].size_bytes) == ("hash-dup", 5)
    assert (seen["futo.pdf"].source, seen["futo.pdf"].content_hash) == ("processing", "hash-run")
    assert seen["futo.pdf"].error_fields == ["due_date"]
    assert seen["futo.pdf"].error_message == "missing"
    assert seen["futo.pdf"].job_id == running.id
    assert (seen["sorban.pdf"].source, seen["sorban.pdf"].upload_id) == ("cancelled", "u2")
    assert seen["sorban.pdf"].error_text.startswith("Megszakítva")
    assert queued.id == seen["sorban.pdf"].job_id


def test_new_content_is_created_pending_and_in_progress_content_is_duplicate():
    jobs = JobTracker()

    job, outcome = jobs.add("h1", "a.pdf", 10)
    again, again_outcome = jobs.add("h1", "b.pdf", 10)

    assert (outcome, again_outcome) == (AddOutcome.CREATED, AddOutcome.DUPLICATE)
    assert again.id == job.id
    assert job.status is JobStatus.PENDING


def test_failed_job_can_be_requeued_and_loses_its_error():
    jobs = JobTracker()
    job, _ = jobs.add("h1", "a.pdf", 10)
    jobs.claim(job.id)
    jobs.mark_failed(job.id, "no_text", "üres", ("x",))
    job.created_at = "2000-01-01T00:00:00+00:00"

    again, outcome = jobs.add("h1", "uj-nev.pdf", 10)

    assert outcome is AddOutcome.REQUEUED
    assert (again.id, again.status, again.filename) == (job.id, JobStatus.PENDING, "uj-nev.pdf")
    assert (again.error_code, again.error_message, again.error_fields) == (None, None, ())
    assert again.created_at > "2000-01-01T00:00:00+00:00"


def test_claim_succeeds_only_once():
    jobs = JobTracker()
    job, _ = jobs.add("h1", "a.pdf", 10)

    assert jobs.claim(job.id) is not None
    assert jobs.claim(job.id) is None
    assert jobs.claim(999) is None


def test_finished_job_is_forgotten_and_same_content_can_be_added_again():
    jobs = JobTracker()
    job, _ = jobs.add("h1", "a.pdf", 10)

    jobs.finish(job.id)

    assert jobs.get(job.id) is None
    assert jobs.list_all() == []
    assert jobs.add("h1", "a.pdf", 10)[1] is AddOutcome.CREATED


def test_listing_is_newest_first_and_can_be_filtered_by_ids():
    jobs = JobTracker()
    first, _ = jobs.add("h1", "a.pdf", 1)
    second, _ = jobs.add("h2", "b.pdf", 1)

    assert [job.id for job in jobs.list_all()] == [second.id, first.id]
    assert [job.id for job in jobs.list_all([first.id, 999])] == [first.id]


def test_cancel_pending_affects_only_pending_jobs_of_that_upload():
    jobs = JobTracker()
    mine, _ = jobs.add("h1", "a.pdf", 1, upload_id="u1")
    running, _ = jobs.add("h2", "b.pdf", 1, upload_id="u1")
    other, _ = jobs.add("h3", "c.pdf", 1, upload_id="u2")
    jobs.claim(running.id)

    assert jobs.cancel_pending("u1") == 1

    assert (mine.status, mine.error_code) == (JobStatus.FAILED, "cancelled")
    assert running.status is JobStatus.PROCESSING
    assert other.status is JobStatus.PENDING
    assert jobs.claim(mine.id) is None
    assert jobs.cancel_pending("u1") == 0


def test_clear_failed_forgets_failed_and_rejected_files_but_keeps_active_ones():
    jobs = JobTracker()
    failed, _ = jobs.add("h1", "a.pdf", 1)
    jobs.claim(failed.id)
    jobs.mark_failed(failed.id, "no_text", "üres")
    pending, _ = jobs.add("h2", "b.pdf", 1)
    running, _ = jobs.add("h3", "c.pdf", 1)
    jobs.claim(running.id)
    jobs.record_rejection("ures.pdf", 0, "empty_file")

    assert jobs.clear_failed() == 2

    assert {job.id for job in jobs.list_all()} == {pending.id, running.id}
    assert jobs.add("h1", "a.pdf", 1)[1] is AddOutcome.CREATED
    assert jobs.clear_failed() == 0


def test_has_active_counts_only_pending_and_processing_jobs():
    jobs = JobTracker()
    assert not jobs.has_active()
    job, _ = jobs.add("h1", "a.pdf", 1)
    assert jobs.has_active()
    jobs.claim(job.id)
    assert jobs.has_active()
    jobs.mark_failed(job.id, "no_text", "üres")
    assert not jobs.has_active()


def test_cancelled_file_can_be_uploaded_again():
    jobs = JobTracker()
    job, _ = jobs.add("h1", "a.pdf", 1, upload_id="u1")
    jobs.cancel_pending("u1")

    again, outcome = jobs.add("h1", "a.pdf", 1, upload_id="u2")

    assert outcome is AddOutcome.REQUEUED
    assert (again.id, again.status, again.upload_id) == (job.id, JobStatus.PENDING, "u2")
