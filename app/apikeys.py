import hashlib
from flask import Blueprint, render_template, redirect, url_for, flash, request
from flask_login import login_required, current_user
from app.models import ApiKey, ApiUsage, Thread, Wallet
from app import db
from sqlalchemy import func
from datetime import datetime, timezone, timedelta

apikeys_bp = Blueprint("apikeys", __name__, url_prefix="/api-keys")

MAX_KEYS = 5


@apikeys_bp.route("/")
@login_required
def index():
    keys = (
        ApiKey.query
        .filter_by(user_id=current_user.id)
        .order_by(ApiKey.created_at.desc())
        .all()
    )

    # Get or create wallet
    wallet = Wallet.query.filter_by(user_id=current_user.id).first()

    # Usage stats per key
    key_stats = {}
    now = datetime.now(timezone.utc)
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    week_start = now - timedelta(days=7)
    month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)

    for key in keys:
        daily = db.session.query(
            func.coalesce(func.sum(ApiUsage.tokens_prompt + ApiUsage.tokens_completion), 0)
        ).filter(ApiUsage.api_key_id == key.id, ApiUsage.created_at >= today_start).scalar() or 0

        weekly = db.session.query(
            func.coalesce(func.sum(ApiUsage.tokens_prompt + ApiUsage.tokens_completion), 0)
        ).filter(ApiUsage.api_key_id == key.id, ApiUsage.created_at >= week_start).scalar() or 0

        monthly = db.session.query(
            func.coalesce(func.sum(ApiUsage.tokens_prompt + ApiUsage.tokens_completion), 0)
        ).filter(ApiUsage.api_key_id == key.id, ApiUsage.created_at >= month_start).scalar() or 0

        key_stats[key.id] = {"daily": daily, "weekly": weekly, "monthly": monthly}

    threads = Thread.query.filter_by(user_id=current_user.id).order_by(Thread.updated_at.desc()).limit(50).all()
    return render_template("apikeys/index.html", keys=keys, key_stats=key_stats, max_keys=MAX_KEYS, threads=threads, wallet=wallet)


@apikeys_bp.route("/create", methods=["POST"])
@login_required
def create():
    # No premium check — any user can create API keys
    existing = ApiKey.query.filter_by(user_id=current_user.id).count()
    if existing >= MAX_KEYS:
        flash(f"Maximum {MAX_KEYS} API keys allowed.", "error")
        return redirect(url_for("apikeys.index"))

    name = request.form.get("name", "Default").strip()[:80] or "Default"

    raw_key, key_hash, prefix = ApiKey.generate_key()
    api_key = ApiKey(
        user_id=current_user.id,
        name=name,
        key_hash=key_hash,
        key_prefix=prefix,
    )
    db.session.add(api_key)
    db.session.commit()

    # Pass the raw key to show once
    flash(f"API key created! Copy it now — it won't be shown again.\n\n{raw_key}", "success")
    return redirect(url_for("apikeys.index"))


@apikeys_bp.route("/<int:key_id>/revoke", methods=["POST"])
@login_required
def revoke(key_id):
    key = ApiKey.query.filter_by(id=key_id, user_id=current_user.id).first_or_404()
    key.active = False
    db.session.commit()
    flash(f"API key '{key.name}' revoked.", "success")
    return redirect(url_for("apikeys.index"))


@apikeys_bp.route("/<int:key_id>/delete", methods=["POST"])
@login_required
def delete(key_id):
    key = ApiKey.query.filter_by(id=key_id, user_id=current_user.id).first_or_404()
    db.session.delete(key)
    db.session.commit()
    flash(f"API key '{key.name}' deleted.", "success")
    return redirect(url_for("apikeys.index"))
