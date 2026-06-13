"""Jobs routes tests: chat submit, image/video/edit/animate submit, status, stream."""
import json
import uuid
from datetime import datetime, timezone, timedelta
from unittest.mock import patch, MagicMock
from app.models import Thread, Message, GenerationJob, GeneratedImage


def _thread(db, user, title="Job Thread"):
    t = Thread(id=str(uuid.uuid4()), user_id=user.id, title=title)
    db.session.add(t)
    db.session.commit()
    return t


def _make_premium(db, user):
    user.tier = "premium"
    user.subscription_status = "active"
    db.session.commit()


# ─── submit_chat ───

def test_submit_chat_requires_login(client):
    resp = client.post("/jobs/chat/fake-id", json={"content": "hi"})
    assert resp.status_code in (301, 302)


def test_submit_chat_success(app, db, make_user, login_as, client):
    user = make_user()
    thread = _thread(db, user)
    _make_premium(db, user)
    login_as(user)
    with patch("app.jobs.routes.enqueue") as mock_enq, \
         patch("app.chat.check_rate_limit", return_value=(True, 0, 100)):
        resp = client.post(f"/jobs/chat/{thread.id}", json={"content": "Hello", "mode": "standard"})
    assert resp.status_code == 202
    data = resp.get_json()
    assert data["status"] == "queued"
    assert "job_id" in data
    assert data["stream_url"] == f"/jobs/{data['job_id']}/stream"
    mock_enq.assert_called_once()


def test_submit_chat_empty_message(app, db, make_user, login_as, client):
    user = make_user()
    thread = _thread(db, user)
    _make_premium(db, user)
    login_as(user)
    with patch("app.chat.check_rate_limit", return_value=(True, 0, 100)):
        resp = client.post(f"/jobs/chat/{thread.id}", json={})
    assert resp.status_code == 400
    assert b"Empty message" in resp.data


def test_submit_chat_not_owner(app, db, make_user, login_as, client):
    user1 = make_user()
    user2 = make_user(username="other", email="other@test.com")
    thread = _thread(db, user1)
    login_as(user2)
    resp = client.post(f"/jobs/chat/{thread.id}", json={"content": "hi"})
    assert resp.status_code == 404


# ─── submit_image ───

def test_submit_image_success(app, db, make_user, login_as, client):
    user = make_user()
    thread = _thread(db, user)
    _make_premium(db, user)
    login_as(user)
    with patch("app.jobs.routes.enqueue"):
        resp = client.post(f"/jobs/image/{thread.id}", json={
            "prompt": "a cat", "width": 1024, "height": 1024, "seed": 42,
        })
    assert resp.status_code == 202
    data = resp.get_json()
    assert data["status"] == "queued"
    job = GenerationJob.query.get(data["job_id"])
    assert job.kind == "image"
    assert job.params["prompt"] == "a cat"


def test_submit_image_empty_prompt(app, db, make_user, login_as, client):
    user = make_user()
    thread = _thread(db, user)
    _make_premium(db, user)
    login_as(user)
    resp = client.post(f"/jobs/image/{thread.id}", json={"prompt": ""})
    assert resp.status_code == 400
    assert b"Empty prompt" in resp.data


def test_submit_image_bad_int_param(app, db, make_user, login_as, client):
    user = make_user()
    thread = _thread(db, user)
    _make_premium(db, user)
    login_as(user)
    resp = client.post(f"/jobs/image/{thread.id}", json={
        "prompt": "cat", "width": "not-a-number",
    })
    assert resp.status_code == 400
    assert b"Invalid numeric value" in resp.data


# ─── submit_video ───

def test_submit_video_success(app, db, make_user, login_as, client):
    user = make_user()
    thread = _thread(db, user)
    _make_premium(db, user)
    login_as(user)
    with patch("app.jobs.routes.enqueue"):
        resp = client.post(f"/jobs/video/{thread.id}", json={
            "prompt": "dancing cat", "frames": 33,
        })
    assert resp.status_code == 202
    data = resp.get_json()
    job = GenerationJob.query.get(data["job_id"])
    assert job.kind == "video"


def test_submit_video_empty_prompt(app, db, make_user, login_as, client):
    user = make_user()
    thread = _thread(db, user)
    _make_premium(db, user)
    login_as(user)
    resp = client.post(f"/jobs/video/{thread.id}", json={"prompt": ""})
    assert resp.status_code == 400


