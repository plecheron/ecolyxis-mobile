"""Fixtures for API tests — creates user, wallet, and API key."""
import hashlib
import secrets
import pytest
from app.models import ApiKey, Wallet


@pytest.fixture
def make_api_key(app, db, make_user):
    """Factory: create a user with wallet + active API key. Returns (raw_key, api_key_obj, wallet, user)."""
    def _make(balance_pence=100_000, **user_kw):
        user = make_user(**user_kw)
        wallet = Wallet(user_id=user.id, balance_pence=balance_pence)
        db.session.add(wallet)
        db.session.commit()

        # Generate a realistic key starting with ecolyx_
        raw_key = "ecolyx_" + secrets.token_urlsafe(32)
        key_hash = hashlib.sha256(raw_key.encode()).hexdigest()
        api_key = ApiKey(
            user_id=user.id,
            name="test-key",
            key_hash=key_hash,
            key_prefix=raw_key[:8],
            active=True,
        )
        db.session.add(api_key)
        db.session.commit()
        return raw_key, api_key, wallet, user
    return _make
