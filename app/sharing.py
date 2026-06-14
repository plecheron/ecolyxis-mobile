"""Conversation sharing: create/revoke public read-only links."""
import uuid
from datetime import datetime, timezone, timedelta
from flask import Blueprint, render_template, request, jsonify, abort, redirect, url_for
from flask_login import login_required, current_user
from app import db
from app.models import Thread, Message, SharedLink

share_bp = Blueprint("share", __name__)


@share_bp.route("/share/create/<string:thread_id>", methods=["POST"])
@login_required
def create_share(thread_id):
    """Create a public share link for a thread."""
    thread = db.session.get(Thread, thread_id)
    if not thread or thread.user_id != current_user.id:
        return jsonify({"error": "Thread not found"}), 404

    # Check if there's already an active share
    existing = SharedLink.query.filter_by(
        thread_id=thread_id, user_id=current_user.id, is_active=True
    ).first()
    if existing and not existing.is_expired():
        return jsonify({
            "share_id": existing.id,
            "url": url_for("share.view_shared", share_id=existing.id, _external=True),
            "view_count": existing.view_count,
        })

    link = SharedLink(
        thread_id=thread_id,
        user_id=current_user.id,
        expires_at=datetime.now(timezone.utc) + timedelta(days=7),
    )
    db.session.add(link)
    db.session.commit()

    return jsonify({
        "share_id": link.id,
        "url": url_for("share.view_shared", share_id=link.id, _external=True),
        "view_count": 0,
    })


@share_bp.route("/share/revoke/<string:share_id>", methods=["POST"])
@login_required
def revoke_share(share_id):
    """Revoke a share link."""
    link = db.session.get(SharedLink, share_id)
    if not link or link.user_id != current_user.id:
        return jsonify({"error": "Share link not found"}), 404

    link.is_active = False
    db.session.commit()
    return jsonify({"success": True})


@share_bp.route("/share/status/<string:thread_id>")
@login_required
def share_status(thread_id):
    """Get share status for a thread."""
    links = SharedLink.query.filter_by(
        thread_id=thread_id, user_id=current_user.id, is_active=True
    ).all()
    active = [l for l in links if not l.is_expired()]
    return jsonify({
        "shared": len(active) > 0,
        "links": [{
            "share_id": l.id,
            "url": url_for("share.view_shared", share_id=l.id, _external=True),
            "view_count": l.view_count,
            "created_at": l.created_at.isoformat() if l.created_at else None,
        } for l in active],
    })


@share_bp.route("/s/<string:share_id>")
def view_shared(share_id):
    """Public read-only view of a shared conversation."""
    link = db.session.get(SharedLink, share_id)
    if not link or not link.is_active or link.is_expired():
        abort(404)

    thread = db.session.get(Thread, link.thread_id)
    if not thread:
        abort(404)

    # Pagination (#124) — cap at 50 messages per page
    page = request.args.get("page", 1, type=int)
    per_page = 50
    total = Message.query.filter_by(thread_id=thread.id).count()
    messages = (
        Message.query
        .filter_by(thread_id=thread.id)
        .order_by(Message.created_at)
        .offset((page - 1) * per_page)
        .limit(per_page)
        .all()
    )
    total_pages = max(1, (total + per_page - 1) // per_page)

    # Increment view count (only on first page to avoid inflation)
    if page == 1:
        link.view_count += 1
        db.session.commit()

    return render_template(
        "shared.html",
        thread=thread,
        messages=messages,
        shared_link=link,
        page=page,
        total_pages=total_pages,
    )
