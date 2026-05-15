"""Wallet blueprint — balance, top-up, transaction history."""
from flask import Blueprint, render_template, redirect, url_for, flash, request, current_app
from flask_login import login_required, current_user
from app import db
from app.models import Wallet, Transaction
from sqlalchemy import func
from datetime import datetime, timezone
import stripe

wallet_bp = Blueprint("wallet", __name__, url_prefix="/wallet")


def _get_or_create_wallet(user):
    """Get or create a wallet for the user."""
    w = Wallet.query.filter_by(user_id=user.id).first()
    if not w:
        w = Wallet(user_id=user.id)
        db.session.add(w)
        db.session.commit()
    return w


@wallet_bp.route("/")
@login_required
def index():
    """Show balance, top-up options, and transaction history."""
    wallet = _get_or_create_wallet(current_user)

    # Recent transactions
    transactions = (
        Transaction.query
        .filter_by(wallet_id=wallet.id)
        .order_by(Transaction.created_at.desc())
        .limit(50)
        .all()
    )

    # Total usage this month
    month_start = datetime.now(timezone.utc).replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    monthly_usage = (
        db.session.query(func.coalesce(func.sum(Transaction.amount_pence), 0))
        .filter(Transaction.wallet_id == wallet.id, Transaction.type == "usage", Transaction.created_at >= month_start)
        .scalar()
    )

    return render_template("wallet/index.html", wallet=wallet, transactions=transactions, monthly_usage=abs(monthly_usage))


@wallet_bp.route("/topup", methods=["POST"])
@login_required
def topup():
    """Initiate a Stripe Checkout session for credit top-up."""
    stripe.api_key = current_app.config["STRIPE_SECRET_KEY"]
    wallet = _get_or_create_wallet(current_user)

    amount_str = request.form.get("amount", "5")
    try:
        amount = int(amount_str)
        if amount < 5:
            flash("Minimum top-up is £5.", "error")
            return redirect(url_for("wallet.index"))
        if amount > 100:
            flash("Maximum top-up is £100.", "error")
            return redirect(url_for("wallet.index"))
    except ValueError:
        flash("Invalid amount.", "error")
        return redirect(url_for("wallet.index"))

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

    # Stripe requires amount in pence
    session = stripe.checkout.Session.create(
        customer=customer.id,
        payment_method_types=["card"],
        mode="payment",
        line_items=[{
            "price_data": {
                "currency": "gbp",
                "unit_amount": amount * 100,  # amount in pence
                "product_data": {
                    "name": f"Ecolyxis API Credits — £{amount}",
                    "description": f"£{amount} prepaid credits at £0.20/mtok",
                },
            },
            "quantity": 1,
        }],
        success_url=request.host_url + "wallet?topup=success",
        cancel_url=request.host_url + "wallet?topup=cancelled",
        metadata={
            "user_id": str(current_user.id),
            "wallet_id": str(wallet.id),
            "credit_amount_pence": str(amount * 100),
            "type": "api_credit_topup",
        },
    )

    return redirect(session.url, code=303)
