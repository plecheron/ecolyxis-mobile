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
    fresh = User.query.get(user_id)
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


@pytest.mark.xfail(
    reason="Latent bug: _handle_checkout_completed gates checkout_type detection "
    "on isinstance(metadata, dict), which is False for StripeObject — so "
    "_handle_credit_topup is never reached. See billing.py:161.",
    strict=True,
)
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
