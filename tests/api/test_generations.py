"""Tests for image generation, upscaling, and video generation endpoints."""
import io
import json
import os
import tempfile
from unittest.mock import patch, MagicMock

import pytest

from app.models import User, Thread, Message, GeneratedImage, GeneratedVideo


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def upload_dir():
    """Create a temp upload dir and patch UPLOAD_FOLDER + _ensure_upload_dir."""
    with tempfile.TemporaryDirectory() as tmpdir:
        uploads = os.path.join(tmpdir, "uploads")
        os.makedirs(uploads)
        with patch("app.chat.UPLOAD_FOLDER", uploads), \
             patch("app.chat.images.UPLOAD_FOLDER", uploads), \
             patch("app.chat.video.UPLOAD_FOLDER", uploads), \
             patch("app.chat._ensure_upload_dir"), \
             patch("app.chat.images._ensure_upload_dir"), \
             patch("app.chat.video._ensure_upload_dir"):
            yield uploads


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _setup_user_with_thread(db, make_user, login_as):
    user = make_user()
    login_as(user)
    thread = Thread(user_id=user.id, title="Test Thread")
    db.session.add(thread)
    db.session.commit()
    return user, thread


def _mock_generate_response(filename="test_img.png", seed=42, width=128, height=128):
    """Mock 200 response from image backend /generate."""
    resp = MagicMock()
    resp.status_code = 200
    resp.json.return_value = {
        "filename": filename,
        "seed": seed,
        "width": width,
        "height": height,
    }
    return resp


def _mock_fetch_response(data=b"\x89PNG\r\n\x1a\n" + b"\x00" * 100):
    """Mock 200 response for fetching a generated file."""
    resp = MagicMock()
    resp.status_code = 200
    resp.content = data
    return resp


def _mock_sse_resp(events):
    """Mock streaming response yielding SSE lines."""
    lines = []
    for ev in events:
        lines.append(f"data: {json.dumps(ev)}")
    resp = MagicMock()
    resp.status_code = 200
    resp.iter_lines.return_value = iter(lines)
    return resp


# ---------------------------------------------------------------------------
# Image generation — synchronous endpoint
# ---------------------------------------------------------------------------

class TestGenerateImageSync:

    def test_success(self, client, db, make_user, login_as, upload_dir):
        user, thread = _setup_user_with_thread(db, make_user, login_as)

        with patch("app.chat.images.req_lib.post", return_value=_mock_generate_response()), \
             patch("app.chat.images.req_lib.get", return_value=_mock_fetch_response()):

            resp = client.post(
                f"/chat/{thread.id}/generate-image",
                json={"prompt": "a green forest", "width": 128, "height": 128},
            )

        assert resp.status_code == 200
        data = resp.get_json()
        assert data["url"].startswith("/uploads/")
        assert data["seed"] == 42
        assert data["size"] == [128, 128]
        assert "image_id" in data

        img = GeneratedImage.query.filter_by(thread_id=thread.id).first()
        assert img is not None
        assert img.prompt == "a green forest"
        assert img.seed == 42
        assert img.user_id == user.id

    def test_empty_prompt(self, client, db, make_user, login_as):
        _, thread = _setup_user_with_thread(db, make_user, login_as)
        resp = client.post(
            f"/chat/{thread.id}/generate-image",
            json={"prompt": "   "},
        )
        assert resp.status_code == 400
        assert "Empty prompt" in resp.get_json()["error"]

    def test_rate_limited(self, client, db, make_user, login_as):
        user, thread = _setup_user_with_thread(db, make_user, login_as)
        for i in range(5):
            db.session.add(Message(thread_id=thread.id, role="user", content=f"msg {i}"))
        db.session.commit()

        resp = client.post(
            f"/chat/{thread.id}/generate-image",
            json={"prompt": "a forest"},
        )
        assert resp.status_code == 429

    def test_backend_unavailable(self, client, db, make_user, login_as):
        _, thread = _setup_user_with_thread(db, make_user, login_as)
        import requests as req_lib

        with patch("app.chat.images.req_lib.post",
                   side_effect=req_lib.ConnectionError("Connection refused")):
            resp = client.post(
                f"/chat/{thread.id}/generate-image",
                json={"prompt": "a forest"},
            )
        assert resp.status_code == 503

    def test_backend_returns_error(self, client, db, make_user, login_as):
        _, thread = _setup_user_with_thread(db, make_user, login_as)
        err = MagicMock(status_code=500, text="Internal Server Error")

        with patch("app.chat.images.req_lib.post", return_value=err):
            resp = client.post(
                f"/chat/{thread.id}/generate-image",
                json={"prompt": "a forest"},
            )
        assert resp.status_code == 502

    def test_no_image_backend_configured(self, client, db, make_user, login_as):
        _, thread = _setup_user_with_thread(db, make_user, login_as)
        with patch("app.chat.images._get_image_url", return_value=None):
            resp = client.post(
                f"/chat/{thread.id}/generate-image",
                json={"prompt": "a forest"},
            )
        assert resp.status_code == 503

    def test_requires_login(self, client, db):
        resp = client.post(
            f"/chat/nonexistent/generate-image",
            json={"prompt": "a forest"},
        )
        assert resp.status_code == 302

    def test_other_users_thread(self, client, db, make_user, login_as):
        _, thread = _setup_user_with_thread(db, make_user, login_as)
        other = make_user(username="other", email="other@test.com")
        other_thread = Thread(user_id=other.id, title="Other Thread")
        db.session.add(other_thread)
        db.session.commit()

        resp = client.post(
            f"/chat/{other_thread.id}/generate-image",
            json={"prompt": "a forest"},
        )
        assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Image generation — streaming endpoint
