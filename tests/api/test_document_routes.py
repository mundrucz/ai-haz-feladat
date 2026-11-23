import asyncio

import pytest
from pdfs import invoice_text, make_blank_pdf, make_encrypted_pdf, make_pdf


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


def test_empty_file_is_rejected_without_record(client):
    result = upload(client, ("ures.pdf", b"")).json()["results"][0]

    assert result["outcome"] == "rejected"
    assert result["error"]["code"] == "empty_file"
    assert documents(client)["documents"] == []


def test_too_large_file_is_rejected(client, app):
    app.state.upload_service._max_bytes = 100

    result = upload(client, ("nagy.pdf", make_pdf("x" * 500))).json()["results"][0]

    assert result["outcome"] == "rejected"
    assert result["error"]["code"] == "file_too_large"
    assert documents(client)["documents"] == []


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
