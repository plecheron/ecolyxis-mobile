"""Chat routes — extended tests: delete, rename, clear, generate-title, search,
compact, compact-save, upload, workspace-context-stats."""
import uuid
import json
import os
import io
from datetime import datetime, timezone, timedelta
from unittest.mock import patch, MagicMock
from app.models import Thread, Message, Workspace
from app import db as _db


def _thread(db, user, **kw):
    t = Thread(id=str(uuid.uuid4()), user_id=user.id, title=kw.pop("title", "New Chat"), **kw)
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
    return make_user(email=email, tier="premium", subscription_status="active")


def _mock_client(chunks=None):
    """Create a mock LLM client that yields given string chunks."""
    client = MagicMock()
    client.stream_chat.return_value = iter(chunks or ["Summarised text"])
    client.build_messages.return_value = [{"role": "user", "content": "test"}]
    return client


# ─── delete_message ───

def test_delete_message_success(app, db, make_user, login_as, client):
    user = make_user()
    thread = _thread(db, user)
    m = _msg(db, thread, "user", "hello", 0)
    login_as(user)
    resp = client.delete(f"/chat/{thread.id}/message/{m.id}")
    assert resp.status_code == 204
    assert db.session.get(Message, m.id) is None


def test_delete_message_nonexistent_thread_404(app, db, make_user, login_as, client):
    user = make_user()
    login_as(user)
    resp = client.delete(f"/chat/{uuid.uuid4()}/message/999")
    assert resp.status_code == 404


def test_delete_message_nonexistent_msg_404(app, db, make_user, login_as, client):
    user = make_user()
    thread = _thread(db, user)
    login_as(user)
    resp = client.delete(f"/chat/{thread.id}/message/99999")
    assert resp.status_code == 404


def test_delete_message_owner_scoped(app, db, make_user, login_as, client):
    owner = make_user(email="owner@example.com")
    other = make_user(email="other@example.com")
    thread = _thread(db, owner)
    m = _msg(db, thread, "user", "secret", 0)
    login_as(other)
    resp = client.delete(f"/chat/{thread.id}/message/{m.id}")
    assert resp.status_code == 404
    assert db.session.get(Message, m.id) is not None


# ─── rename_thread ───

def test_rename_thread_success(app, db, make_user, login_as, client):
    user = make_user()
    thread = _thread(db, user)
    login_as(user)
    resp = client.patch(
        f"/chat/{thread.id}/rename",
        data=json.dumps({"title": "My New Title"}),
        content_type="application/json",
    )
    assert resp.status_code == 200
    assert db.session.get(Thread, thread.id).title == "My New Title"


def test_rename_thread_empty_title_400(app, db, make_user, login_as, client):
    user = make_user()
    thread = _thread(db, user)
    login_as(user)
    resp = client.patch(
        f"/chat/{thread.id}/rename",
        data=json.dumps({"title": "   "}),
        content_type="application/json",
    )
    assert resp.status_code == 400


def test_rename_thread_nonexistent_404(app, db, make_user, login_as, client):
    user = make_user()
    login_as(user)
    resp = client.patch(
        f"/chat/{uuid.uuid4()}/rename",
        data=json.dumps({"title": "X"}),
        content_type="application/json",
    )
    assert resp.status_code == 404


def test_rename_thread_owner_scoped(app, db, make_user, login_as, client):
    owner = make_user(email="owner@example.com")
    other = make_user(email="other@example.com")
    thread = _thread(db, owner)
    login_as(other)
    resp = client.patch(
        f"/chat/{thread.id}/rename",
        data=json.dumps({"title": "Hacked"}),
        content_type="application/json",
    )
    assert resp.status_code == 404


# ─── clear_thread ───

def test_clear_thread_removes_all_messages(app, db, make_user, login_as, client):
    user = make_user()
    thread = _thread(db, user)
    _msg(db, thread, "user", "msg1", 0)
    _msg(db, thread, "assistant", "resp1", 1)
    _msg(db, thread, "user", "msg2", 2)
    login_as(user)
    resp = client.post(f"/chat/{thread.id}/clear")
    assert resp.status_code == 200
    assert Message.query.filter_by(thread_id=thread.id).count() == 0


def test_clear_thread_nonexistent_404(app, db, make_user, login_as, client):
    user = make_user()
    login_as(user)
    resp = client.post(f"/chat/{uuid.uuid4()}/clear")
    assert resp.status_code == 404


def test_clear_thread_owner_scoped(app, db, make_user, login_as, client):
    owner = make_user(email="owner@example.com")
    other = make_user(email="other@example.com")
    thread = _thread(db, owner)
    _msg(db, thread, "user", "secret", 0)
    login_as(other)
    resp = client.post(f"/chat/{thread.id}/clear")
    assert resp.status_code == 404
    assert Message.query.filter_by(thread_id=thread.id).count() == 1


