"""Video routes tests: admin gate, rate limit, empty prompt, not configured, animate."""
import json
import pytest
from unittest.mock import patch, MagicMock
from app.models import Thread

pytestmark = pytest.mark.skip(reason="Video routes disabled — Wan2.2 backend non-functional (#118)")


def _thread(db, user, title="Vid"):
    import uuid
    t = Thread(id=str(uuid.uuid4()), user_id=user.id, title=title)
    db.session.add(t)
    db.session.commit()
    return t


def _make_admin(db, user):
    user.is_admin = True
    db.session.commit()


# ─── generate_video_stream ───

def test_video_requires_login(client):
    resp = client.post("/chat/fake-id/generate-video-stream", json={"prompt": "cat"})
    assert resp.status_code in (301, 302)


def test_video_non_admin_forbidden(app, db, make_user, login_as, client):
    user = make_user()
    thread = _thread(db, user)
    login_as(user)
    resp = client.post(f"/chat/{thread.id}/generate-video-stream", json={"prompt": "cat"})
    assert resp.status_code == 200
    data = resp.get_data(as_text=True)
    assert "forbidden" in data


def test_video_rate_limited(app, db, make_user, login_as, client):
    user = make_user()
    thread = _thread(db, user)
    _make_admin(db, user)
    login_as(user)
    with patch("app.chat.video.check_rate_limit", return_value=(False, 10, 10)):
        resp = client.post(f"/chat/{thread.id}/generate-video-stream", json={"prompt": "cat"})
    assert resp.status_code == 200
    data = resp.get_data(as_text=True)
    assert "rate_limited" in data


def test_video_empty_prompt(app, db, make_user, login_as, client):
    user = make_user()
    thread = _thread(db, user)
    _make_admin(db, user)
    login_as(user)
    with patch("app.chat.video.check_rate_limit", return_value=(True, 0, 10)):
        resp = client.post(f"/chat/{thread.id}/generate-video-stream", json={})
    assert resp.status_code == 200
    data = resp.get_data(as_text=True)
    assert "Empty prompt" in data


def test_video_wan22_unreachable(app, db, make_user, login_as, client):
    """When WAN22_URL is set but unreachable, the SSE stream reports the error."""
    user = make_user()
    thread = _thread(db, user)
    _make_admin(db, user)
    login_as(user)
    with patch("app.chat.video.check_rate_limit", return_value=(True, 0, 10)):
        resp = client.post(f"/chat/{thread.id}/generate-video-stream", json={"prompt": "cat"})
    assert resp.status_code == 200
    data = resp.get_data(as_text=True)
    # Either "not configured" or "unavailable" depending on env config
    assert "error" in data


def test_video_not_owner(app, db, make_user, login_as, client):
    user1 = make_user()
    user2 = make_user(username="other", email="other@test.com")
    thread = _thread(db, user1)
    login_as(user2)
    resp = client.post(f"/chat/{thread.id}/generate-video-stream", json={"prompt": "cat"})
    assert resp.status_code == 404


# ─── animate_image ───

def test_animate_non_admin_forbidden(app, db, make_user, login_as, client):
    user = make_user()
    thread = _thread(db, user)
    login_as(user)
    resp = client.post(f"/chat/{thread.id}/animate-image", json={"prompt": "wave", "image_url": "/uploads/x.png"})
    assert resp.status_code == 200
    data = resp.get_data(as_text=True)
    assert "forbidden" in data


def test_animate_missing_fields(app, db, make_user, login_as, client):
    user = make_user()
    thread = _thread(db, user)
    _make_admin(db, user)
    login_as(user)
    with patch("app.chat.video.check_rate_limit", return_value=(True, 0, 10)):
        resp = client.post(f"/chat/{thread.id}/animate-image", json={"prompt": "wave"})
    assert resp.status_code == 400


def test_animate_image_not_found(app, db, make_user, login_as, client):
    user = make_user()
    thread = _thread(db, user)
    _make_admin(db, user)
    login_as(user)
    with patch("app.chat.video.check_rate_limit", return_value=(True, 0, 10)):
        resp = client.post(f"/chat/{thread.id}/animate-image", json={
            "prompt": "wave", "image_url": "/uploads/nonexistent.png",
        })
    assert resp.status_code == 404
