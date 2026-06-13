"""Wallet route tests — view, top-up validation, auto-creation."""
import pytest
from unittest.mock import patch, MagicMock
from app.models import Wallet, Transaction


def test_wallet_view_creates_wallet_if_missing(app, db, make_user, login_as, client):
    user = make_user()
    login_as(user)
    resp = client.get("/wallet/")
    assert resp.status_code == 200
    wallet = Wallet.query.filter_by(user_id=user.id).first()
    assert wallet is not None
    assert wallet.balance_pence == 0


def test_wallet_view_shows_balance(app, db, make_user, login_as, client):
    user = make_user()
    w = Wallet(user_id=user.id, balance_pence=1500)
    db.session.add(w)
    db.session.commit()
    login_as(user)
    resp = client.get("/wallet/")
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert "15.00" in body or "£15" in body


def test_wallet_requires_auth(app, client):
    resp = client.get("/wallet/", follow_redirects=False)
    assert resp.status_code in (301, 302, 303)


def test_topup_below_minimum(app, db, make_user, login_as, client):
    user = make_user()
    login_as(user)
    resp = client.post("/wallet/topup", data={"amount": "3"}, follow_redirects=True)
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert "Minimum" in body or "minimum" in body


def test_topup_above_maximum(app, db, make_user, login_as, client):
    user = make_user()
    login_as(user)
    resp = client.post("/wallet/topup", data={"amount": "500"}, follow_redirects=True)
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert "Maximum" in body or "maximum" in body


def test_topup_invalid_amount(app, db, make_user, login_as, client):
    user = make_user()
    login_as(user)
    resp = client.post("/wallet/topup", data={"amount": "abc"}, follow_redirects=True)
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert "Invalid" in body


def test_topup_valid_amount_creates_stripe_session(app, db, make_user, login_as, client):
    user = make_user()
    login_as(user)
    mock_session = MagicMock()
    mock_session.url = "https://checkout.stripe.com/test"
    mock_session.id = "cs_test_123"

    with patch("stripe.Customer.create", return_value=MagicMock(id="cus_test123")), \
         patch("stripe.checkout.Session.create", return_value=mock_session):
        resp = client.post("/wallet/topup", data={"amount": "10"}, follow_redirects=False)
        assert resp.status_code in (301, 302, 303)
        assert "stripe.com" in resp.headers.get("Location", "") or "checkout" in resp.headers.get("Location", "")