# ─── generate_title ───

def test_generate_title_with_mocked_llm(app, db, make_user, login_as, client):
    user = make_user()
    thread = _thread(db, user)  # title="New Chat"
    _msg(db, thread, "user", "How do I bake a chocolate cake?", 0)
    login_as(user)
    mock_client = _mock_client(chunks=["Baking", " Chocolate Cake"])
    with patch("app.chat.routes.get_client", return_value=mock_client):
        resp = client.post(f"/chat/{thread.id}/generate-title")
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["title"] == "Baking Chocolate Cake"
    assert db.session.get(Thread, thread.id).title == "Baking Chocolate Cake"


def test_generate_title_skips_if_already_titled(app, db, make_user, login_as, client):
    user = make_user()
    thread = _thread(db, user, title="Custom Title")
    _msg(db, thread, "user", "hello", 0)
    login_as(user)
    resp = client.post(f"/chat/{thread.id}/generate-title")
    assert resp.status_code == 200
    assert resp.get_json()["title"] == "Custom Title"


def test_generate_title_no_messages(app, db, make_user, login_as, client):
    user = make_user()
    thread = _thread(db, user)
    login_as(user)
    resp = client.post(f"/chat/{thread.id}/generate-title")
    assert resp.status_code == 200
    assert resp.get_json()["title"] == "New Chat"


def test_generate_title_fallback_on_error(app, db, make_user, login_as, client):
    user = make_user()
    thread = _thread(db, user)
    _msg(db, thread, "user", "This is a very long question about quantum physics and entanglement", 0)
    login_as(user)
    mock_client = MagicMock()
    mock_client.stream_chat.side_effect = Exception("LLM unavailable")
    with patch("app.chat.routes.get_client", return_value=mock_client):
        resp = client.post(f"/chat/{thread.id}/generate-title")
    assert resp.status_code == 200
    title = resp.get_json()["title"]
    assert "quantum" in title.lower()  # truncated fallback


# ─── search ───

def test_search_premium_only(app, db, make_user, login_as, client):
    user = make_user()
    login_as(user)
    resp = client.get("/api/search?q=test")
    assert resp.status_code == 403


def test_search_results(app, db, make_user, login_as, client):
    user = _premium_user(make_user)
    thread = _thread(db, user)
    _msg(db, thread, "user", "Tell me about quantum physics", 0)
    _msg(db, thread, "assistant", "Quantum physics is fascinating", 1)
    login_as(user)
    resp = client.get("/api/search?q=quantum")
    assert resp.status_code == 200
    results = resp.get_json()["results"]
    assert len(results) >= 1
    assert any("quantum" in r["content"].lower() for r in results)


def test_search_short_query_returns_empty(app, db, make_user, login_as, client):
    user = _premium_user(make_user)
    login_as(user)
    resp = client.get("/api/search?q=a")
    assert resp.status_code == 200
    assert resp.get_json()["results"] == []


def test_search_owner_scoped(app, db, make_user, login_as, client):
    owner = _premium_user(make_user, email="owner@example.com")
    other = _premium_user(make_user, email="other@example.com")
    thread = _thread(db, owner)
    _msg(db, thread, "user", "unique_secret_data", 0)
    login_as(other)
    resp = client.get("/api/search?q=unique_secret_data")
    assert resp.status_code == 200
    assert len(resp.get_json()["results"]) == 0


# ─── serve_upload (path traversal) ───

def test_serve_upload_path_traversal_blocked(app, db, make_user, login_as, client):
    user = make_user()
    login_as(user)
    resp = client.get("/uploads/../etc/passwd")
    assert resp.status_code == 404


def test_serve_upload_invalid_chars_blocked(app, db, make_user, login_as, client):
    user = make_user()
    login_as(user)
    resp = client.get("/uploads/file;rm-rf.png")
    assert resp.status_code == 404


# ─── compact_thread ───

def test_compact_too_few_messages(app, db, make_user, login_as, client):
    user = make_user()
    thread = _thread(db, user)
    _msg(db, thread, "user", "only one", 0)
    login_as(user)
    resp = client.post(f"/chat/{thread.id}/compact")
    assert resp.status_code == 200
    data = b"".join(resp.data for _ in [0])  # consume response
    assert b"Nothing to compact" in resp.data


