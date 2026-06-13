"""Chat route tests — save message, edit message, search, upload serving, rate limit."""
import uuid
import json
from datetime import datetime, timezone, timedelta
from unittest.mock import patch, MagicMock
from app.models import Thread, Message


def _thread(db, user, **kw):
    t = Thread(id=str(uuid.uuid4()), user_id=user.id, title="T", **kw)
    db.session.add(t)
    db.session.commit()
    return t


def _msg(db, thread, role, content, secs=0):
    m = Message(
        thread_id=thread.id, role=role, content=content,
        created_at=datetime(2026, 1, 1, tzinfo=timezone.utc) + timedelta(seconds=secs),
    )
    db.session.add(m)
    db.session.commit()
    return m


def _premium_user(make_user, email="premium@example.com"):
    """Create a premium user with correct fields."""
    return make_user(email=email, tier="premium", subscription_status="active")


def test_save_message_persists_assistant_response(app, db, make_user, login_as, client):
    user = make_user()
    thread = _thread(db, user)
    login_as(user)
    resp = client.post(
        f"/chat/{thread.id}/save",
        data=json.dumps({"content": "Hello from the assistant"}),
        content_type="application/json",
    )
    assert resp.status_code == 200
    msg = Message.query.filter_by(thread_id=thread.id, role="assistant").first()
    assert msg is not None
    assert "Hello from the assistant" in msg.content


def test_save_message_owner_scoped(app, db, make_user, login_as, client):
    owner = make_user(email="owner@example.com")
    other = make_user(email="other@example.com")
    thread = _thread(db, owner)
    login_as(other)
    resp = client.post(
        f"/chat/{thread.id}/save",
        data=json.dumps({"content": "hack"}),
        content_type="application/json",
    )
    assert resp.status_code == 404


def test_edit_message_owner_scoped(app, db, make_user, login_as, client):
    """Can't edit another user's message."""
    owner = make_user(email="owner@example.com")
    other = make_user(email="other@example.com")
    thread = _thread(db, owner)
    m = _msg(db, thread, "user", "secret", 0)
    login_as(other)
    resp = client.post(
        f"/chat/{thread.id}/edit/{m.id}",
        data=json.dumps({"content": "hacked"}),
        content_type="application/json",
    )
    assert resp.status_code == 404
    assert "secret" in db.session.get(Message, m.id).content


def test_chat_view_requires_auth(app, client):
    resp = client.get(f"/chat/{uuid.uuid4()}", follow_redirects=False)
    assert resp.status_code in (301, 302, 303)


def test_chat_view_nonexistent_thread_404(app, db, make_user, login_as, client):
    user = make_user()
    login_as(user)
    resp = client.get(f"/chat/{uuid.uuid4()}")
    assert resp.status_code == 404


def test_chat_view_owner_scoped(app, db, make_user, login_as, client):
    owner = make_user(email="owner@example.com")
    other = make_user(email="other@example.com")
    thread = _thread(db, owner)
    _msg(db, thread, "user", "private", 0)
    login_as(other)
    resp = client.get(f"/chat/{thread.id}")
    assert resp.status_code == 404


def test_rate_limit_status_endpoint(app, db, make_user, login_as, client):
    user = make_user()
    login_as(user)
    resp = client.get("/api/rate-limit")
    assert resp.status_code == 200
    data = resp.get_json()
    assert data is not None


def test_save_system_prompt_premium(app, db, make_user, login_as, client):
    """POST /chat/<id>/system-prompt should save for premium users."""
    user = _premium_user(make_user)
    thread = _thread(db, user)
    login_as(user)
    resp = client.post(
        f"/chat/{thread.id}/system-prompt",
        data=json.dumps({"system_prompt": "You are a helpful assistant"}),
        content_type="application/json",
    )
    assert resp.status_code == 200
    updated = db.session.get(Thread, thread.id)
    assert updated.system_prompt == "You are a helpful assistant"


def test_save_system_prompt_blocked_for_free(app, db, make_user, login_as, client):
    """System prompt should be blocked for non-premium users."""
    user = make_user()
    thread = _thread(db, user)
    login_as(user)
    resp = client.post(
        f"/chat/{thread.id}/system-prompt",
        data=json.dumps({"system_prompt": "You are a helpful assistant"}),
        content_type="application/json",
    )
    assert resp.status_code == 403


def test_serve_upload_404_for_missing_file(app, db, make_user, login_as, client):
    user = make_user()
    login_as(user)
    resp = client.get("/uploads/nonexistent_file.png")
    assert resp.status_code == 404


def test_thread_mode_save(app, db, make_user, login_as, client):
    """POST /chat/<id>/mode should persist the conversation mode."""
    user = make_user()
    thread = _thread(db, user)
    login_as(user)
    resp = client.post(
        f"/chat/{thread.id}/mode",
        data=json.dumps({"mode": "standard"}),
        content_type="application/json",
    )
    assert resp.status_code in (200, 204)
    updated = db.session.get(Thread, thread.id)
    assert updated.last_mode == "standard"


def test_thread_mode_rejects_invalid(app, db, make_user, login_as, client):
    """Invalid mode should be rejected."""
    user = make_user()
    thread = _thread(db, user)
    login_as(user)
    resp = client.post(
        f"/chat/{thread.id}/mode",
        data=json.dumps({"mode": "invalid_mode"}),
        content_type="application/json",
    )
    assert resp.status_code in (400, 422)
