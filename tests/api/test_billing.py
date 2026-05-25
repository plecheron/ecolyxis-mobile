"""Tests for billing helpers in app/api/__init__.py."""
import math
import time
from datetime import datetime, timezone
from unittest.mock import patch

from app.models import ApiKey, ApiUsage, User, Wallet, Transaction


class TestTokensToPence:
    """_tokens_to_pence: convert token count to pence at £2.78/MTok."""

    def test_zero_tokens(self):
        from app.api import _tokens_to_pence
        assert _tokens_to_pence(0) == 0

    def test_one_million_tokens(self):
        from app.api import _tokens_to_pence, PRICE_PER_MTOK
        assert _tokens_to_pence(1_000_000) == PRICE_PER_MTOK  # 278 pence = £2.78

    def test_small_count_ceil(self):
        from app.api import _tokens_to_pence, PRICE_PER_MTOK
        # Even 1 token should cost something (ceil)
        result = _tokens_to_pence(1)
        assert result == math.ceil(1 * PRICE_PER_MTOK / 1_000_000)

    def test_exact_division(self):
        from app.api import _tokens_to_pence, PRICE_PER_MTOK
        # 500k tokens = 139 pence
        result = _tokens_to_pence(500_000)
        assert result == math.ceil(500_000 * PRICE_PER_MTOK / 1_000_000)

    def test_large_count(self):
        from app.api import _tokens_to_pence, PRICE_PER_MTOK
        tokens = 10_000_000
        expected = math.ceil(tokens * PRICE_PER_MTOK / 1_000_000)
        assert _tokens_to_pence(tokens) == expected


class TestCheckRateLimit:
    """_check_rate_limit: token bucket rate limiter."""

    def test_allows_first_request(self, app, db):
        with app.app_context():
            from app.api import _check_rate_limit, RATE_REQUESTS_PER_MIN
            allowed, remaining, retry_after = _check_rate_limit("test_key_1", RATE_REQUESTS_PER_MIN)
            assert allowed is True
            assert remaining >= 0
            assert retry_after == 0

    def test_exhausts_bucket(self, app, db):
        with app.app_context():
            from app.api import _check_rate_limit, RATE_REQUESTS_PER_MIN
            key = "test_exhaust_key"
            limit = RATE_REQUESTS_PER_MIN
            # Drain the bucket
            for _ in range(limit):
                allowed, _, _ = _check_rate_limit(key, limit)
                assert allowed is True
            # Next request should be denied
            allowed, remaining, retry_after = _check_rate_limit(key, limit)
            assert allowed is False
            assert remaining == 0
            assert retry_after > 0

    def test_bucket_refills_over_time(self, app, db):
        with app.app_context():
            from app.api import _check_rate_limit, RATE_REQUESTS_PER_MIN
            key = "test_refill_key"
            limit = 5
            # Exhaust
            for _ in range(limit):
                _check_rate_limit(key, limit)
            allowed, _, _ = _check_rate_limit(key, limit)
            assert allowed is False
            # Simulate time passing (monkey-patch time.time in the model)
            with patch("app.models.time.time", return_value=time.time() + 60):
                allowed, _, _ = _check_rate_limit(key, limit)
                assert allowed is True

    def test_different_keys_independent(self, app, db):
        with app.app_context():
            from app.api import _check_rate_limit, RATE_REQUESTS_PER_MIN
            limit = 3
            # Exhaust key A
            for _ in range(limit):
                _check_rate_limit("key_A", limit)
            allowed_a, _, _ = _check_rate_limit("key_A", limit)
            assert allowed_a is False
            # Key B should still work
            allowed_b, _, _ = _check_rate_limit("key_B", limit)
            assert allowed_b is True


