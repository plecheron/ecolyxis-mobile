"""Stripe webhook: signature verification + event handler effects."""
import hmac
import hashlib
import time
import json
import pytest
from app.models import User, Wallet


WEBHOOK_SECRET = "whsec_test_dummy"  # matches conftest


def _stripe_sign(payload_str, secret=WEBHOOK_SECRET, ts=None):
    """Compute a Stripe-Signature header for a JSON payload."""
    ts = ts or int(time.time())
    signed_payload = f"{ts}.{payload_str}"
    sig = hmac.new(secret.encode(), signed_payload.encode(), hashlib.sha256).hexdigest()
    return f"t={ts},v1={sig}"


def _post_event(client, event, secret=WEBHOOK_SECRET):
    """Wrap the event in a complete Stripe envelope and POST with valid signature."""
    full = {
        "id": "evt_test_" + str(int(time.time())),
        "object": "event",
        "api_version": "2024-04-10",
        "created": int(time.time()),
        **event,
    }
    payload = json.dumps(full)
    sig_header = _stripe_sign(payload, secret=secret)
    return client.post(
        "/billing/webhook",
        data=payload,
        headers={"Stripe-Signature": sig_header, "Content-Type": "application/json"},
    )


def test_webhook_rejects_missing_signature(client, db):
    resp = client.post("/billing/webhook", data="{}", headers={"Content-Type": "application/json"})
    assert resp.status_code == 400


def test_webhook_rejects_bad_signature(client, db):
    payload = json.dumps({"type": "checkout.session.completed", "data": {"object": {}}})
    resp = client.post(
        "/billing/webhook",
        data=payload,
        headers={"Stripe-Signature": "t=0,v1=deadbeef", "Content-Type": "application/json"},
    )
    assert resp.status_code == 400


def test_webhook_accepts_valid_signature(client, db, make_user):
    make_user()
    event = {
        "type": "checkout.session.completed",
        "data": {"object": {"metadata": {"user_id": "999"}, "customer": "cus_x", "subscription": None}},
    }
    resp = _post_event(client, event)
    assert resp.status_code == 200


def test_checkout_completed_upgrades_user_to_premium(client, db, make_user):
    """Production path: subscription upgrade via customer_id lookup.

    Note: the metadata.user_id path in billing._handle_checkout_completed
    is gated on isinstance(metadata, dict), which is False for stripe
    SDK's StripeObject — so production relies on the customer_id fallback.
    """
    user = make_user()
    user_id = user.id
    user.stripe_customer_id = "cus_x"  # mirrors what /billing/checkout sets
    db.session.commit()

    event = {
        "type": "checkout.session.completed",
        "data": {"object": {
            "metadata": {"user_id": str(user_id)},
            "customer": "cus_x",
            "subscription": "sub_abc",
        }},
    }
    resp = _post_event(client, event)
    assert resp.status_code == 200
    db.session.expire_all()
    fresh = db.session.get(User, user_id)
    assert fresh.tier == "premium"
    assert fresh.subscription_status == "active"
    assert fresh.stripe_subscription_id == "sub_abc"


def test_subscription_deleted_reverts_to_free(client, db, make_user):
    user = make_user()
    user.tier = "premium"
    user.subscription_status = "active"
    user.stripe_subscription_id = "sub_to_delete"
    db.session.commit()

    event = {
        "type": "customer.subscription.deleted",
        "data": {"object": {"id": "sub_to_delete"}},
    }
    resp = _post_event(client, event)
    assert resp.status_code == 200
    db.session.refresh(user)
    assert user.tier == "free"
    assert user.subscription_status == "canceled"
    assert user.stripe_subscription_id is None


def test_subscription_updated_past_due_downgrades(client, db, make_user):
    user = make_user()
    user.tier = "premium"
    user.subscription_status = "active"
    user.stripe_subscription_id = "sub_abc"
    db.session.commit()

    event = {
        "type": "customer.subscription.updated",
        "data": {"object": {"id": "sub_abc", "status": "past_due", "cancel_at_period_end": False}},
    }
    resp = _post_event(client, event)
    assert resp.status_code == 200
    db.session.refresh(user)
    assert user.tier == "free"
    assert user.subscription_status == "past_due"


def test_credit_topup_credits_wallet(client, db, make_user):
    user = make_user()
    wallet = Wallet(user_id=user.id, balance_pence=0)
    db.session.add(wallet)
    db.session.commit()

    event = {
        "type": "checkout.session.completed",
        "data": {"object": {
            "metadata": {
                "type": "api_credit_topup",
                "user_id": str(user.id),
                "wallet_id": str(wallet.id),
                "credit_amount_pence": "500",
            },
            "payment_intent": "pi_abc",
        }},
    }
    resp = _post_event(client, event)
    assert resp.status_code == 200
    db.session.refresh(wallet)
    assert wallet.balance_pence == 500


def test_payment_failed_marks_past_due(client, db, make_user):
    user = make_user()
    user.tier = "premium"
    user.subscription_status = "active"
    user.stripe_subscription_id = "sub_invoice"
    db.session.commit()

    event = {
        "type": "invoice.payment_failed",
        "data": {"object": {"subscription": "sub_invoice"}},
    }
    resp = _post_event(client, event)
    assert resp.status_code == 200
    db.session.refresh(user)
    assert user.subscription_status == "past_due"


def test_credit_topup_is_idempotent(client, db, make_user):
    """Issue #87: a redelivered checkout.session.completed must not double-credit."""
    from app.models import Transaction

    user = make_user()
    wallet = Wallet(user_id=user.id, balance_pence=0)
    db.session.add(wallet)
    db.session.commit()

    event = {
        "type": "checkout.session.completed",
        "data": {"object": {
            "metadata": {
                "type": "api_credit_topup",
                "user_id": str(user.id),
                "wallet_id": str(wallet.id),
                "credit_amount_pence": "500",
            },
            "payment_intent": "pi_redelivered",
        }},
    }
    assert _post_event(client, event).status_code == 200
    # Stripe retries on timeout/non-2xx — the redelivery must be a no-op 200.
    assert _post_event(client, event).status_code == 200

    db.session.refresh(wallet)
    assert wallet.balance_pence == 500
    txns = Transaction.query.filter_by(stripe_payment_intent_id="pi_redelivered").all()
    assert len(txns) == 1


def test_webhook_fails_closed_without_secret(db):
    """Issue #88: a missing webhook secret must reject events, not trust raw JSON."""
    from app import create_app

    app = create_app(test_config={"TESTING": True, "STRIPE_WEBHOOK_SECRET": ""})
    payload = json.dumps({"type": "checkout.session.completed", "data": {"object": {}}})
    resp = app.test_client().post(
        "/billing/webhook", data=payload, headers={"Content-Type": "application/json"}
    )
    assert resp.status_code == 400


def test_startup_requires_webhook_secret_with_live_key():
    """Issue #88: live Stripe key + no webhook secret must fail at startup."""
    from app import create_app

    with pytest.raises(RuntimeError, match="STRIPE_WEBHOOK_SECRET"):
        create_app(test_config={
            "TESTING": False,
            "STRIPE_SECRET_KEY": "sk_live_dummy",
            "STRIPE_WEBHOOK_SECRET": "",
        })
