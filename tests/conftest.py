"""Pytest fixtures for Ecolyxis.

Critical: env vars are set BEFORE importing the app so the .env loader
in config.py can't override us with production secrets. setdefault()
means real env vars win.
"""
import os
import sys
import tempfile

# Force test config before any app import.
_TEST_DB_FD, _TEST_DB_PATH = tempfile.mkstemp(suffix=".sqlite", prefix="ecolyxis-test-")
os.close(_TEST_DB_FD)

os.environ["DATABASE_URL"] = f"sqlite:///{_TEST_DB_PATH}"
os.environ["SECRET_KEY"] = "test-secret-key"
os.environ["STRIPE_SECRET_KEY"] = "sk_test_dummy"
os.environ["STRIPE_PUBLISHABLE_KEY"] = "pk_test_dummy"
os.environ["STRIPE_WEBHOOK_SECRET"] = "whsec_test_dummy"
os.environ["LLM_BASE_URL"] = "http://test-llm.invalid/v1"
os.environ["HIDREAM_URL"] = "http://test-hidream.invalid"
os.environ["WAN22_URL"] = "http://test-wan22.invalid"

# Put project root on path so `from app import create_app` works.
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import pytest  # noqa: E402
from app import create_app, db as _db  # noqa: E402


@pytest.fixture(scope="session")
def app():
    app = create_app(test_config={
        "TESTING": True,
        "SQLALCHEMY_DATABASE_URI": f"sqlite:///{_TEST_DB_PATH}",
        "WTF_CSRF_ENABLED": False,
    })

    # Mirror run.py: register the landing route on the app object.
    # The refactor will move this into the factory or a blueprint.
    from flask import render_template, redirect, url_for
    from flask_login import current_user

    @app.route("/")
    def landing():
        if current_user.is_authenticated:
            return redirect(url_for("dashboard.index"))
        return render_template("landing.html")

    yield app
    try:
        os.unlink(_TEST_DB_PATH)
    except OSError:
        pass


@pytest.fixture
def client(app):
    return app.test_client()


@pytest.fixture
def db(app):
    """Fresh tables per test."""
    with app.app_context():
        _db.drop_all()
        _db.create_all()
        yield _db
        _db.session.remove()