# ---------------------------------------------------------------------------

class TestGenerateImageStream:

    def test_success(self, client, db, make_user, login_as, upload_dir):
        user, thread = _setup_user_with_thread(db, make_user, login_as)

        sse = _mock_sse_resp([
            {"stage": "progress", "step": 1, "total": 10},
            {"stage": "done", "filename": "test_out.png", "seed": 99,
             "width": 256, "height": 256},
        ])

        with patch("app.chat.images.req_lib.post", return_value=sse), \
             patch("app.chat.images.req_lib.get", return_value=_mock_fetch_response()):
            resp = client.post(
                f"/chat/{thread.id}/generate-image-stream",
                json={"prompt": "a sunset", "width": 256, "height": 256},
            )
            assert resp.status_code == 200
            assert "text/event-stream" in resp.content_type
            # MUST consume the response while patches are active —
            # the SSE generator runs lazily.
            body = resp.get_data(as_text=True)

        assert "progress" in body
        assert "done" in body

        img = GeneratedImage.query.filter_by(thread_id=thread.id).first()
        assert img is not None
        assert img.seed == 99
        assert img.width == 256

    def test_empty_prompt(self, client, db, make_user, login_as):
        _, thread = _setup_user_with_thread(db, make_user, login_as)
        resp = client.post(
            f"/chat/{thread.id}/generate-image-stream",
            json={"prompt": ""},
        )
        body = resp.get_data(as_text=True)
        assert "Empty prompt" in body

    def test_no_backend(self, client, db, make_user, login_as):
        _, thread = _setup_user_with_thread(db, make_user, login_as)
        with patch("app.chat.images._get_image_url", return_value=None):
            resp = client.post(
                f"/chat/{thread.id}/generate-image-stream",
                json={"prompt": "a sunset"},
            )
        body = resp.get_data(as_text=True)
        assert "not configured" in body

    def test_backend_connection_error(self, client, db, make_user, login_as, upload_dir):
        _, thread = _setup_user_with_thread(db, make_user, login_as)
        import requests as req_lib

        with patch("app.chat.images.req_lib.post",
                   side_effect=req_lib.ConnectionError("Connection refused")):
            resp = client.post(
                f"/chat/{thread.id}/generate-image-stream",
                json={"prompt": "a sunset"},
            )
            body = resp.get_data(as_text=True)

        assert "unavailable" in body.lower()


# ---------------------------------------------------------------------------
# Image upscale
# ---------------------------------------------------------------------------

class TestUpscaleImage:

    def test_upscale_success(self, client, db, make_user, login_as, upload_dir):
        user, thread = _setup_user_with_thread(db, make_user, login_as)

        img = GeneratedImage(
            user_id=user.id, thread_id=thread.id, prompt="a forest",
            seed=42, width=128, height=128, filename="base_img.png",
        )
        db.session.add(img)
        db.session.commit()

        sse = _mock_sse_resp([
            {"stage": "progress", "step": 1, "total": 10},
            {"stage": "done", "filename": "upscaled.png", "seed": 42,
             "width": 256, "height": 256},
        ])

        with patch("app.chat.images.req_lib.post", return_value=sse), \
             patch("app.chat.images.req_lib.get", return_value=_mock_fetch_response()):
            resp = client.post(
                f"/chat/{thread.id}/upscale-image",
                json={"image_id": img.id},
            )
            body = resp.get_data(as_text=True)

        assert resp.status_code == 200
        assert "done" in body

        new_img = GeneratedImage.query.filter(
            GeneratedImage.parent_id == img.id).first()
        assert new_img is not None
        assert new_img.width == 256
        assert new_img.height == 256
        assert new_img.seed == 42

    def test_no_image_id(self, client, db, make_user, login_as):
        _, thread = _setup_user_with_thread(db, make_user, login_as)
        resp = client.post(
            f"/chat/{thread.id}/upscale-image",
            json={},
        )
        assert resp.status_code == 400

    def test_already_max_size(self, client, db, make_user, login_as):
        user, thread = _setup_user_with_thread(db, make_user, login_as)
        # 2048 is the largest entry in GeneratedImage.SIZES, so it has no next size.
        img = GeneratedImage(
            user_id=user.id, thread_id=thread.id, prompt="a forest",
            seed=42, width=2048, height=2048, filename="max_img.png",
        )
        db.session.add(img)
        db.session.commit()

        resp = client.post(
            f"/chat/{thread.id}/upscale-image",
            json={"image_id": img.id},
        )
        assert resp.status_code == 400
        assert "maximum" in resp.get_json()["error"].lower()