class TestGetDailyUsage:
    """_get_daily_usage: sum tokens used today for an API key."""

    def test_no_usage(self, app, db):
        with app.app_context():
            from app.api import _get_daily_usage
            result = _get_daily_usage(999)
            assert result == 0

    def test_with_usage(self, app, db):
        with app.app_context():
            from app.api import _get_daily_usage
            # Create user + api key + usage records
            user = User(username="testuser", email="test@test.com")
            user.set_password("password123")
            db.session.add(user)
            db.session.flush()

            api_key = ApiKey(
                user_id=user.id,
                name="test",
                key_hash="abc123",
                key_prefix="abcd",
            )
            db.session.add(api_key)
            db.session.flush()

            usage = ApiUsage(
                api_key_id=api_key.id,
                endpoint="/v1/chat/completions",
                model="test-model",
                tokens_prompt=100,
                tokens_completion=50,
            )
            db.session.add(usage)
            db.session.commit()

            result = _get_daily_usage(api_key.id)
            assert result == 150  # 100 + 50


class TestGetOrCreateWallet:
    """_get_or_create_wallet: creates wallet if missing, returns existing."""

    def test_creates_wallet(self, app, db):
        with app.app_context():
            from app.api import _get_or_create_wallet
            user = User(username="walletuser", email="wallet@test.com")
            user.set_password("password123")
            db.session.add(user)
            db.session.commit()

            wallet = _get_or_create_wallet(user.id)
            assert wallet is not None
            assert wallet.user_id == user.id
            assert wallet.balance_pence == 0

    def test_returns_existing(self, app, db):
        with app.app_context():
            from app.api import _get_or_create_wallet
            user = User(username="walletuser2", email="wallet2@test.com")
            user.set_password("password123")
            db.session.add(user)
            db.session.commit()

            w1 = _get_or_create_wallet(user.id)
            w1.balance_pence = 500
            db.session.commit()

            w2 = _get_or_create_wallet(user.id)
            assert w2.id == w1.id
            assert w2.balance_pence == 500


class TestLogUsageAndDebit:
    """_log_usage_and_debit: logs usage + debits wallet."""

    def test_logs_usage_and_debits(self, app, db):
        with app.app_context():
            from app.api import _log_usage_and_debit, _tokens_to_pence
            user = User(username="debituser", email="debit@test.com")
            user.set_password("password123")
            db.session.add(user)
            db.session.commit()

            wallet = Wallet(user_id=user.id, balance_pence=10000)  # £100
            db.session.add(wallet)
            db.session.commit()

            api_key = ApiKey(
                user_id=user.id,
                name="test",
                key_hash="debit123",
                key_prefix="db12",
            )
            db.session.add(api_key)
            db.session.commit()

            prompt_tokens = 500
            completion_tokens = 200
            _log_usage_and_debit(
                app, api_key.id, wallet.id,
                "/v1/chat/completions", "test-model",
                prompt_tokens, completion_tokens,
            )

            # Check usage logged
            usage = ApiUsage.query.filter_by(api_key_id=api_key.id).first()
            assert usage is not None
            assert usage.tokens_prompt == 500
            assert usage.tokens_completion == 200

            # Check wallet debited
            expected_cost = _tokens_to_pence(700)
            db.session.refresh(wallet)
            assert wallet.balance_pence == 10000 - expected_cost

            # Check transaction created
            txn = Transaction.query.filter_by(wallet_id=wallet.id).first()
            assert txn is not None
            assert txn.type == "usage"
            assert txn.amount_pence == -expected_cost

    def test_insufficient_balance_no_crash(self, app, db):
        with app.app_context():
            from app.api import _log_usage_and_debit
            user = User(username="pooruser", email="poor@test.com")
            user.set_password("password123")
            db.session.add(user)
            db.session.commit()

            wallet = Wallet(user_id=user.id, balance_pence=0)  # £0
            db.session.add(wallet)
            db.session.commit()

            api_key = ApiKey(
                user_id=user.id,
                name="test",
                key_hash="poor123",
                key_prefix="po12",
            )
            db.session.add(api_key)
            db.session.commit()

            # Should NOT crash, just log warning
            _log_usage_and_debit(
                app, api_key.id, wallet.id,
                "/v1/chat/completions", "test-model",
                1000, 1000,
            )

            # Wallet should still be 0 (no negative)
            db.session.refresh(wallet)
            assert wallet.balance_pence == 0

            # But usage should still be logged
            usage = ApiUsage.query.filter_by(api_key_id=api_key.id).first()
            assert usage is not None
