import asyncio
import json
import logging

import pytest
from pdfs import invoice_text, make_blank_pdf, make_encrypted_pdf, make_pdf

from app.jobs import MAX_FAILED_FILES, AddOutcome, JobTracker


def upload(client, *files):
    payload = [("files", (name, data, "application/pdf")) for name, data in files]
    return client.post("/api/documents", files=payload)


def wait_until_processed(client, app, timeout=10.0):
    """A háttérfeldolgozás befejezését várja meg a tesztben használt eseményhurkon."""
    portal = client.portal
    portal.call(asyncio.wait_for, app.state.processor.join(), timeout)


def documents(client):
    return client.get("/api/documents").json()


def invoices(client):
    return client.get("/api/invoices").json()["invoices"]


def test_successful_upload_stores_only_the_invoice_data(client, app, settings):
    response = upload(client, ("szamla.pdf", make_pdf(invoice_text())))

    assert response.status_code == 200
    result = response.json()["results"][0]
    assert result["outcome"] == "accepted"
    assert result["error"] is None

    wait_until_processed(client, app)
    assert documents(client) == {"documents": [], "counts": {"pending": 0, "processing": 0, "failed": 0}}
    (invoice,) = invoices(client)
    assert invoice["filename"] == "szamla.pdf"
    assert not list(settings.database_path.parent.glob("**/*.pdf"))


def test_same_content_is_not_processed_twice_even_with_other_name(client, app):
    data = make_pdf(invoice_text("ugyanaz-1"))
    upload(client, ("a.pdf", data))
    wait_until_processed(client, app)

    second = upload(client, ("masik-nev.pdf", data)).json()["results"][0]

    assert second["outcome"] == "duplicate"
    wait_until_processed(client, app)
    assert len(invoices(client)) == 1


def test_duplicates_within_one_batch_create_one_invoice(client, app):
    data = make_pdf(invoice_text())

    results = upload(client, *[(f"{i}.pdf", data) for i in range(5)]).json()["results"]

    outcomes = sorted(r["outcome"] for r in results)
    assert outcomes == ["accepted"] + ["duplicate"] * 4
    wait_until_processed(client, app)
    assert len(invoices(client)) == 1


def test_bad_files_are_reported_as_errors_and_nothing_is_stored(client, app):
    files = [
        ("jo.pdf", make_pdf(invoice_text())),
        ("nem-pdf.pdf", b"nem pdf tartalom"),
        ("jelszavas.pdf", make_encrypted_pdf()),
        ("ures-oldal.pdf", make_blank_pdf()),
        ("serult.pdf", make_pdf("x")[:200]),
        ("level.pdf", make_pdf("Ez csak egy levél, nincs benne semmi.")),
    ]

    results = upload(client, *files).json()["results"]
    assert {r["outcome"] for r in results} == {"accepted"}
    wait_until_processed(client, app)

    by_name = {d["filename"]: d for d in documents(client)["documents"]}
    assert "jo.pdf" not in by_name
    assert by_name["nem-pdf.pdf"]["error"]["code"] == "not_a_pdf"
    assert by_name["jelszavas.pdf"]["error"]["code"] == "password_protected"
    assert by_name["ures-oldal.pdf"]["error"]["code"] == "no_text"
    assert by_name["serult.pdf"]["status"] == "failed"
    assert by_name["level.pdf"]["error"]["code"] == "no_invoice_found"
    assert by_name["nem-pdf.pdf"]["error"]["message"]
    assert [i["filename"] for i in invoices(client)] == ["jo.pdf"]


def test_invoice_with_missing_field_is_an_error_and_is_not_stored(client, app):
    text = invoice_text().replace("Fizetési határidő: 2026.02.11.\n", "")

    upload(client, ("hianyos.pdf", make_pdf(text)))
    wait_until_processed(client, app)

    (job,) = documents(client)["documents"]
    assert job["status"] == "failed"
    assert job["error"]["code"] == "incomplete_data"
    assert job["error"]["fields"] == ["due_date"]
    assert invoices(client) == []


