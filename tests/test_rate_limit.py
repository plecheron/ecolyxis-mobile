"""Free-tier chat rate limit: 5 messages per 3600s window."""
import uuid
from datetime import datetime, timezone, timedelta
from app.models import Thread, Message
from app.chat import check_rate_limit


def _make_thread(db, user):
    t = Thread(id=str(uuid.uuid4()), user_id=user.id, title="Test")
    db.session.add(t)
    db.session.commit()
    return t


def _add_user_message(db, thread, when):
    m = Message(thread_id=thread.id, role="user", content="hi", created_at=when)
    db.session.add(m)
    db.session.commit()


def test_free_user_under_limit_allowed(app, db, make_user):
    user = make_user()
    thread = _make_thread(db, user)
    now = datetime.now(timezone.utc)
    for _ in range(3):
        _add_user_message(db, thread, now)

    with app.test_request_context():
        from flask_login import login_user
        login_user(user)
        allowed, used, limit = check_rate_limit()
        assert allowed is True
        assert used == 3
        assert limit == 5


def test_free_user_at_limit_blocked(app, db, make_user):
    user = make_user()
    thread = _make_thread(db, user)
    now = datetime.now(timezone.utc)
    for _ in range(5):
        _add_user_message(db, thread, now)

    with app.test_request_context():
        from flask_login import login_user
        login_user(user)
        allowed, used, _ = check_rate_limit()
        assert allowed is False
        assert used == 5


def test_old_messages_dont_count(app, db, make_user):
    user = make_user()
    thread = _make_thread(db, user)
    old = datetime.now(timezone.utc) - timedelta(hours=2)  # outside 1h window
    for _ in range(10):
        _add_user_message(db, thread, old)

    with app.test_request_context():
        from flask_login import login_user
        login_user(user)
        allowed, used, _ = check_rate_limit()
        assert allowed is True
        assert used == 0


def test_premium_user_bypasses_limit(app, db, make_user):
    user = make_user()
    user.tier = "premium"
    user.subscription_status = "active"
    db.session.commit()
    thread = _make_thread(db, user)
    now = datetime.now(timezone.utc)
    for _ in range(50):
        _add_user_message(db, thread, now)

    with app.test_request_context():
        from flask_login import login_user
        login_user(user)
        allowed, used, limit = check_rate_limit()
        assert allowed is True
        assert limit is None


def test_assistant_messages_dont_count(app, db, make_user):
    user = make_user()
    thread = _make_thread(db, user)
    now = datetime.now(timezone.utc)
    for _ in range(3):
        _add_user_message(db, thread, now)
    for _ in range(10):
        m = Message(thread_id=thread.id, role="assistant", content="reply", created_at=now)
        db.session.add(m)
    db.session.commit()

    with app.test_request_context():
        from flask_login import login_user
        login_user(user)
        _, used, _ = check_rate_limit()
        assert used == 3
