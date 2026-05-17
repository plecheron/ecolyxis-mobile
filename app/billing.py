from flask import Blueprint, render_template, redirect, url_for, flash, request, current_app
from flask_login import login_required, current_user
from app import db
from app.models import User, Wallet
import stripe

billing_bp = Blueprint("billing", __name__)


def _sd(stripe_obj, key, default=None):
    """Safely get a value from a Stripe object or dict."""
    try:
        return stripe_obj[key]
    except (KeyError, TypeError, IndexError):
        return default


def _get_or_create_wallet(user):
    """Get or create a wallet for the user."""
    w = Wallet.query.filter_by(user_id=user.id).first()
    if not w:
        w = Wallet(user_id=user.id)
        db.session.add(w)
        db.session.commit()
    return w


@billing_bp.route("/billing")
@login_required
def index():
    """Billing & subscription management page."""
    return render_template("billing/index.html")


@billing_bp.route("/billing/checkout")
@login_required
def checkout():
    """Create a Stripe Checkout session for Premium subscription."""
    stripe.api_key = current_app.config["STRIPE_SECRET_KEY"]

    # Create or reuse Stripe customer
    if not current_user.stripe_customer_id:
        customer = stripe.Customer.create(
            email=current_user.email,
            metadata={"user_id": current_user.id, "username": current_user.username},
        )
        current_user.stripe_customer_id = customer.id
        db.session.commit()
    else:
        customer = stripe.Customer.retrieve(current_user.stripe_customer_id)

    # Check if already has active subscription
    if current_user.stripe_subscription_id:
        try:
            sub = stripe.Subscription.retrieve(current_user.stripe_subscription_id)
            if sub.status in ("active", "trialing"):
                flash("You already have an active subscription.", "success")
                return redirect(url_for("billing.index"))
        except stripe.InvalidRequestError:
            pass

    checkout_params = {
        "customer": customer.id,
        "payment_method_types": ["card"],
        "mode": "subscription",
        "line_items": [{
            "price": current_app.config["STRIPE_PRICE_ID"],
            "quantity": 1,
        }],
        "success_url": request.host_url + "billing/success?session_id={CHECKOUT_SESSION_ID}",
        "cancel_url": request.host_url + "billing?canceled=1",
        "metadata": {"user_id": str(current_user.id)},
    }

    # Apply introductory coupon if configured
    coupon_id = current_app.config.get("STRIPE_COUPON_ID")
    if coupon_id:
        checkout_params["discounts"] = [{"coupon": coupon_id}]

    session = stripe.checkout.Session.create(**checkout_params)

    return redirect(session.url, code=303)


@billing_bp.route("/billing/success")
@login_required
def success():
    """Return URL after successful checkout. Webhook handles the actual upgrade."""
    flash("Subscription activated! Welcome to Ecolyxis Premium 🌿✨", "success")
    return redirect(url_for("billing.index"))


@billing_bp.route("/billing/cancel-subscription", methods=["POST"])
@login_required
def cancel_subscription():
    """Cancel the current subscription. Reverts to Free at period end."""
    stripe.api_key = current_app.config["STRIPE_SECRET_KEY"]

    if not current_user.stripe_subscription_id:
        flash("No active subscription to cancel.", "error")
        return redirect(url_for("billing.index"))

    try:
        stripe.Subscription.modify(
            current_user.stripe_subscription_id,
            cancel_at_period_end=True,
        )
        flash("Subscription will be canceled at the end of your billing period.", "success")
    except stripe.InvalidRequestError as e:
        flash("Could not cancel subscription: " + str(e), "error")

    return redirect(url_for("billing.index"))


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

    # Normalize event type
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

    # Check if this is a credit top-up (has type=api_credit_topup in metadata)
    checkout_type = _sd(metadata, "type")
    if checkout_type == "api_credit_topup":
        _handle_credit_topup(session_obj, metadata)
        return

    # Otherwise it's a subscription checkout
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
    wallet_id = _sd(metadata, "wallet_id")
    credit_amount_pence = _sd(metadata, "credit_amount_pence")

    if not user_id or not credit_amount_pence:
        return

    user = User.query.get(int(user_id))
    if not user:
        return

    wallet = _get_or_create_wallet(user)

    # Get Stripe payment intent ID for audit trail
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
