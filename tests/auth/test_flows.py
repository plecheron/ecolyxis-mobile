"""Auth flows: signup (with CAPTCHA + honeypot + time gate), login, logout."""
import time
import pytest

from app.models import User
from app import auth as auth_module


@pytest.fixture(autouse=True)
def reset_signup_rate_limit():
    """The IP rate limiter is a module-level dict; clear between tests."""
    auth_module._signup_attempts.clear()
    yield
    auth_module._signup_attempts.clear()


def _prep_signup_session(client, answer="5"):
    """Set up session as if GET /signup had populated it."""
    with client.session_transaction() as sess:
        sess["captcha_answer"] = answer
        sess["captcha_time"] = time.time() - 10  # past the 3s gate


def test_signup_creates_user(client, db):
    _prep_signup_session(client)
    resp = client.post("/signup", data={
        "email": "newuser@example.com",
        "password": "password123",
        "confirm": "password123",
        "captcha": "5",
        "website": "",
    })
    assert resp.status_code == 303
    user = User.query.filter_by(email="newuser@example.com").first()
    assert user is not None
    assert user.check_password("password123")
    assert not user.check_password("wrong")


def test_signup_rejects_honeypot(client, db):
    _prep_signup_session(client)
    resp = client.post("/signup", data={
        "email": "bot@example.com",
        "password": "password123",
        "confirm": "password123",
        "captcha": "5",
        "website": "http://spam.example.com",  # bot fills hidden field
    })
    assert resp.status_code == 302  # redirect back to /signup
    assert User.query.filter_by(email="bot@example.com").first() is None


def test_signup_rejects_wrong_captcha(client, db):
    _prep_signup_session(client, answer="42")
    resp = client.post("/signup", data={
        "email": "foo@example.com",
        "password": "password123",
        "confirm": "password123",
        "captcha": "7",  # wrong answer
        "website": "",
    })
    assert resp.status_code == 200  # form re-rendered with error
    assert User.query.filter_by(email="foo@example.com").first() is None


def test_signup_rejects_short_password(client, db):
    _prep_signup_session(client)
    resp = client.post("/signup", data={
        "email": "weak@example.com",
        "password": "123",
        "confirm": "123",
        "captcha": "5",
        "website": "",
    })
    assert resp.status_code == 200
    assert User.query.filter_by(email="weak@example.com").first() is None


def test_signup_rejects_mismatched_passwords(client, db):
    _prep_signup_session(client)
    resp = client.post("/signup", data={
        "email": "mismatch@example.com",
        "password": "password123",
        "confirm": "different456",
        "captcha": "5",
        "website": "",
    })
    assert resp.status_code == 200
    assert User.query.filter_by(email="mismatch@example.com").first() is None


def test_signup_rejects_duplicate_email(client, db, make_user):
    make_user(email="dup@example.com")
    _prep_signup_session(client)
    resp = client.post("/signup", data={
        "email": "dup@example.com",
        "password": "password123",
        "confirm": "password123",
        "captcha": "5",
        "website": "",
    })
    assert resp.status_code == 200
    assert User.query.filter_by(email="dup@example.com").count() == 1


def test_signup_ip_rate_limit(client, db):
    # Burn through the 3-per-hour limit with successful signups
    for i in range(3):
        _prep_signup_session(client)
        client.post("/signup", data={
            "email": f"user{i}@example.com",
            "password": "password123",
            "confirm": "password123",
            "captcha": "5",
            "website": "",
        })
    assert User.query.count() == 3

    # 4th signup should be blocked
    _prep_signup_session(client)
    resp = client.post("/signup", data={
        "email": "blocked@example.com",
        "password": "password123",
        "confirm": "password123",
        "captcha": "5",
        "website": "",
    })
    assert resp.status_code == 200
    assert User.query.filter_by(email="blocked@example.com").first() is None


def test_login_with_correct_password(client, db, make_user):
    make_user(email="login@example.com", password="secret123")
    resp = client.post("/login", data={"login": "login@example.com", "password": "secret123"})
    assert resp.status_code == 303


def test_login_rejects_wrong_password(client, db, make_user):
    make_user(email="login@example.com", password="secret123")
    resp = client.post("/login", data={"login": "login@example.com", "password": "wrong"})
    assert resp.status_code == 200  # re-renders form, no redirect


def test_login_rejects_unknown_user(client, db):
    resp = client.post("/login", data={"login": "nobody@example.com", "password": "anything"})
    assert resp.status_code == 200


def test_logout_redirects_to_landing(client, db, make_user, login_as):
    user = make_user()
    login_as(user)
    resp = client.get("/logout")
    assert resp.status_code == 302


def test_login_next_rejects_offsite_redirect(client, db, make_user):
    """#90: ?next= must not redirect off-site after login."""
    make_user(email="redir@example.com", password="secret123")
    for evil in ("https://evil.com", "//evil.com", "/\\evil.com", "javascript:alert(1)"):
        resp = client.post(f"/login?next={evil}",
                           data={"login": "redir@example.com", "password": "secret123"})
        assert resp.status_code == 303
        assert resp.headers["Location"].startswith("/")
        assert "evil.com" not in resp.headers["Location"]
        assert not resp.headers["Location"].startswith("//")
        client.get("/logout")


def test_login_next_allows_relative_path(client, db, make_user):
    make_user(email="redir2@example.com", password="secret123")
    resp = client.post("/login?next=/dashboard",
                       data={"login": "redir2@example.com", "password": "secret123"})
    assert resp.status_code == 303
    assert resp.headers["Location"] == "/dashboard"
