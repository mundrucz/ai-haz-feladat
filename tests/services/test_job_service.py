from app.jobs import AddOutcome, JobStatus, JobTracker


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
