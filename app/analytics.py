"""Usage analytics dashboard: token usage, costs, activity over time."""
from datetime import datetime, timezone, timedelta
from flask import Blueprint, render_template, jsonify
from flask_login import login_required, current_user
from app import db
from app.models import Thread, Message, GenerationJob, ApiKey, ApiUsage, Wallet, Transaction

analytics_bp = Blueprint("analytics", __name__)


def _date_range(days=30):
    now = datetime.now(timezone.utc)
    start = now - timedelta(days=days)
    return start, now


@analytics_bp.route("/analytics")
@login_required
def index():
    """Analytics dashboard page."""
    start, now = _date_range(30)

    # Token usage stats
    total_messages = db.session.query(db.func.count(Message.id)).join(Thread).filter(
        Thread.user_id == current_user.id
    ).scalar() or 0

    total_tokens = db.session.query(db.func.coalesce(db.func.sum(Message.tokens_used), 0)).join(
        Thread
    ).filter(Thread.user_id == current_user.id).scalar() or 0

    # Token usage by day (last 30 days)
    daily_data = []
    for i in range(30, 0, -1):
        day_start = now - timedelta(days=i)
        day_end = now - timedelta(days=i - 1)
        day_tokens = db.session.query(
            db.func.coalesce(db.func.sum(Message.tokens_used), 0)
        ).join(Thread).filter(
            Thread.user_id == current_user.id,
            Message.created_at >= day_start,
            Message.created_at < day_end,
        ).scalar() or 0
        daily_data.append({
            "date": day_start.strftime("%Y-%m-%d"),
            "tokens": day_tokens,
        })

    # Job stats
    total_jobs = db.session.query(db.func.count(GenerationJob.id)).filter(
        GenerationJob.user_id == current_user.id
    ).scalar() or 0

    job_by_kind = db.session.query(
        GenerationJob.kind, db.func.count(GenerationJob.id)
    ).filter(
        GenerationJob.user_id == current_user.id
    ).group_by(GenerationJob.kind).all()

    # API usage (if any)
    api_usage = []
    api_keys = ApiKey.query.filter_by(user_id=current_user.id).all()
    if api_keys:
        api_usage = db.session.query(
            ApiUsage.endpoint, db.func.count(ApiUsage.id),
            db.func.coalesce(db.func.sum(ApiUsage.tokens_prompt + ApiUsage.tokens_completion), 0)
        ).join(ApiKey).filter(
            ApiKey.user_id == current_user.id,
            ApiUsage.created_at >= start,
        ).group_by(ApiUsage.endpoint).all()

    # Wallet balance
    wallet = Wallet.query.filter_by(user_id=current_user.id).first()
    balance_pence = wallet.balance_pence if wallet else 0

    # Thread stats
    total_threads = db.session.query(db.func.count(Thread.id)).filter(
        Thread.user_id == current_user.id
    ).scalar() or 0

    return render_template(
        "analytics.html",
        total_messages=total_messages,
        total_tokens=total_tokens,
        daily_data=daily_data,
        total_jobs=total_jobs,
        job_by_kind=dict(job_by_kind),
        api_usage=api_usage,
        balance_pence=balance_pence,
        total_threads=total_threads,
    )


@analytics_bp.route("/analytics/api/usage")
@login_required
def api_usage_data():
    """JSON API for usage charts (last 30 days, grouped by day)."""
    start, now = _date_range(30)

    daily = []
    for i in range(30, 0, -1):
        day_start = now - timedelta(days=i)
        day_end = now - timedelta(days=i - 1)
        day_tokens = db.session.query(
            db.func.coalesce(db.func.sum(Message.tokens_used), 0)
        ).join(Thread).filter(
            Thread.user_id == current_user.id,
            Message.created_at >= day_start,
            Message.created_at < day_end,
        ).scalar() or 0
        day_messages = db.session.query(
            db.func.count(Message.id)
        ).join(Thread).filter(
            Thread.user_id == current_user.id,
            Message.created_at >= day_start,
            Message.created_at < day_end,
        ).scalar() or 0
        daily.append({
            "date": day_start.strftime("%Y-%m-%d"),
            "tokens": day_tokens,
            "messages": day_messages,
        })

    return jsonify({"data": daily})
