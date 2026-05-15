#!/usr/bin/env python3
"""Delete messages older than 24h for free-tier users. Run via cron."""
import sys
sys.path.insert(0, "/opt/Ecolyxis")

from datetime import datetime, timezone, timedelta
from app import create_app, db
from app.models import User, Thread, Message

app = create_app()

with app.app_context():
    cutoff = datetime.now(timezone.utc) - timedelta(hours=24)
    free_users = User.query.filter(User.tier != "premium").all()

    total_deleted = 0
    for user in free_users:
        thread_ids = [t.id for t in Thread.query.filter_by(user_id=user.id).all()]
        if not thread_ids:
            continue
        deleted = Message.query.filter(
            Message.thread_id.in_(thread_ids),
            Message.created_at < cutoff
        ).delete(synchronize_session="fetch")
        total_deleted += deleted

    # Also delete empty threads for free users (threads with no messages left)
    for user in free_users:
        empty_threads = Thread.query.filter_by(user_id=user.id).all()
        for t in empty_threads:
            if Message.query.filter_by(thread_id=t.id).count() == 0:
                db.session.delete(t)

    db.session.commit()
    print(f"[{datetime.now(timezone.utc).isoformat()}] Deleted {total_deleted} expired messages for {len(free_users)} free users")
