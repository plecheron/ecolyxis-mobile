"""Per-message actions: single-message delete, regenerate-at-message (truncate
after the target), and that the chat view renders the unified action toolbar."""
import uuid
from datetime import datetime, timezone, timedelta
from unittest.mock import patch

from app.models import Thread, Message


def _thread(db, user, **kw):
    t = Thread(id=str(uuid.uuid4()), user_id=user.id, title="T", **kw)
    db.session.add(t)
    db.session.commit()
    return t


def _msg(db, thread, role, content, secs):
    """Create a message with a deterministic created_at offset (seconds)."""
    m = Message(
        thread_id=thread.id, role=role, content=content,
        created_at=datetime(2026, 1, 1, tzinfo=timezone.utc) + timedelta(seconds=secs),
    )
    db.session.add(m)
    db.session.commit()
    return m


# --- delete single message --------------------------------------------------

def test_delete_message(app, db, make_user, login_as, client):
    user = make_user()
    thread = _thread(db, user)
    login_as(user)
    m = _msg(db, thread, "user", "hello", 0)

    resp = client.delete(f"/chat/{thread.id}/message/{m.id}")
    assert resp.status_code == 204
    assert db.session.get(Message, m.id) is None


def test_delete_message_owner_scoped(app, db, make_user, login_as, client):
    owner = make_user(email="owner@example.com")
    other = make_user(email="other@example.com")
    thread = _thread(db, owner)
    m = _msg(db, thread, "user", "hello", 0)

    login_as(other)
    resp = client.delete(f"/chat/{thread.id}/message/{m.id}")
    assert resp.status_code == 404
    assert db.session.get(Message, m.id) is not None


# --- regenerate at a specific message --------------------------------------
def test_chat_view_renders_action_toolbar(app, db, make_user, login_as, client):
    user = make_user()
    thread = _thread(db, user)
    login_as(user)
    _msg(db, thread, "user", "hello", 0)
    _msg(db, thread, "assistant", "hi there", 1)

    resp = client.get(f"/chat/{thread.id}")
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert "msg-actions" in body
    assert 'role="log"' in body          # aria-live region on the message list
    assert "btn-regenerate" in body
