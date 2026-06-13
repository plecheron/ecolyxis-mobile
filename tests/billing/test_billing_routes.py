"""Billing routes tests: index, checkout, success, cancel subscription."""
from unittest.mock import patch, MagicMock
import stripe


# ─── index ───

def test_billing_requires_login(client):
    resp = client.get("/billing", follow_redirects=False)
    assert resp.status_code in (301, 302)


def test_billing_index(app, db, make_user, login_as, client):
    user = make_user()
    login_as(user)
    resp = client.get("/billing")
    assert resp.status_code == 200


# ─── checkout ───

def test_checkout_requires_login(client):
    resp = client.get("/billing/checkout", follow_redirects=False)
    assert resp.status_code in (301, 302)


def test_checkout_creates_new_customer(app, db, make_user, login_as, client):
    user = make_user()
    login_as(user)
    mock_customer = MagicMock()
    mock_customer.id = "cus_new123"
    mock_session = MagicMock()
    mock_session.url = "https://checkout.stripe.com/session123"

    with patch("app.billing.routes.stripe.Customer.create", return_value=mock_customer) as mock_create, \
         patch("app.billing.routes.stripe.checkout.Session.create", return_value=mock_session):
        resp = client.get("/billing/checkout", follow_redirects=False)
        assert resp.status_code == 303
        assert resp.location == "https://checkout.stripe.com/session123"
        mock_create.assert_called_once()


def test_checkout_reuses_existing_customer(app, db, make_user, login_as, client):
    user = make_user()
    user.stripe_customer_id = "cus_existing"
    db.session.commit()
    login_as(user)
    mock_customer = MagicMock()
    mock_customer.id = "cus_existing"
    mock_session = MagicMock()
    mock_session.url = "https://checkout.stripe.com/sess"

    with patch("app.billing.routes.stripe.Customer.retrieve", return_value=mock_customer) as mock_retrieve, \
         patch("app.billing.routes.stripe.Customer.create") as mock_create, \
         patch("app.billing.routes.stripe.checkout.Session.create", return_value=mock_session):
        resp = client.get("/billing/checkout", follow_redirects=False)
        assert resp.status_code == 303
        mock_retrieve.assert_called_once_with("cus_existing")
        mock_create.assert_not_called()


def test_checkout_already_subscribed(app, db, make_user, login_as, client):
    user = make_user()
    user.stripe_customer_id = "cus_123"
    user.stripe_subscription_id = "sub_123"
    db.session.commit()
    login_as(user)

    mock_sub = MagicMock()
    mock_sub.status = "active"

    with patch("app.billing.routes.stripe.Customer.retrieve", return_value=MagicMock(id="cus_123")), \
         patch("app.billing.routes.stripe.Subscription.retrieve", return_value=mock_sub):
        resp = client.get("/billing/checkout", follow_redirects=False)
        assert resp.status_code in (301, 302)


def test_checkout_with_coupon(app, db, make_user, login_as, client):
    user = make_user()
    login_as(user)
    mock_customer = MagicMock()
    mock_customer.id = "cus_1"
    mock_session = MagicMock()
    mock_session.url = "https://checkout.stripe.com/sess"

    with patch("app.billing.routes.stripe.Customer.create", return_value=mock_customer), \
         patch("app.billing.routes.stripe.checkout.Session.create", return_value=mock_session) as mock_sc, \
         patch.dict(app.config, {"STRIPE_COUPON_ID": "coupon50"}):
        client.get("/billing/checkout", follow_redirects=False)
        call_kwargs = mock_sc.call_args[1]
        assert "discounts" in call_kwargs
        assert call_kwargs["discounts"] == [{"coupon": "coupon50"}]


# ─── success ───

def test_success_redirect(app, db, make_user, login_as, client):
    user = make_user()
    login_as(user)
    resp = client.get("/billing/success", follow_redirects=False)
    assert resp.status_code in (301, 302)


def test_success_requires_login(client):
    resp = client.get("/billing/success", follow_redirects=False)
    assert resp.status_code in (301, 302)


# ─── cancel_subscription ───

def test_cancel_requires_login(client):
    resp = client.post("/billing/cancel-subscription", follow_redirects=False)
    assert resp.status_code in (301, 302)


def test_cancel_no_subscription(app, db, make_user, login_as, client):
    user = make_user()
    login_as(user)
    resp = client.post("/billing/cancel-subscription", follow_redirects=False)
    assert resp.status_code in (301, 302)


def test_cancel_subscription_success(app, db, make_user, login_as, client):
    user = make_user()
    user.stripe_subscription_id = "sub_abc"
    db.session.commit()
    login_as(user)
    mock_sub = MagicMock()
    with patch("app.billing.routes.stripe.Subscription.modify", return_value=mock_sub):
        resp = client.post("/billing/cancel-subscription", follow_redirects=False)
        assert resp.status_code in (301, 302)


def test_cancel_subscription_stripe_error(app, db, make_user, login_as, client):
    user = make_user()
    user.stripe_subscription_id = "sub_bad"
    db.session.commit()
    login_as(user)
    with patch("app.billing.routes.stripe.Subscription.modify",
               side_effect=stripe.InvalidRequestError("No such subscription", "sub_bad")):
        resp = client.post("/billing/cancel-subscription", follow_redirects=False)
        assert resp.status_code in (301, 302)
