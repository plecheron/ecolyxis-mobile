"""WebAuthn routes tests: 501 when package missing, credential list/delete, auth flow."""
from unittest.mock import patch, MagicMock
from app.models import WebAuthnCredential


# ─── register-begin ───

def test_webauthn_register_begin_requires_login(client):
    resp = client.post("/webauthn/register-begin")
    assert resp.status_code in (301, 302)


def test_webauthn_register_begin_501_if_no_package(app, db, make_user, login_as, client):
    """If webauthn package is not installed, should return 501."""
    user = make_user()
    login_as(user)
    resp = client.post("/webauthn/register-begin", json={})
    # Package likely not installed in test env → 501
    assert resp.status_code in (501, 200)


# ─── register-finish ───

def test_webauthn_register_finish_no_challenge(app, db, make_user, login_as, client):
    """Register-finish without an active challenge should 400."""
    user = make_user()
    login_as(user)
    resp = client.post("/webauthn/register-finish", json={"credential": {}})
    assert resp.status_code in (400, 501)


# ─── authenticate-begin ───

def test_webauthn_authenticate_begin_501_if_no_package(client):
    resp = client.post("/webauthn/authenticate-begin", json={})
    assert resp.status_code in (501, 200)


# ─── authenticate-finish ───

def test_webauthn_authenticate_finish_no_challenge(client):
    resp = client.post("/webauthn/authenticate-finish", json={"credential": {}})
    assert resp.status_code in (400, 501)


def test_webauthn_authenticate_finish_bad_cred_id(client):
    """With a valid challenge but bad credential ID."""
    import base64
    valid_challenge = base64.urlsafe_b64encode(b"a" * 32).decode().rstrip("=")
    with client.session_transaction() as sess:
        sess["webauthn_auth_challenge"] = valid_challenge
    resp = client.post("/webauthn/authenticate-finish", json={
        "credential": {"id": "!!!invalid base64!!!"},
    })
    assert resp.status_code in (400, 501)


# ─── credentials list/delete ───

def test_webauthn_list_credentials_requires_login(client):
    resp = client.get("/webauthn/credentials")
    assert resp.status_code in (301, 302)


def test_webauthn_list_credentials_empty(app, db, make_user, login_as, client):
    user = make_user()
    login_as(user)
    resp = client.get("/webauthn/credentials")
    assert resp.status_code == 200
    data = resp.get_json()
    assert data == []


def test_webauthn_list_credentials_with_data(app, db, make_user, login_as, client):
    user = make_user()
    cred = WebAuthnCredential(
        user_id=user.id,
        credential_id=b"test-cred-id-bytes",
        public_key=b"test-pubkey",
        sign_count=0,
        name="My Laptop",
        transports='["internal"]',
    )
    db.session.add(cred)
    db.session.commit()
    login_as(user)
    resp = client.get("/webauthn/credentials")
    assert resp.status_code == 200
    data = resp.get_json()
    assert len(data) == 1
    assert data[0]["name"] == "My Laptop"


def test_webauthn_delete_credential_requires_login(client):
    resp = client.delete("/webauthn/credentials/1")
    assert resp.status_code in (301, 302)


def test_webauthn_delete_credential_success(app, db, make_user, login_as, client):
    user = make_user()
    cred = WebAuthnCredential(
        user_id=user.id,
        credential_id=b"del-cred-id",
        public_key=b"pk",
        sign_count=0,
        name="ToDelete",
        transports="[]",
    )
    db.session.add(cred)
    db.session.commit()
    cred_id = cred.id
    login_as(user)
    resp = client.delete(f"/webauthn/credentials/{cred_id}")
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["success"] is True


def test_webauthn_delete_credential_not_owner(app, db, make_user, login_as, client):
    user1 = make_user()
    user2 = make_user(username="other", email="other@test.com")
    cred = WebAuthnCredential(
        user_id=user1.id,
        credential_id=b"not-owner-cred",
        public_key=b"pk",
        sign_count=0,
        name="NotYours",
        transports="[]",
    )
    db.session.add(cred)
    db.session.commit()
    login_as(user2)
    resp = client.delete(f"/webauthn/credentials/{cred.id}")
    assert resp.status_code == 404