# ---------------------------------------------------------------------------
# Video generation — text-to-video
# ---------------------------------------------------------------------------

class TestGenerateVideoStream:

    def test_success(self, client, db, make_user, login_as, upload_dir):
        user, thread = _setup_user_with_thread(db, make_user, login_as)

        sse = _mock_sse_resp([
            {"stage": "progress", "step": 5, "total": 33},
            {"stage": "done", "filename": "test_vid.mp4", "seed": 7,
             "fps": 16, "elapsed_s": 45.2},
        ])

        vid_data = b"\x00\x00\x00 ftypisom" + b"\x00" * 200

        with patch("app.chat.video.req_lib.post", return_value=sse), \
             patch("app.chat.video.req_lib.get", return_value=_mock_fetch_response(vid_data)):
            resp = client.post(
                f"/chat/{thread.id}/generate-video-stream",
                json={"prompt": "a cat walking", "width": 480, "height": 480, "frames": 33},
            )
            assert resp.status_code == 200
            assert "text/event-stream" in resp.content_type
            body = resp.get_data(as_text=True)

        assert "done" in body

        vid = GeneratedVideo.query.filter_by(thread_id=thread.id).first()
        assert vid is not None
        assert vid.prompt == "a cat walking"
        assert vid.frames == 33
        assert vid.duration_s == 45.2

    def test_empty_prompt(self, client, db, make_user, login_as):
        _, thread = _setup_user_with_thread(db, make_user, login_as)
        resp = client.post(
            f"/chat/{thread.id}/generate-video-stream",
            json={"prompt": ""},
        )
        body = resp.get_data(as_text=True)
        assert "Empty prompt" in body

    def test_no_wan22_configured(self, client, db, make_user, login_as):
        _, thread = _setup_user_with_thread(db, make_user, login_as)
        from flask import current_app
        original = current_app.config.get("WAN22_URL")
        current_app.config["WAN22_URL"] = None
        resp = client.post(
            f"/chat/{thread.id}/generate-video-stream",
            json={"prompt": "a cat walking"},
        )
        current_app.config["WAN22_URL"] = original
        body = resp.get_data(as_text=True)
        assert "not configured" in body

    def test_backend_connection_error(self, client, db, make_user, login_as, upload_dir):
        _, thread = _setup_user_with_thread(db, make_user, login_as)
        import requests as req_lib

        with patch("app.chat.video.req_lib.post",
                   side_effect=req_lib.ConnectionError("Connection refused")):
            resp = client.post(
                f"/chat/{thread.id}/generate-video-stream",
                json={"prompt": "a cat walking"},
            )
            body = resp.get_data(as_text=True)

        assert "unavailable" in body.lower()


# ---------------------------------------------------------------------------
# Video generation — image-to-video (animate)
# ---------------------------------------------------------------------------

class TestAnimateImage:

    def test_missing_params(self, client, db, make_user, login_as):
        _, thread = _setup_user_with_thread(db, make_user, login_as)
        resp = client.post(
            f"/chat/{thread.id}/animate-image",
            json={"prompt": "animate this"},
        )
        assert resp.status_code == 400
        assert "Missing" in resp.get_json()["error"]

    def test_image_not_found(self, client, db, make_user, login_as, upload_dir):
        _, thread = _setup_user_with_thread(db, make_user, login_as)
        resp = client.post(
            f"/chat/{thread.id}/animate-image",
            json={"prompt": "animate this", "image_url": "/uploads/nonexistent.png"},
        )
        assert resp.status_code == 404

    def test_success(self, client, db, make_user, login_as, upload_dir):
        user, thread = _setup_user_with_thread(db, make_user, login_as)

        # Create a dummy image in temp upload dir
        test_img = os.path.join(upload_dir, "test_animate_img.png")
        with open(test_img, "wb") as f:
            f.write(b"\x89PNG" + b"\x00" * 50)

        img = GeneratedImage(
            user_id=user.id, thread_id=thread.id, prompt="test",
            seed=1, width=128, height=128, filename="test_animate_img.png",
        )
        db.session.add(img)
        db.session.commit()

        sse = _mock_sse_resp([
            {"stage": "done", "filename": "animated.mp4", "seed": 1,
             "fps": 16, "elapsed_s": 30.0},
        ])
        vid_data = b"\x00\x00\x00 ftypisom" + b"\x00" * 200

        with patch("app.chat.video.req_lib.post", return_value=sse), \
             patch("app.chat.video.req_lib.get", return_value=_mock_fetch_response(vid_data)):
            resp = client.post(
                f"/chat/{thread.id}/animate-image",
                json={"prompt": "make it move",
                      "image_url": "/uploads/test_animate_img.png"},
            )
            assert resp.status_code == 200
            body = resp.get_data(as_text=True)

        assert "done" in body

        vid = GeneratedVideo.query.filter_by(thread_id=thread.id).first()
        assert vid is not None
        assert vid.prompt == "make it move"
        assert vid.parent_image_id == img.id