def test_reuploading_failed_file_reprocesses_it(client, app):
    data = make_blank_pdf()
    upload(client, ("ures.pdf", data))
    wait_until_processed(client, app)
    assert documents(client)["documents"][0]["status"] == "failed"

    result = upload(client, ("ures.pdf", data)).json()["results"][0]

    assert result["outcome"] == "accepted"
    wait_until_processed(client, app)
    assert len(documents(client)["documents"]) == 1


def test_empty_file_is_rejected_and_listed_with_its_reason(client):
    result = upload(client, ("ures.pdf", b"")).json()["results"][0]

    assert result["outcome"] == "rejected"
    assert result["error"]["code"] == "empty_file"
    [entry] = documents(client)["documents"]
    assert (entry["filename"], entry["status"], entry["error"]["code"]) == ("ures.pdf", "failed", "empty_file")


def test_too_large_file_is_rejected(client, app):
    app.state.upload_service._max_bytes = 100

    result = upload(client, ("nagy.pdf", make_pdf("x" * 500))).json()["results"][0]

    assert result["outcome"] == "rejected"
    assert result["error"]["code"] == "file_too_large"
    [entry] = documents(client)["documents"]
    assert (entry["filename"], entry["error"]["code"]) == ("nagy.pdf", "file_too_large")


def test_duplicate_upload_is_listed_with_duplicate_reason(client, app):
    data = make_pdf(invoice_text("dup-lista-1"))
    upload(client, ("a.pdf", data))
    wait_until_processed(client, app)

    upload(client, ("masolat.pdf", data))

    [entry] = documents(client)["documents"]
    assert (entry["filename"], entry["status"], entry["error"]["code"]) == ("masolat.pdf", "failed", "duplicate_file")


def test_failed_files_are_bounded_and_the_oldest_drop_out():
    tracker = JobTracker()
    processed, _ = tracker.add("h-failed", "feldolgozott.pdf", 1)
    tracker.claim(processed.id)
    tracker.mark_failed(processed.id, "no_text", "üres")

    for index in range(MAX_FAILED_FILES + 5):
        tracker.record_rejection(f"{index}.pdf", 1, "empty_file")

    listed = tracker.list_all()
    assert len(listed) == MAX_FAILED_FILES
    assert listed[0].filename == f"{MAX_FAILED_FILES + 4}.pdf"
    assert tracker.get(processed.id) is None
    assert tracker.add("h-failed", "feldolgozott.pdf", 1)[1] is AddOutcome.CREATED


def test_active_jobs_do_not_count_towards_the_failed_limit():
    tracker = JobTracker()
    active = [tracker.add(f"h{index}", f"{index}.pdf", 1)[0] for index in range(MAX_FAILED_FILES + 5)]

    for index in range(MAX_FAILED_FILES):
        tracker.record_rejection(f"x{index}.pdf", 1, "empty_file")

    assert len(tracker.list_all()) == 2 * MAX_FAILED_FILES + 5
    assert all(tracker.get(job.id) is not None for job in active)


def test_every_failed_file_is_written_to_the_failed_files_log(client, app, settings):
    upload(client, ("ures.pdf", b""))
    upload(client, ("hibas.pdf", make_blank_pdf()))
    wait_until_processed(client, app)
    client.delete("/api/documents/failed")

    logging.getLogger("app.failed_files").handlers[0].flush()
    lines = [json.loads(line) for line in settings.failed_files_log_path.read_text(encoding="utf-8").splitlines()]
    assert [(line["file_name"], line["error_code"]) for line in lines] == [
        ("ures.pdf", "empty_file"),
        ("hibas.pdf", "no_text"),
    ]
    assert all(line["uploaded_at"] and "size_bytes" in line for line in lines)
    assert [(line["source"], line["error_text"]) for line in lines] == [
        ("intake", "A fájl üres."),
        ("processing", "A PDF-ben nincs szöveges réteg (üres vagy szkennelt dokumentum)."),
    ]
    assert lines[1]["content_hash"]


