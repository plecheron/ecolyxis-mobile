"""API key generation, hashing, and management endpoints."""
import hashlib
from app.models import ApiKey


def test_generate_key_format():
    raw, hashed, prefix = ApiKey.generate_key()
    assert raw.startswith("ecolyx_")
    assert len(raw) > 20
    assert hashed == hashlib.sha256(raw.encode()).hexdigest()
    assert len(hashed) == 64
    assert prefix == raw[-4:]


def test_hash_token_matches_generated():
    raw, hashed, _ = ApiKey.generate_key()
    assert ApiKey.hash_token(raw) == hashed


def test_create_key_via_endpoint(client, db, make_user, login_as):
    user = make_user()
    login_as(user)
    resp = client.post("/api-keys/create", data={"name": "test-key"})
    assert resp.status_code == 302
    keys = ApiKey.query.filter_by(user_id=user.id).all()
    assert len(keys) == 1
    assert keys[0].name == "test-key"
    assert keys[0].active is True


def test_max_keys_enforced(client, db, make_user, login_as):
    user = make_user()
    login_as(user)
    for i in range(ApiKey.MAX_KEYS_PER_USER):
        raw, h, p = ApiKey.generate_key()
        db.session.add(ApiKey(user_id=user.id, name=f"k{i}", key_hash=h, key_prefix=p))
    db.session.commit()

    resp = client.post("/api-keys/create", data={"name": "one-too-many"})
    assert resp.status_code == 302
    assert ApiKey.query.filter_by(user_id=user.id).count() == ApiKey.MAX_KEYS_PER_USER


def test_revoke_deactivates_key(client, db, make_user, login_as):
    user = make_user()
    login_as(user)
    raw, h, p = ApiKey.generate_key()
    key = ApiKey(user_id=user.id, name="to-revoke", key_hash=h, key_prefix=p)
    db.session.add(key)
    db.session.commit()

    resp = client.post(f"/api-keys/{key.id}/revoke")
    assert resp.status_code == 302
    db.session.refresh(key)
    assert key.active is False


def test_cannot_revoke_other_users_key(client, db, make_user, login_as):
    owner = make_user(email="owner@example.com")
    attacker = make_user(email="attacker@example.com")
    raw, h, p = ApiKey.generate_key()
    key = ApiKey(user_id=owner.id, name="owners-key", key_hash=h, key_prefix=p)
    db.session.add(key)
    db.session.commit()

    login_as(attacker)
    resp = client.post(f"/api-keys/{key.id}/revoke")
    assert resp.status_code == 404
    db.session.refresh(key)
    assert key.active is True