# ─── submit_edit ───

def test_submit_edit_success(app, db, make_user, login_as, client):
    user = make_user()
    thread = _thread(db, user)
    _make_premium(db, user)
    login_as(user)
    with patch("app.jobs.routes.enqueue"):
        resp = client.post(f"/jobs/edit/{thread.id}", json={
            "prompt": "make it blue", "image": "base64data",
        })
    assert resp.status_code == 202


def test_submit_edit_no_prompt(app, db, make_user, login_as, client):
    user = make_user()
    thread = _thread(db, user)
    _make_premium(db, user)
    login_as(user)
    resp = client.post(f"/jobs/edit/{thread.id}", json={"image": "data"})
    assert resp.status_code == 400


def test_submit_edit_no_image(app, db, make_user, login_as, client):
    user = make_user()
    thread = _thread(db, user)
    _make_premium(db, user)
    login_as(user)
    resp = client.post(f"/jobs/edit/{thread.id}", json={"prompt": "edit"})
    assert resp.status_code == 400


# ─── submit_animate ───

def test_submit_animate_success(app, db, make_user, login_as, client):
    user = make_user()
    thread = _thread(db, user)
    _make_premium(db, user)
    login_as(user)
    with patch("app.jobs.routes.enqueue"):
        resp = client.post(f"/jobs/animate/{thread.id}", json={
            "prompt": "wave", "image_url": "http://img.png",
        })
    assert resp.status_code == 202


def test_submit_animate_missing_fields(app, db, make_user, login_as, client):
    user = make_user()
    thread = _thread(db, user)
    _make_premium(db, user)
    login_as(user)
    resp = client.post(f"/jobs/animate/{thread.id}", json={"prompt": "wave"})
    assert resp.status_code == 400


# ─── active_jobs ───

def test_active_jobs(app, db, make_user, login_as, client):
    user = make_user()
    thread = _thread(db, user)
    job1 = GenerationJob(user_id=user.id, thread_id=thread.id, kind="image",
                         status="running", is_premium=False, params={})
    job2 = GenerationJob(user_id=user.id, thread_id=thread.id, kind="video",
                         status="queued", is_premium=False, params={})
    job3 = GenerationJob(user_id=user.id, thread_id=thread.id, kind="image",
                         status="done", is_premium=False, params={})
    db.session.add_all([job1, job2, job3])
    db.session.commit()
    login_as(user)
    resp = client.get("/jobs/active")
    assert resp.status_code == 200
    data = resp.get_json()
    assert len(data["jobs"]) == 2  # running + queued, not done


# ─── job_status ───

def test_job_status(app, db, make_user, login_as, client):
    user = make_user()
    thread = _thread(db, user)
    job = GenerationJob(user_id=user.id, thread_id=thread.id, kind="image",
                        status="done", is_premium=False, params={},
                        result={"url": "http://result.png"})
    db.session.add(job)
    db.session.commit()
    login_as(user)
    resp = client.get(f"/jobs/{job.id}")
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["status"] == "done"
    assert data["result"]["url"] == "http://result.png"


def test_job_status_not_owner(app, db, make_user, login_as, client):
    user1 = make_user()
    user2 = make_user(username="other", email="other@test.com")
    thread = _thread(db, user1)
    job = GenerationJob(user_id=user1.id, thread_id=thread.id, kind="image",
                        status="done", is_premium=False, params={})
    db.session.add(job)
    db.session.commit()
    login_as(user2)
    resp = client.get(f"/jobs/{job.id}")
    assert resp.status_code == 404


# ─── generation rate limiting ───

def test_generation_rate_limited_free_tier(app, db, make_user, login_as, client):
    user = make_user()  # free tier
    thread = _thread(db, user)
    login_as(user)
    # Fill quota
    limit = 5  # default from config
    for i in range(limit + 1):
        j = GenerationJob(user_id=user.id, thread_id=thread.id, kind="image",
                          status="done", is_premium=False, params={})
        db.session.add(j)
    db.session.commit()
    resp = client.post(f"/jobs/image/{thread.id}", json={"prompt": "cat"})
    assert resp.status_code == 429
