"""Stripe webhook receiver + event handlers.

Verifies signatures, then dispatches by event type to the per-event
handlers below. Each handler is idempotent — same event twice produces
the same end state.
"""
from flask import request, current_app
import stripe

from app import db
from app.models import User
from app.billing import billing_bp, _sd, _get_or_create_wallet


@billing_bp.route("/billing/webhook", methods=["POST"])
def webhook():
    """Handle Stripe webhook events."""
    stripe.api_key = current_app.config["STRIPE_SECRET_KEY"]
    webhook_secret = current_app.config["STRIPE_WEBHOOK_SECRET"]

    payload = request.data
    sig_header = request.headers.get("Stripe-Signature", "")

    try:
        if webhook_secret:
            event = stripe.Webhook.construct_event(payload, sig_header, webhook_secret)
        else:
            event = request.json
    except (stripe.SignatureVerificationError, ValueError):
        return {"error": "Invalid signature"}, 400

    if isinstance(event, dict):
        event_type = event.get("type", "")
        obj = event.get("data", {}).get("object", {})
    else:
        event_type = event.type
        obj = event.data.object

    if event_type == "checkout.session.completed":
        _handle_checkout_completed(obj)
    elif event_type == "customer.subscription.updated":
        _handle_subscription_updated(obj)
    elif event_type == "customer.subscription.deleted":
        _handle_subscription_deleted(obj)
    elif event_type == "invoice.payment_failed":
        _handle_payment_failed(obj)

    return {"status": "ok"}


def _handle_checkout_completed(session_obj):
    """Handle checkout completion — either subscription or credit top-up."""
    metadata = _sd(session_obj, "metadata") or {}
    user_id = _sd(metadata, "user_id")

    checkout_type = _sd(metadata, "type")
    if checkout_type == "api_credit_topup":
        _handle_credit_topup(session_obj, metadata)
        return

    if not user_id:
        customer_id = _sd(session_obj, "customer")
        user = User.query.filter_by(stripe_customer_id=customer_id).first()
    else:
        user = User.query.get(int(user_id))

    if not user:
        return

    subscription_id = _sd(session_obj, "subscription")
    if subscription_id:
        user.stripe_subscription_id = subscription_id

    user.tier = "premium"
    user.subscription_status = "active"
    user.cancel_at_period_end = False
    db.session.commit()


def _handle_credit_topup(session_obj, metadata):
    """Credit wallet after successful Stripe payment for API credits."""
    user_id = _sd(metadata, "user_id")
    credit_amount_pence = _sd(metadata, "credit_amount_pence")

    if not user_id or not credit_amount_pence:
        return

    user = User.query.get(int(user_id))
    if not user:
        return

    wallet = _get_or_create_wallet(user)
    payment_intent_id = _sd(session_obj, "payment_intent")

    amount_pence = int(credit_amount_pence)
    wallet.credit(
        pence=amount_pence,
        description=f"Credit top-up: £{amount_pence / 100:.0f} ({amount_pence / 0.02:,.0f} mtok)",
        stripe_payment_intent_id=payment_intent_id,
    )
    db.session.commit()


def _handle_subscription_updated(subscription_obj):
    """Handle subscription status changes."""
    subscription_id = _sd(subscription_obj, "id")
    status = _sd(subscription_obj, "status")
    cancel_at_end = _sd(subscription_obj, "cancel_at_period_end") or False

    user = User.query.filter_by(stripe_subscription_id=subscription_id).first()
    if not user:
        return

    user.subscription_status = status
    user.cancel_at_period_end = bool(cancel_at_end)

    if status in ("active", "trialing"):
        user.tier = "premium"
    elif status in ("past_due", "unpaid", "canceled"):
        user.tier = "free"

    db.session.commit()


def _handle_subscription_deleted(subscription_obj):
    """Revert to Free when subscription is fully deleted."""
    subscription_id = _sd(subscription_obj, "id")
    user = User.query.filter_by(stripe_subscription_id=subscription_id).first()
    if not user:
        return

    user.tier = "free"
    user.subscription_status = "canceled"
    user.stripe_subscription_id = None
    user.cancel_at_period_end = False
    db.session.commit()


def _handle_payment_failed(invoice_obj):
    """Handle failed payment — don't immediately revert, let Stripe retry."""
    subscription_id = _sd(invoice_obj, "subscription")
    if not subscription_id:
        return

    user = User.query.filter_by(stripe_subscription_id=subscription_id).first()
    if not user:
        return

    user.subscription_status = "past_due"
    db.session.commit()
