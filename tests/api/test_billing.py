"""Tests for billing helpers in app.api.__init__."""
import math
import pytest
from app.api import _tokens_to_pence, PRICE_PER_MTOK
from app.models import ApiKey, Wallet, Transaction, ApiUsage


class TestTokensToPence:
    """Unit tests for the _tokens_to_pence helper."""

    def test_zero_tokens(self):
        assert _tokens_to_pence(0) == 0

    def test_one_token(self):
        expected = math.ceil(1 * PRICE_PER_MTOK / 1_000_000)
        assert _tokens_to_pence(1) == expected

    def test_one_million_tokens(self):
        assert _tokens_to_pence(1_000_000) == PRICE_PER_MTOK

    def test_exactly_pence_boundary(self):
        # 1000 tokens at 278 pence/MTok = 0.278 pence -> ceil = 1
        assert _tokens_to_pence(1000) == 1

    def test_large_token_count(self):
        tokens = 500_000
        expected = math.ceil(tokens * PRICE_PER_MTOK / 1_000_000)
        assert _tokens_to_pence(tokens) == expected


class TestLogUsageAndDebit:
    """Tests for _log_usage_and_debit."""

    def test_debit_reduces_balance(self, app, db, make_api_key):
        raw_key, api_key, wallet, user = make_api_key(balance_pence=100_000)
        prompt = 1000
        completion = 500

        from app.api import _log_usage_and_debit
        _log_usage_and_debit(app, api_key.id, wallet.id, "/v1/chat/completions", "test", prompt, completion)

        with app.app_context():
            w = db.session.get(Wallet, wallet.id)
            expected_cost = math.ceil((prompt + completion) * PRICE_PER_MTOK / 1_000_000)
            assert w.balance_pence == 100_000 - expected_cost

    def test_creates_usage_record(self, app, db, make_api_key):
        raw_key, api_key, wallet, user = make_api_key()

        from app.api import _log_usage_and_debit
        _log_usage_and_debit(app, api_key.id, wallet.id, "/v1/chat/completions", "test-model", 100, 200)

        with app.app_context():
            usage = ApiUsage.query.filter_by(api_key_id=api_key.id).one()
            assert usage.tokens_prompt == 100
            assert usage.tokens_completion == 200
            assert usage.model == "test-model"
            assert usage.endpoint == "/v1/chat/completions"

    def test_creates_transaction(self, app, db, make_api_key):
        raw_key, api_key, wallet, user = make_api_key(balance_pence=50_000)

        from app.api import _log_usage_and_debit
        _log_usage_and_debit(app, api_key.id, wallet.id, "/v1/chat/completions", "test", 5000, 5000)

        with app.app_context():
            txn = Transaction.query.filter_by(wallet_id=wallet.id).one()
            assert txn.type == "usage"
            assert txn.amount_pence < 0
            expected_cost = math.ceil(10_000 * PRICE_PER_MTOK / 1_000_000)
            assert abs(txn.amount_pence) == expected_cost
            assert txn.api_key_id == api_key.id

    def test_insufficient_balance_rejects_debit(self, app, db, make_api_key):
        """Bug #44: When balance is too low, wallet should still be debited (go <= 0)."""
        raw_key, api_key, wallet, user = make_api_key(balance_pence=1)

        from app.api import _log_usage_and_debit
        _log_usage_and_debit(app, api_key.id, wallet.id, "/v1/chat/completions", "test", 1_000_000, 1_000_000)

        with app.app_context():
            w = db.session.get(Wallet, wallet.id)
            # After fixing #44: wallet should be debited (balance <= 0)
            assert w.balance_pence <= 0

    def test_zero_tokens_no_debit(self, app, db, make_api_key):
        raw_key, api_key, wallet, user = make_api_key(balance_pence=1000)

        from app.api import _log_usage_and_debit
        _log_usage_and_debit(app, api_key.id, wallet.id, "/v1/chat/completions", "test", 0, 0)

        with app.app_context():
            w = db.session.get(Wallet, wallet.id)
            assert w.balance_pence == 1000
            # Still creates usage record
            assert ApiUsage.query.filter_by(api_key_id=api_key.id).count() == 1
