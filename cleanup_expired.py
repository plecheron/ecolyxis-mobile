#!/usr/bin/env python3
"""Periodic cleanup: expired messages, empty threads, stale rate limit buckets.
Run via cron every hour.
"""
import sys, time
sys.path.insert(0, "/opt/Ecolyxis")

from datetime import datetime, timezone, timedelta
from app import create_app, db
from app.models import User, Thread, Message

app = create_app()

with app.app_context():
    now_iso = datetime.now(timezone.utc).isoformat()

    # 1. Delete messages older than 24h for free-tier users
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

    # 2. Delete empty threads for free users
    threads_deleted = 0
    for user in free_users:
        empty_threads = Thread.query.filter_by(user_id=user.id).all()
        for t in empty_threads:
            if Message.query.filter_by(thread_id=t.id).count() == 0:
                db.session.delete(t)
                threads_deleted += 1

    db.session.commit()

    # 3. Clean stale rate limit buckets (older than 24h)
    bucket_cutoff = time.time() - 86400
    bucket_result = db.session.execute(
        db.text("DELETE FROM rate_limit_bucket WHERE last_refill < :cutoff"),
        {"cutoff": bucket_cutoff},
    )
    db.session.commit()
    buckets_deleted = bucket_result.rowcount

    print(f"[{now_iso}] Cleanup: {total_deleted} expired messages, "
          f"{threads_deleted} empty threads, {buckets_deleted} stale rate limit buckets")
