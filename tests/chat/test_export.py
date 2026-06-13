"""Export routes tests: JSON export, Markdown export, rate limiting, auth checks."""
import json
from datetime import datetime, timezone, timedelta
from app.models import Thread, Message
from app.chat.export import _extract_text, _check_export_rate, _record_export, _export_timestamps


def _thread(db, user, title="Export Thread"):
    import uuid
    t = Thread(id=str(uuid.uuid4()), user_id=user.id, title=title)
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


def _make_premium(db, user):
    user.tier = "premium"
    user.subscription_status = "active"
    db.session.commit()


# ─── _extract_text ───

def test_export_extract_text_plain():
    assert _extract_text("Hello") == "Hello"


def test_export_extract_text_empty():
    assert _extract_text("") == ""
    assert _extract_text(None) == ""


def test_export_extract_text_json_blocks():
    content = json.dumps([
        {"type": "text", "text": "Hello"},
        {"type": "image", "file": "photo.png"},
    ])
    result = _extract_text(content)
    assert "Hello" in result
    assert "[Image: photo.png]" in result


def test_export_extract_text_invalid_json():
    assert _extract_text("[not json") == "[not json"


# ─── rate limiting ───

def test_export_rate_limit_allows(app):
    _export_timestamps.clear()
    allowed, count = _check_export_rate("user1")
    assert allowed is True
    assert count == 0


def test_export_rate_limit_blocks(app):
    _export_timestamps.clear()
    user_id = "rate-test-user"
    for _ in range(10):
        _record_export(user_id)
    allowed, count = _check_export_rate(user_id)
    assert allowed is False
    assert count == 10


# ─── export_thread endpoint ───

def test_export_requires_login(client):
    resp = client.get("/chat/fake-id/export/json")
    assert resp.status_code in (301, 302)


def test_export_invalid_format(app, db, make_user, login_as, client):
    user = make_user()
    login_as(user)
    resp = client.get("/chat/x/export/pdf")
    assert resp.status_code == 400


def test_export_non_premium_blocked(app, db, make_user, login_as, client):
    user = make_user()
    thread = _thread(db, user)
    _msg(db, thread, "user", "Hi", 0)
    login_as(user)
    _export_timestamps.clear()
    resp = client.get(f"/chat/{thread.id}/export/json")
    assert resp.status_code == 403
    assert b"premium_required" in resp.data


def test_export_json_success(app, db, make_user, login_as, client):
    user = make_user()
    thread = _thread(db, user, title="Export Me")
    _msg(db, thread, "user", "Question?", 0)
    _msg(db, thread, "assistant", "Answer!", 1)
    _make_premium(db, user)
    login_as(user)
    _export_timestamps.clear()
    resp = client.get(f"/chat/{thread.id}/export/json")
    assert resp.status_code == 200
    data = json.loads(resp.data)
    assert data["thread"]["title"] == "Export Me"
    assert len(data["messages"]) == 2
    assert data["messages"][0]["content"] == "Question?"
    assert data["export_metadata"]["total_messages"] == 2


def test_export_markdown_success(app, db, make_user, login_as, client):
    user = make_user()
    thread = _thread(db, user, title="MD Export")
    _msg(db, thread, "user", "Hello", 0)
    _msg(db, thread, "assistant", "World", 1)
    _make_premium(db, user)
    login_as(user)
    _export_timestamps.clear()
    resp = client.get(f"/chat/{thread.id}/export/md")
    assert resp.status_code == 200
    assert b"# MD Export" in resp.data
    assert b"**You**" in resp.data
    assert b"Hello" in resp.data
    assert b"World" in resp.data


def test_export_empty_thread(app, db, make_user, login_as, client):
    user = make_user()
    thread = _thread(db, user, title="Empty")
    _make_premium(db, user)
    login_as(user)
    _export_timestamps.clear()
    resp = client.get(f"/chat/{thread.id}/export/json")
    assert resp.status_code == 400
    assert b"No messages to export" in resp.data


def test_export_not_owner(app, db, make_user, login_as, client):
    user1 = make_user()
    user2 = make_user(username="other", email="other@example.com")
    thread = _thread(db, user1, title="Mine")
    _msg(db, thread, "user", "Hi", 0)
    _make_premium(db, user2)
    login_as(user2)
    _export_timestamps.clear()
    resp = client.get(f"/chat/{thread.id}/export/json")
    assert resp.status_code == 404


def test_export_rate_limited(app, db, make_user, login_as, client):
    user = make_user()
    thread = _thread(db, user, title="RL")
    _msg(db, thread, "user", "Hi", 0)
    _make_premium(db, user)
    login_as(user)
    _export_timestamps.clear()
    for _ in range(10):
        _record_export(user.id)
    resp = client.get(f"/chat/{thread.id}/export/json")
    assert resp.status_code == 429


def test_export_json_multimodal_content(app, db, make_user, login_as, client):
    user = make_user()
    thread = _thread(db, user, title="MM")
    multimodal = json.dumps([
        {"type": "text", "text": "What is this?"},
        {"type": "image", "file": "photo.png"},
    ])
    _msg(db, thread, "user", multimodal, 0)
    _make_premium(db, user)
    login_as(user)
    _export_timestamps.clear()
    resp = client.get(f"/chat/{thread.id}/export/json")
    assert resp.status_code == 200
    data = json.loads(resp.data)
    assert "What is this?" in data["messages"][0]["content"]
    assert "[Image: photo.png]" in data["messages"][0]["content"]
