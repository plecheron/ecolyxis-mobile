from flask import Blueprint, render_template, redirect, url_for, request, flash, make_response
from flask_login import login_required, current_user
from sqlalchemy import func
from app import db
from app.models import Thread, Message, Workspace
from datetime import datetime, timezone, timedelta

dash_bp = Blueprint("dashboard", __name__)


@dash_bp.route("/dashboard")
@login_required
def index():
    q = request.args.get("q", "").strip()

    threads_q = (
        Thread.query.filter_by(user_id=current_user.id)
        .filter(Thread.messages.any())  # Hide empty threads
        .order_by(Thread.updated_at.desc())
    )

    if q and len(q) >= 2:
        threads_q = threads_q.filter(Thread.messages.any(Message.content.ilike(f"%{q}%")))

    threads = threads_q.all()
    thread_ids = [t.id for t in threads]

    # Batch: message counts + last message snippet per thread (single query)
    msg_counts = {}
    last_snippets = {}
    if thread_ids:
        # Count per thread
        counts = (
            db.session.query(Message.thread_id, func.count(Message.id))
            .filter(Message.thread_id.in_(thread_ids))
            .group_by(Message.thread_id)
            .all()
        )
        msg_counts = dict(counts)

        # Last message snippet per thread
        from sqlalchemy.sql import and_
        subq = (
            db.session.query(
                Message.thread_id,
                func.max(Message.id).label("max_id")
            )
            .filter(Message.thread_id.in_(thread_ids))
            .group_by(Message.thread_id)
            .subquery()
        )
        last_msgs = (
            db.session.query(Message.thread_id, Message.content)
            .join(subq, and_(Message.thread_id == subq.c.thread_id, Message.id == subq.c.max_id))
            .all()
        )
        last_snippets = {tid: (content[:80] + ("..." if len(content) > 80 else "")) for tid, content in last_msgs}

    # Token usage stats (combined into fewer queries)
    today_start = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)

    stats_row = (
        db.session.query(
            func.coalesce(func.sum(Message.tokens_used), 0).label("total_tokens"),
            func.count(Message.id).label("total_messages"),
        )
        .join(Thread, Message.thread_id == Thread.id)
        .filter(Thread.user_id == current_user.id)
        .one()
    )

    today_tokens = (
        db.session.query(func.coalesce(func.sum(Message.tokens_used), 0))
        .join(Thread, Message.thread_id == Thread.id)
        .filter(Thread.user_id == current_user.id, Message.created_at >= today_start)
        .scalar()
    ) or 0

    stats = {
        "total_tokens": stats_row.total_tokens,
        "total_messages": stats_row.total_messages,
        "today_tokens": today_tokens,
        "total_threads": len(threads),
    }

    # --- Workspaces ---
    workspaces = (
        Workspace.query.filter_by(user_id=current_user.id)
        .order_by(Workspace.updated_at.desc())
        .all()
    )
    # Pre-compute thread count per workspace (single query)
    ws_thread_counts = {}
    if workspaces:
        ws_ids = [ws.id for ws in workspaces]
        ws_counts = (
            db.session.query(Thread.workspace_id, func.count(Thread.id))
            .filter(Thread.workspace_id.in_(ws_ids), Thread.user_id == current_user.id)
            .group_by(Thread.workspace_id)
            .all()
        )
        ws_thread_counts = dict(ws_counts)

    return render_template("dashboard.html", threads=threads, query=q, stats=stats,
                           msg_counts=msg_counts, last_snippets=last_snippets,
                           workspaces=workspaces, ws_thread_counts=ws_thread_counts)


@dash_bp.route("/threads", methods=["POST"])
@login_required
def create_thread():
    thread = Thread(user_id=current_user.id, title="New Chat")
    # Optional workspace_id: accept from form data or query string
    ws_id = request.form.get("workspace_id") or request.args.get("workspace_id")
    if ws_id:
        ws = Workspace.query.filter_by(id=ws_id, user_id=current_user.id).first()
        if ws:
            thread.workspace_id = ws.id
    db.session.add(thread)
    db.session.commit()
    resp = make_response(redirect(url_for("chat.view", thread_id=thread.id)))
    resp.status_code = 303
    return resp


@dash_bp.route("/threads/<string:thread_id>", methods=["DELETE"])
@login_required
def delete_thread(thread_id):
    thread = Thread.query.filter_by(id=thread_id, user_id=current_user.id).first_or_404()
    db.session.delete(thread)
    db.session.commit()

    if request.headers.get("HX-Request"):
        return "", 200
    flash("Thread deleted.", "success")
    return redirect(url_for("dashboard.index"))


@dash_bp.route("/threads/bulk-delete", methods=["POST"])
@login_required
def bulk_delete_threads():
    data = request.get_json(silent=True) or request.form.to_dict()
    thread_ids = data.get("thread_ids", [])
    if isinstance(thread_ids, str):
        import json
        try:
            thread_ids = json.loads(thread_ids)
        except (json.JSONDecodeError, TypeError):
            thread_ids = []

    if not thread_ids:
        return jsonify({"error": "No thread IDs provided"}), 400

    # Only delete threads belonging to the current user
    deleted = Thread.query.filter(
        Thread.id.in_(thread_ids),
        Thread.user_id == current_user.id
    ).delete(synchronize_session="fetch")
    db.session.commit()

    return jsonify({"deleted": deleted})
