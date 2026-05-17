"""Subscription checkout, success/cancel pages, and cancellation."""
from flask import render_template, redirect, url_for, flash, request, current_app
from flask_login import login_required, current_user
import stripe

from app import db
from app.billing import billing_bp


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

    if not current_user.stripe_customer_id:
        customer = stripe.Customer.create(
            email=current_user.email,
            metadata={"user_id": current_user.id, "username": current_user.username},
        )
        current_user.stripe_customer_id = customer.id
        db.session.commit()
    else:
        customer = stripe.Customer.retrieve(current_user.stripe_customer_id)

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
