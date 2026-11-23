from app.errors import InvalidInputError


def test_health_is_ok_with_working_database(client):
    response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok", "database": "ok"}


def test_health_reports_degraded_when_database_is_down(client, app, tmp_path):
    app.state.db.path = tmp_path / "gone" / "app.db"

    response = client.get("/health")

    assert response.status_code == 503
    assert response.json()["status"] == "degraded"


def test_index_renders_hungarian_page_with_company_data(client):
    response = client.get("/")

    assert response.status_code == 200
    assert "Számlafeldolgozó" in response.text
    assert "Nordwind Solutions Kft." in response.text
    assert "26481937-2-43" in response.text


def test_static_assets_are_served(client):
    assert client.get("/static/style.css").status_code == 200
    assert client.get("/static/app.js").status_code == 200


def test_app_errors_become_structured_json(client, app):
    @app.get("/_test/app-error")
    async def app_error():
        raise InvalidInputError("rossz bemenet")

    response = client.get("/_test/app-error")

    assert response.status_code == 400
    assert response.json() == {"error": {"code": "invalid_input", "message": "rossz bemenet"}}


def test_unexpected_errors_do_not_kill_the_app(client, app):
    @app.get("/_test/boom")
    async def boom():
        raise RuntimeError("váratlan")

    response = client.get("/_test/boom")

    assert response.status_code == 500
    assert response.json()["error"]["code"] == "internal_error"
    assert "váratlan" not in response.text
    assert client.get("/health").status_code == 200