def test_compact_thread_success(app, db, make_user, login_as, client):
    user = make_user()
    thread = _thread(db, user)
    _msg(db, thread, "user", "What is AI?", 0)
    _msg(db, thread, "assistant", "AI is artificial intelligence.", 1)
    _msg(db, thread, "user", "Tell me more", 2)
    login_as(user)
    mock_client = _mock_client(chunks=["AI stands for Artificial Intelligence."])
    with patch("app.chat.routes.get_client", return_value=mock_client), \
         patch("app.chat.routes.check_rate_limit", return_value=(True, 1, 100)):
        resp = client.post(f"/chat/{thread.id}/compact")
    assert resp.status_code == 200
    body = resp.data.decode()
    assert "done" in body
    assert "compacted" in body
    # Old messages deleted, new summary messages created
    msgs = Message.query.filter_by(thread_id=thread.id).all()
    assert len(msgs) == 2  # user summary + assistant summary


def test_compact_thread_rate_limited(app, db, make_user, login_as, client):
    user = make_user()
    thread = _thread(db, user)
    _msg(db, thread, "user", "msg1", 0)
    _msg(db, thread, "assistant", "resp1", 1)
    login_as(user)
    with patch("app.chat.routes.check_rate_limit", return_value=(False, 100, 100)):
        resp = client.post(f"/chat/{thread.id}/compact")
    assert resp.status_code == 200
    assert b"rate_limited" in resp.data


# ─── compact_progressive ───

def test_compact_progressive_too_few(app, db, make_user, login_as, client):
    user = make_user()
    thread = _thread(db, user)
    _msg(db, thread, "user", "msg1", 0)
    _msg(db, thread, "assistant", "resp1", 1)
    login_as(user)
    resp = client.post(f"/chat/{thread.id}/compact/progressive")
    assert resp.status_code == 200
    assert b"Need at least 4" in resp.data


def test_compact_progressive_success(app, db, make_user, login_as, client):
    """Progressive compact streams a summary of the oldest half."""
    user = make_user()
    thread = _thread(db, user)
    for i in range(6):
        _msg(db, thread, "user" if i % 2 == 0 else "assistant", f"msg{i}", i)
    login_as(user)
    mock_client = _mock_client(chunks=["Summary of older messages."])
    with patch("app.chat.routes.get_client", return_value=mock_client), \
         patch("app.chat.routes.check_rate_limit", return_value=(True, 1, 100)):
        resp = client.post(f"/chat/{thread.id}/compact/progressive")
    assert resp.status_code == 200
    body = resp.data.decode()
    assert "compacted" in body
    assert "Summary of older messages" in body


# ─── compact_save ───

def test_compact_save_full(app, db, make_user, login_as, client):
    user = make_user()
    thread = _thread(db, user)
    _msg(db, thread, "user", "msg1", 0)
    _msg(db, thread, "assistant", "resp1", 1)
    login_as(user)
    resp = client.post(
        f"/chat/{thread.id}/compact/save",
        data=json.dumps({"content": "Custom summary", "msg_count": 2}),
        content_type="application/json",
    )
    assert resp.status_code == 200
    msgs = Message.query.filter_by(thread_id=thread.id).all()
    assert len(msgs) == 2
    assert "Custom summary" in msgs[1].content


def test_compact_save_empty_400(app, db, make_user, login_as, client):
    user = make_user()
    thread = _thread(db, user)
    login_as(user)
    resp = client.post(
        f"/chat/{thread.id}/compact/save",
        data=json.dumps({"content": "  "}),
        content_type="application/json",
    )
    assert resp.status_code == 400


# ─── upload_image ───

def test_upload_no_file(app, db, make_user, login_as, client):
    user = make_user()
    thread = _thread(db, user)
    login_as(user)
    resp = client.post(f"/chat/{thread.id}/upload")
    assert resp.status_code == 400


def test_upload_wrong_extension(app, db, make_user, login_as, client):
    user = make_user()
    thread = _thread(db, user)
    login_as(user)
    data = {"file": (io.BytesIO(b"fake"), "malware.exe")}
    resp = client.post(f"/chat/{thread.id}/upload", data=data, content_type="multipart/form-data")
    assert resp.status_code == 400
    assert "Unsupported" in resp.get_json()["error"]


def test_upload_valid_image(app, db, make_user, login_as, client):
    user = make_user()
    thread = _thread(db, user)
    login_as(user)
    # Minimal 1x1 PNG
    png_bytes = b'\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x02\x00\x00\x00\x90wS\xde\x00\x00\x00\x0cIDATx\x9cc\xf8\xff\xff?\x00\x05\xfe\x02\xfe\xa3=\x01_\x00\x00\x00\x00IEND\xaeB`\x82'
    data = {"file": (io.BytesIO(png_bytes), "test.png")}
    resp = client.post(f"/chat/{thread.id}/upload", data=data, content_type="multipart/form-data")
    assert resp.status_code == 200
    result = resp.get_json()
    assert "url" in result
    assert result["url"].startswith("/uploads/")
    # Cleanup
    fname = result.get("filename")
    if fname:
        path = os.path.join("/opt/Ecolyxis/uploads", fname)
        if os.path.exists(path):
            os.unlink(path)


