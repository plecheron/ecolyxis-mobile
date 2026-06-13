"""Workspace summaries tests: conversation text building, summary generation, get_or_generate."""
import json
from datetime import datetime, timezone, timedelta
from unittest.mock import patch, MagicMock
import uuid
import requests
from app.models import Thread, Message, Workspace
from app.workspace.summaries import (
    _build_conversation_text,
    _call_llm_non_streaming,
    generate_thread_summary,
    get_or_generate_summary,
)


def _thread(db, user, ws_id=None, title="Test"):
    t = Thread(id=str(uuid.uuid4()), user_id=user.id, title=title, workspace_id=ws_id)
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


# ─── _build_conversation_text ───

def test_build_convo_text_empty_thread(app, db, make_user):
    user = make_user()
    thread = _thread(db, user)
    result = _build_conversation_text(thread)
    assert result == ""


def test_build_convo_text_basic(app, db, make_user):
    user = make_user()
    thread = _thread(db, user)
    _msg(db, thread, "user", "Hello AI", 0)
    _msg(db, thread, "assistant", "Hello human", 1)
    result = _build_conversation_text(thread)
    assert "User: Hello AI" in result
    assert "Assistant: Hello human" in result


def test_build_convo_text_truncation(app, db, make_user):
    user = make_user()
    thread = _thread(db, user)
    for i in range(20):
        _msg(db, thread, "user", f"Message number {i} " * 20, i)
    result = _build_conversation_text(thread, max_chars=500)
    assert len(result) <= 600


def test_build_convo_text_json_content(app, db, make_user):
    user = make_user()
    thread = _thread(db, user)
    content = json.dumps([
        {"type": "text", "text": "Hello from JSON"},
        {"type": "image", "file": "img.png"},
    ])
    _msg(db, thread, "user", content, 0)
    result = _build_conversation_text(thread)
    assert "Hello from JSON" in result
    assert "[image]" in result


# ─── _call_llm_non_streaming ───

def test_call_llm_success(app):
    with app.app_context():
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "choices": [{"message": {"content": "Summary text"}}]
        }
        with patch("app.workspace.summaries.requests.post", return_value=mock_resp):
            result = _call_llm_non_streaming([{"role": "user", "content": "test"}])
        assert result == "Summary text"


def test_call_llm_http_error(app):
    with app.app_context():
        mock_resp = MagicMock()
        mock_resp.status_code = 500
        mock_resp.text = "Server error"
        with patch("app.workspace.summaries.requests.post", return_value=mock_resp):
            result = _call_llm_non_streaming([{"role": "user", "content": "test"}])
        assert result is None


def test_call_llm_no_choices(app):
    with app.app_context():
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"choices": []}
        with patch("app.workspace.summaries.requests.post", return_value=mock_resp):
            result = _call_llm_non_streaming([{"role": "user", "content": "test"}])
        assert result is None


def test_call_llm_connection_error(app):
    with app.app_context():
        with patch("app.workspace.summaries.requests.post", side_effect=requests.ConnectionError("refused")):
            result = _call_llm_non_streaming([{"role": "user", "content": "test"}])
        assert result is None


def test_call_llm_empty_content(app):
    with app.app_context():
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "choices": [{"message": {"content": ""}}]
        }
        with patch("app.workspace.summaries.requests.post", return_value=mock_resp):
            result = _call_llm_non_streaming([{"role": "user", "content": "test"}])
        assert result is None


# ─── generate_thread_summary ───

def test_generate_summary_success(app, db, make_user):
    user = make_user()
    thread = _thread(db, user)
    _msg(db, thread, "user", "What is AI?", 0)
    _msg(db, thread, "assistant", "AI is...", 1)
    with patch("app.workspace.summaries._call_llm_non_streaming", return_value="AI discussion summary"):
        result = generate_thread_summary(thread)
    assert result == "AI discussion summary"
    assert thread.summary == "AI discussion summary"


def test_generate_summary_no_messages(app, db, make_user):
    user = make_user()
    thread = _thread(db, user)
    result = generate_thread_summary(thread)
    assert result is None


def test_generate_summary_llm_failure(app, db, make_user):
    user = make_user()
    thread = _thread(db, user)
    _msg(db, thread, "user", "Test", 0)
    with patch("app.workspace.summaries._call_llm_non_streaming", return_value=None):
        result = generate_thread_summary(thread)
    assert result is None
    assert thread.summary is None


def test_generate_summary_preserves_existing_on_no_messages(app, db, make_user):
    user = make_user()
    thread = _thread(db, user)
    thread.summary = "Old summary"
    db.session.commit()
    result = generate_thread_summary(thread)
    assert result == "Old summary"


# ─── get_or_generate_summary ───

def test_get_or_generate_existing(app, db, make_user):
    user = make_user()
    thread = _thread(db, user)
    thread.summary = "Existing summary"
    db.session.commit()
    result = get_or_generate_summary(thread)
    assert result == "Existing summary"


def test_get_or_generate_creates_new(app, db, make_user):
    user = make_user()
    thread = _thread(db, user)
    _msg(db, thread, "user", "Hello", 0)
    _msg(db, thread, "assistant", "Hi", 1)
    with patch("app.workspace.summaries._call_llm_non_streaming", return_value="Generated"):
        result = get_or_generate_summary(thread)
    assert result == "Generated"
