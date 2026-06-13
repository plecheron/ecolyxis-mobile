"""TTS routes tests: endpoint, text extraction, markdown stripping."""
import json
from unittest.mock import patch, MagicMock
from datetime import datetime, timezone, timedelta
import requests as req_lib
from flask import current_app
from app.models import Thread, Message
from app.chat.tts import _extract_text_from_message, _strip_markdown


def _msg(db, thread, role, content, secs=0):
    m = Message(
        thread_id=thread.id, role=role, content=content,
        created_at=datetime(2026, 1, 1, tzinfo=timezone.utc) + timedelta(seconds=secs),
    )
    db.session.add(m)
    db.session.commit()
    return m


# ─── _extract_text_from_message ───

def test_extract_text_plain_string(app, db, make_user):
    user = make_user()
    import uuid
    thread = Thread(id=str(uuid.uuid4()), user_id=user.id, title="T")
    db.session.add(thread)
    db.session.commit()
    msg = _msg(db, thread, "assistant", "Hello world")
    assert _extract_text_from_message(msg) == "Hello world"


def test_extract_text_json_blocks(app, db, make_user):
    user = make_user()
    import uuid
    thread = Thread(id=str(uuid.uuid4()), user_id=user.id, title="T")
    db.session.add(thread)
    db.session.commit()
    content = json.dumps([
        {"type": "text", "text": "Hello"},
        {"type": "text", "text": "world"},
    ])
    msg = _msg(db, thread, "assistant", content)
    assert _extract_text_from_message(msg) == "Hello world"


def test_extract_text_empty(app, db, make_user):
    user = make_user()
    import uuid
    thread = Thread(id=str(uuid.uuid4()), user_id=user.id, title="T")
    db.session.add(thread)
    db.session.commit()
    msg = _msg(db, thread, "assistant", "")
    assert _extract_text_from_message(msg) == ""


# ─── _strip_markdown ───

def test_strip_markdown_bold():
    assert _strip_markdown("**bold** text") == "bold text"


def test_strip_markdown_code_block():
    result = _strip_markdown("before ```code here``` after")
    assert "code here" not in result
    assert "before" in result
    assert "after" in result


def test_strip_markdown_inline_code():
    result = _strip_markdown("use `print()` now")
    assert "print()" not in result
    assert "use" in result
    assert "now" in result


def test_strip_markdown_image():
    result = _strip_markdown("look ![alt](http://img.png) done")
    assert "http://img.png" not in result
    assert "done" in result


def test_strip_markdown_heading():
    result = _strip_markdown("## Heading\ntext")
    assert "Heading" in result


def test_strip_markdown_plain_text():
    assert _strip_markdown("just plain text") == "just plain text"


# ─── TTS endpoint ───

def test_tts_requires_login(client):
    resp = client.post("/chat/fake-id/tts", json={"text": "hello"})
    assert resp.status_code in (301, 302)


def test_tts_thread_not_found(app, db, make_user, login_as, client):
    user = make_user()
    login_as(user)
    resp = client.post(f"/chat/{999}/tts", json={"text": "hello"})
    assert resp.status_code == 404


def test_tts_no_text(app, db, make_user, login_as, client):
    import uuid
    user = make_user()
    thread = Thread(id=str(uuid.uuid4()), user_id=user.id, title="T")
    db.session.add(thread)
    db.session.commit()
    login_as(user)
    resp = client.post(f"/chat/{thread.id}/tts", json={})
    assert resp.status_code == 400
    assert b"No text to speak" in resp.data


def test_tts_service_not_configured(app, db, make_user, login_as, client):
    import uuid
    user = make_user()
    thread = Thread(id=str(uuid.uuid4()), user_id=user.id, title="T")
    db.session.add(thread)
    db.session.commit()
    login_as(user)
    with patch.object(current_app, "config", {**current_app.config, "TTS_URL": None}):
        resp = client.post(f"/chat/{thread.id}/tts", json={"text": "hello"})
    assert resp.status_code == 503


def test_tts_success(app, db, make_user, login_as, client):
    import uuid
    user = make_user()
    thread = Thread(id=str(uuid.uuid4()), user_id=user.id, title="T")
    db.session.add(thread)
    db.session.commit()
    login_as(user)
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.content = b"FAKE_WAV_AUDIO"
    mock_resp.headers = {"X-Audio-Duration": "2.5"}
    with patch("app.chat.tts.req_lib.post", return_value=mock_resp):
        resp = client.post(f"/chat/{thread.id}/tts", json={"text": "hello world"})
    assert resp.status_code == 200
    assert resp.mimetype == "audio/wav"
    assert resp.data == b"FAKE_WAV_AUDIO"


def test_tts_timeout(app, db, make_user, login_as, client):
    import uuid
    user = make_user()
    thread = Thread(id=str(uuid.uuid4()), user_id=user.id, title="T")
    db.session.add(thread)
    db.session.commit()
    login_as(user)
    with patch("app.chat.tts.req_lib.post", side_effect=req_lib.exceptions.Timeout("timed out")):
        resp = client.post(f"/chat/{thread.id}/tts", json={"text": "hello"})
    assert resp.status_code == 504


def test_tts_connection_error(app, db, make_user, login_as, client):
    import uuid
    user = make_user()
    thread = Thread(id=str(uuid.uuid4()), user_id=user.id, title="T")
    db.session.add(thread)
    db.session.commit()
    login_as(user)
    with patch("app.chat.tts.req_lib.post", side_effect=req_lib.exceptions.ConnectionError("refused")):
        resp = client.post(f"/chat/{thread.id}/tts", json={"text": "hello"})
    assert resp.status_code == 503


def test_tts_backend_error(app, db, make_user, login_as, client):
    import uuid
    user = make_user()
    thread = Thread(id=str(uuid.uuid4()), user_id=user.id, title="T")
    db.session.add(thread)
    db.session.commit()
    login_as(user)
    mock_resp = MagicMock()
    mock_resp.status_code = 500
    mock_resp.text = "Internal error"
    with patch("app.chat.tts.req_lib.post", return_value=mock_resp):
        resp = client.post(f"/chat/{thread.id}/tts", json={"text": "hello"})
    assert resp.status_code == 502


def test_tts_from_message_id(app, db, make_user, login_as, client):
    import uuid
    user = make_user()
    thread = Thread(id=str(uuid.uuid4()), user_id=user.id, title="T")
    db.session.add(thread)
    db.session.commit()
    msg = _msg(db, thread, "assistant", "Spoken text from message")
    login_as(user)
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.content = b"WAV"
    mock_resp.headers = {}
    with patch("app.chat.tts.req_lib.post", return_value=mock_resp):
        resp = client.post(f"/chat/{thread.id}/tts", json={"message_id": msg.id})
    assert resp.status_code == 200


def test_tts_long_text_truncated(app, db, make_user, login_as, client):
    import uuid
    user = make_user()
    thread = Thread(id=str(uuid.uuid4()), user_id=user.id, title="T")
    db.session.add(thread)
    db.session.commit()
    login_as(user)
    long_text = " ".join(["word"] * 200)
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.content = b"WAV"
    mock_resp.headers = {}
    with patch("app.chat.tts.req_lib.post", return_value=mock_resp) as mock_post:
        resp = client.post(f"/chat/{thread.id}/tts", json={"text": long_text})
        sent_text = mock_post.call_args[1]["json"]["text"]
        assert len(sent_text) <= 503
    assert resp.status_code == 200
