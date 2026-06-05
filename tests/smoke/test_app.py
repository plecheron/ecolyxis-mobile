"""Smoke tests: app boots, routes are registered, basic pages render."""


def test_app_imports(app):
    assert app is not None
    assert app.config["TESTING"] is True


def test_landing_page(client):
    resp = client.get("/")
    assert resp.status_code == 200
    assert b"Ecolyxis" in resp.data


def test_login_page(client):
    resp = client.get("/login")
    assert resp.status_code == 200


def test_signup_page(client):
    resp = client.get("/signup")
    assert resp.status_code == 200


def test_blueprints_registered(app):
    # NB: "admin" is intentionally not registered here — it runs as a
    # standalone controller service (see app/admin.disabled/).
    expected = {
        "auth", "dashboard", "chat", "billing",
        "contact", "api", "apikeys", "wallet", "blog",
        "legal", "health", "pricing", "jobs",
    }
    assert expected.issubset(set(app.blueprints.keys()))
