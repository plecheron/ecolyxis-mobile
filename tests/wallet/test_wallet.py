"""Wallet/Transaction money handling — integer pence, audit trail, insufficient balance."""
import pytest
from app.models import Wallet, Transaction


@pytest.fixture
def wallet(db, make_user):
    user = make_user()
    w = Wallet(user_id=user.id, balance_pence=0)
    db.session.add(w)
    db.session.commit()
    return w


def test_credit_increases_balance_and_logs_transaction(db, wallet):
    txn = wallet.credit(500, "test topup", stripe_payment_intent_id="pi_123")
    db.session.commit()
    assert wallet.balance_pence == 500
    assert wallet.balance == 5.0
    assert txn.type == "topup"
    assert txn.amount_pence == 500
    assert txn.stripe_payment_intent_id == "pi_123"


def test_debit_reduces_balance_and_logs_transaction(db, wallet):
    wallet.credit(1000, "topup")
    db.session.commit()
    txn = wallet.debit(300, "usage", api_key_id=None)
    db.session.commit()
    assert wallet.balance_pence == 700
    assert txn.type == "usage"
    assert txn.amount_pence == -300


def test_debit_insufficient_raises(db, wallet):
    wallet.credit(100, "topup")
    db.session.commit()
    with pytest.raises(ValueError, match="Insufficient"):
        wallet.debit(200, "usage")


def test_can_afford(db, wallet):
    wallet.credit(500, "topup")
    db.session.commit()
    assert wallet.can_afford(500)
    assert wallet.can_afford(499)
    assert not wallet.can_afford(501)


def test_multiple_transactions_audit_trail(db, wallet):
    wallet.credit(1000, "topup 1")
    wallet.credit(500, "topup 2")
    wallet.debit(300, "usage 1")
    db.session.commit()
    txns = Transaction.query.filter_by(wallet_id=wallet.id).order_by(Transaction.id).all()
    assert len(txns) == 3
    assert [t.amount_pence for t in txns] == [1000, 500, -300]
    assert wallet.balance_pence == 1200