# ─── workspace_context_stats ───

def test_workspace_context_stats_no_workspace(app, db, make_user, login_as, client):
    user = make_user()
    thread = _thread(db, user)
    login_as(user)
    resp = client.get(f"/api/workspace-context/{thread.id}")
    assert resp.status_code == 200
    assert resp.get_json()["enabled"] is False


def test_workspace_context_stats_with_workspace(app, db, make_user, login_as, client):
    user = make_user()
    ws = Workspace(id=str(uuid.uuid4()), user_id=user.id, name="Test WS")
    db.session.add(ws)
    db.session.commit()
    thread = _thread(db, user, workspace_id=ws.id, use_workspace_context=True)
    # Sibling thread with messages
    sib = _thread(db, user, title="Sibling", workspace_id=ws.id)
    _msg(db, sib, "user", "Important context about testing", 0)
    login_as(user)
    resp = client.get(f"/api/workspace-context/{thread.id}")
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["enabled"] is True
    assert data["workspace_name"] == "Test WS"
    assert data["sibling_thread_count"] >= 1


def test_workspace_context_stats_disabled(app, db, make_user, login_as, client):
    user = make_user()
    ws = Workspace(id=str(uuid.uuid4()), user_id=user.id, name="WS2")
    db.session.add(ws)
    db.session.commit()
    thread = _thread(db, user, workspace_id=ws.id, use_workspace_context=False)
    login_as(user)
    resp = client.get(f"/api/workspace-context/{thread.id}")
    assert resp.status_code == 200
    assert resp.get_json()["enabled"] is False


# ─── save_message variations ───

def test_save_message_with_tokens(app, db, make_user, login_as, client):
    user = make_user()
    thread = _thread(db, user)
    login_as(user)
    resp = client.post(
        f"/chat/{thread.id}/save",
        data=json.dumps({"content": "Response", "tokens_used": 42, "message_type": "text"}),
        content_type="application/json",
    )
    assert resp.status_code == 200
    msg = Message.query.filter_by(thread_id=thread.id, role="assistant").first()
    assert msg.tokens_used == 42


def test_save_message_empty_content(app, db, make_user, login_as, client):
    user = make_user()
    thread = _thread(db, user)
    login_as(user)
    resp = client.post(
        f"/chat/{thread.id}/save",
        data=json.dumps({"content": ""}),
        content_type="application/json",
    )
    assert resp.status_code == 200
    assert Message.query.filter_by(thread_id=thread.id).count() == 0


# ─── edit_message with mocked streaming ───

def test_edit_message_streams_response(app, db, make_user, login_as, client):
    user = make_user()
    thread = _thread(db, user)
    m = _msg(db, thread, "user", "Original question", 0)
    _msg(db, thread, "assistant", "Original answer", 1)
    login_as(user)
    mock_client = _mock_client(chunks=["Updated", " answer"])
    with patch("app.chat.routes.get_client", return_value=mock_client), \
         patch("app.chat.routes.get_workspace_context", return_value=""), \
         patch("app.chat.routes.check_rate_limit", return_value=(True, 1, 100)):
        resp = client.post(
            f"/chat/{thread.id}/edit/{m.id}",
            data=json.dumps({"content": "Edited question", "mode": "standard"}),
            content_type="application/json",
        )
    assert resp.status_code == 200
    body = resp.data.decode()
    assert "content" in body
    assert "done" in body
    assert "Updated" in body
    # Message content updated, subsequent deleted
    assert db.session.get(Message, m.id).content == "Edited question"
    assert Message.query.filter_by(thread_id=thread.id).count() == 1  # only edited msg


def test_edit_message_empty_content_error(app, db, make_user, login_as, client):
    user = make_user()
    thread = _thread(db, user)
    m = _msg(db, thread, "user", "hello", 0)
    login_as(user)
    with patch("app.chat.routes.check_rate_limit", return_value=(True, 1, 100)):
        resp = client.post(
            f"/chat/{thread.id}/edit/{m.id}",
            data=json.dumps({"content": ""}),
            content_type="application/json",
        )
    assert resp.status_code == 200
    assert b"Empty message" in resp.data


def test_edit_message_rate_limited(app, db, make_user, login_as, client):
    user = make_user()
    thread = _thread(db, user)
    m = _msg(db, thread, "user", "hello", 0)
    login_as(user)
    with patch("app.chat.routes.check_rate_limit", return_value=(False, 100, 100)):
        resp = client.post(
            f"/chat/{thread.id}/edit/{m.id}",
            data=json.dumps({"content": "edited"}),
            content_type="application/json",
        )
    assert resp.status_code == 200
    assert b"rate_limited" in resp.data