def test_cancel_endpoint_stops_queued_files_of_the_upload(client, app):
    jobs = app.state.jobs
    queued, _ = jobs.add("h-queued", "sorban.pdf", 1, upload_id="u-cancel")
    other, _ = jobs.add("h-other", "masik.pdf", 1, upload_id="u-other")

    response = client.post("/api/documents/uploads/u-cancel/cancel")

    assert response.status_code == 200
    assert response.json() == {"cancelled": 1}
    entries = {entry["filename"]: entry for entry in documents(client)["documents"]}
    assert entries["sorban.pdf"]["status"] == "failed"
    assert entries["sorban.pdf"]["error"]["code"] == "cancelled"
    assert "újra feltölthető" in entries["sorban.pdf"]["error"]["message"]
    assert entries["masik.pdf"]["status"] == "pending"
    assert jobs.claim(queued.id) is None
    assert jobs.claim(other.id) is not None


def test_clear_failed_endpoint_empties_the_failed_list_only(client, app):
    upload(client, ("ures.pdf", b""))
    upload(client, ("hibas.pdf", make_blank_pdf()))
    wait_until_processed(client, app)
    waiting, _ = app.state.jobs.add("h-waiting", "var.pdf", 1)
    assert len(documents(client)["documents"]) == 3

    response = client.delete("/api/documents/failed")

    assert response.json() == {"cleared": 2}
    assert [entry["filename"] for entry in documents(client)["documents"]] == ["var.pdf"]
    assert app.state.jobs.get(waiting.id) is not None


def test_cancel_endpoint_for_unknown_upload_cancels_nothing(client):
    assert client.post("/api/documents/uploads/nincs-ilyen/cancel").json() == {"cancelled": 0}


def test_upload_accepts_an_upload_id_and_rejects_an_overlong_one(client, app):
    payload = [("files", ("szamla.pdf", make_pdf(invoice_text("upload-id-1")), "application/pdf"))]

    ok = client.post("/api/documents", files=payload, data={"upload_id": "u-1"})
    too_long = client.post("/api/documents", files=payload, data={"upload_id": "x" * 65})

    assert ok.status_code == 200
    assert too_long.status_code == 422
    wait_until_processed(client, app)


def test_upload_without_files_is_a_clear_client_error(client):
    response = client.post("/api/documents")

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "no_files"


def test_listing_can_be_filtered_by_ids(client, app):
    first = upload(client, ("a.pdf", make_blank_pdf())).json()["results"][0]["document_id"]
    upload(client, ("b.pdf", make_pdf("b")))
    wait_until_processed(client, app)

    only_first = client.get("/api/documents", params={"ids": str(first)}).json()

    assert [d["id"] for d in only_first["documents"]] == [first]
    assert client.get("/api/documents", params={"ids": "x"}).status_code == 400


def test_filename_is_reduced_to_a_display_name(client, app):
    result = upload(client, ("..\\..\\etc/passwd.pdf", make_pdf("út"))).json()["results"][0]

    assert result["filename"] == "passwd.pdf"


def test_index_page_contains_upload_ui(client):
    html = client.get("/").text

    assert 'id="dropzone"' in html
    assert "/static/app.js" in html


def test_index_page_has_hidden_leave_warning_and_translated_text(client):
    html = client.get("/").text

    assert 'id="upload-warning"' in html
    assert "elvesznek" in html
    assert "ui.upload.leave_warning" in html


@pytest.mark.parametrize("count", [30])
def test_many_files_are_all_processed(client, app, count):
    files = [(f"{i}.pdf", make_pdf(invoice_text(f"NW-2026-{i:04d}"))) for i in range(count)]

    results = upload(client, *files).json()["results"]
    wait_until_processed(client, app)

    assert all(r["outcome"] == "accepted" for r in results)
    assert len(invoices(client)) == count
    assert documents(client)["documents"] == []
