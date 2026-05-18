"""Priority queue for LLM requests using PostgreSQL via SQLAlchemy."""
import time
import logging
from flask import current_app, has_app_context
from app import db
from app.models import LLMQueueEntry

logger = logging.getLogger(__name__)


def init_queue():
    """No-op marker for app startup. Schema is owned by Flask-Migrate now."""
    logger.info("Queue subsystem initialized")


def enter_queue(user_id, is_premium, timeout=90, _app=None):
    """Enter the queue. Wait until we are the highest-priority entry. Returns entry id or None on timeout."""
    app = _app or current_app._get_current_object()

    with app.app_context():
        now = time.time()

        # Clean up stale entries (processing for >120s = crashed worker)
        cutoff = now - 120
        LLMQueueEntry.query.filter(
            LLMQueueEntry.status == "processing",
            LLMQueueEntry.created_at < cutoff,
        ).delete()
        db.session.commit()

        entry = LLMQueueEntry(
            user_id=user_id,
            is_premium=bool(is_premium),
            created_at=now,
            status="waiting",
        )
        db.session.add(entry)
        db.session.commit()
        entry_id = entry.id

    deadline = now + timeout
    try:
        while time.time() < deadline:
            with app.app_context():
                top = (
                    LLMQueueEntry.query
                    .filter_by(status="waiting")
                    .order_by(LLMQueueEntry.is_premium.desc(), LLMQueueEntry.created_at.asc())
                    .first()
                )
                if top and top.id == entry_id:
                    top.status = "processing"
                    db.session.commit()
                    return entry_id
            time.sleep(0.3)

        # Timeout
        with app.app_context():
            LLMQueueEntry.query.filter_by(id=entry_id).delete()
            db.session.commit()
        return None
    except Exception:
        try:
            with app.app_context():
                LLMQueueEntry.query.filter_by(id=entry_id).delete()
                db.session.commit()
        except Exception:
            pass
        raise


def leave_queue(entry_id, _app=None):
    """Remove entry from queue after LLM request completes."""
    if entry_id is None:
        return
    try:
        app = _app or current_app._get_current_object()
        with app.app_context():
            LLMQueueEntry.query.filter_by(id=entry_id).delete()
            db.session.commit()
    except Exception:
        pass


def queue_stats():
    """Return current queue stats for admin dashboard."""
    waiting_free = LLMQueueEntry.query.filter_by(status="waiting", is_premium=False).count()
    waiting_premium = LLMQueueEntry.query.filter_by(status="waiting", is_premium=True).count()
    processing = LLMQueueEntry.query.filter_by(status="processing").count()
    return {"waiting_free": waiting_free, "waiting_premium": waiting_premium, "processing": processing}
