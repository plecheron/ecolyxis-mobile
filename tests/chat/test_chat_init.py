"""chat/__init__.py tests: rate limiting, message saving, image handling,
precise mode, remote image saving."""
import json
import base64
import uuid
from unittest.mock import patch, MagicMock
from datetime import datetime, timezone, timedelta
from app.models import Thread, Message, User
from app.chat import (
    check_rate_limit, save_user_message, _ensure_upload_dir,
    _run_precise, _save_remote_image, UPLOAD_FOLDER, MAX_IMAGE_SIZE,
    ALLOWED_EXTENSIONS, get_client,
)


def _thread(db, user, title="Test"):
    t = Thread(id=str(uuid.uuid4()), user_id=user.id, title=title)
    db.session.add(t)
    db.session.commit()
    return t


# ─── check_rate_limit ───

def test_rate_limit_premium_unlimited(app, db, make_user):
    """Premium users bypass rate limiting."""
    with app.test_request_context():
        user = make_user()
        user.tier = "premium"
        user.subscription_status = "active"
        db.session.commit()
        from flask_login import login_user
        login_user(user)
        allowed, used, limit = check_rate_limit()
        assert allowed is True
        assert used == 0
        assert limit is None


def test_rate_limit_free_under_limit(app, db, make_user):
    with app.test_request_context():
        user = make_user()
        from flask_login import login_user
        login_user(user)
        allowed, used, limit = check_rate_limit()
        assert allowed is True
        assert isinstance(used, int)
        assert limit is not None


def test_rate_limit_free_over_limit(app, db, make_user):
    with app.test_request_context():
        user = make_user()
        from flask_login import login_user
        login_user(user)
        # Create enough messages to hit limit
        thread = _thread(db, user)
        for i in range(10):
            msg = Message(thread_id=thread.id, role="user", content=f"msg {i}")
            db.session.add(msg)
        db.session.commit()
        allowed, used, limit = check_rate_limit()
        assert allowed is False


# ─── save_user_message ───

def test_save_text_message(app, db, make_user):
    with app.test_request_context():
        user = make_user()
        from flask_login import login_user
        login_user(user)
        thread = _thread(db, user, title="New Chat")
        msg = save_user_message(thread, "Hello world", images=None)
        assert msg.role == "user"
        assert msg.content == "Hello world"
        assert msg.message_type == "text"


def test_save_image_message(app, db, make_user):
    with app.test_request_context():
        user = make_user()
        from flask_login import login_user
        login_user(user)
        thread = _thread(db, user)
        # Small base64 PNG
        img_b64 = base64.b64encode(b"\x89PNG\r\n\x1a\n" + b"\x00" * 100).decode()
        img_data_url = f"data:image/png;base64,{img_b64}"
        msg = save_user_message(thread, "", images=[img_data_url])
        assert msg.role == "user"
        assert msg.message_type == "image"
        parsed = json.loads(msg.content)
        assert parsed[0]["type"] == "image"
        assert "file" in parsed[0]


def test_save_mixed_message(app, db, make_user):
    with app.test_request_context():
        user = make_user()
        from flask_login import login_user
        login_user(user)
        thread = _thread(db, user)
        img_b64 = base64.b64encode(b"\x89PNG" + b"\x00" * 50).decode()
        img_data_url = f"data:image/png;base64,{img_b64}"
        msg = save_user_message(thread, "Describe this", images=[img_data_url])
        assert msg.message_type == "mixed"


def test_save_message_updates_title(app, db, make_user):
    with app.test_request_context():
        user = make_user()
        from flask_login import login_user
        login_user(user)
        thread = _thread(db, user, title="New Chat")
        save_user_message(thread, "What is quantum computing?", images=None)
        db.session.refresh(thread)
        # Title should be auto-updated
        assert thread.title != "New Chat"


# ─── _save_remote_image ───

def test_save_remote_image_success(app, db, make_user):
    with app.test_request_context():
        user = make_user()
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.content = b"\x89PNG fake image data"
        with patch("app.chat.req_lib.get", return_value=mock_resp):
            filename, path = _save_remote_image("http://example.com/img.png")
        assert filename.endswith(".png")
        import os
        assert os.path.exists(path)
        os.remove(path)


def test_save_remote_image_failure(app, db, make_user):
    with app.test_request_context():
        mock_resp = MagicMock()
        mock_resp.status_code = 404
        with patch("app.chat.req_lib.get", return_value=mock_resp):
            try:
                _save_remote_image("http://example.com/missing.png")
                assert False, "Should have raised"
            except RuntimeError as e:
                assert "404" in str(e)


# ─── _run_precise ───

def test_run_precise_success(app, db, make_user):
    """Precise mode runs plan -> generate -> refine and returns final text."""
    with app.test_request_context():
        user = make_user()
        client = MagicMock()
        # Mock: each _call consumes stream_chat, return text chunks
        def mock_stream(msgs, mode=None):
            yield "Plan: step 1"
            yield {"prompt_tokens": 10, "completion_tokens": 5}
        client.stream_chat = MagicMock(side_effect=[
            iter(["Plan: step 1\nstep 2", {"prompt_tokens": 10, "completion_tokens": 5}]),
            iter(["Draft response here", {"prompt_tokens": 20, "completion_tokens": 15}]),
            iter(["Refined response", {"prompt_tokens": 30, "completion_tokens": 10}]),
        ])
        msgs = [
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "test question"},
        ]
        result, pt, ct = _run_precise(client, msgs, "precise")
        assert isinstance(result, str)
        assert pt > 0
        assert ct > 0


# ─── get_client ───

def test_get_client(app):
    with app.test_request_context():
        client = get_client()
        assert client is not None


# ─── _ensure_upload_dir ───

def test_ensure_upload_dir(app):
    with app.test_request_context():
        _ensure_upload_dir()
        import os
        assert os.path.isdir(UPLOAD_FOLDER)


# ─── constants ───

def test_constants():
    assert MAX_IMAGE_SIZE == 20 * 1024 * 1024
    assert "png" in ALLOWED_EXTENSIONS
    assert "jpg" in ALLOWED_EXTENSIONS
