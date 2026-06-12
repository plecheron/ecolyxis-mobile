"""Media job handlers: image / upscale / edit / video, with Redis faked and the
remote ecolyxis-api stubbed at the stream_remote_job seam. Verifies persistence
(idempotent, keyed by job_id), progress + done events, and the submit endpoints."""
import io
import json
import uuid

import fakeredis
import pytest

from app.models import Thread, Message, GeneratedImage, GeneratedVideo, GenerationJob


@pytest.fixture
def fake_redis():
    import app.redis_client as rc
    fake = fakeredis.FakeRedis(decode_responses=True)
    prev = rc._client
    rc._client = fake
    yield fake
    rc._client = prev


class FakeResp:
    def __init__(self, lines=None, json_data=None, content=b"", status=200, headers=None):
        self.status_code = status
        self._lines = lines or []
        self._json = json_data
        self.content = content
        self.headers = headers or {}
        self.text = ""

    def iter_lines(self, decode_unicode=False):
        for ln in self._lines:
            yield ln

    def json(self):
        return self._json


class FakeRequests:
    """Routes post/get by URL substring to preconfigured responses."""
    def __init__(self, routes):
        self.routes = routes  # list of (substr, FakeResp)

    def _match(self, url):
        for sub, resp in self.routes:
            if sub in url:
                return resp
        raise AssertionError(f"unexpected request URL: {url}")

    def post(self, url, **kw):
        return self._match(url)

    def get(self, url, **kw):
        return self._match(url)


def _stub_remote(monkeypatch, events, result):
    """Stub ecolyxis-api: publish the given events, then return the result dict."""
    import app.jobs.api_client as api_client

    def fake_stream_remote_job(kind, params, publish, *, client_ref=None):
        for ev in events:
            publish(ev)
        return dict(result)

    monkeypatch.setattr(api_client, "stream_remote_job", fake_stream_remote_job)


def _thread(db, user):
    t = Thread(id=str(uuid.uuid4()), user_id=user.id, title="Test")
    db.session.add(t)
    db.session.commit()
    return t


def _make_job(db, user, thread, kind, params):
    job = GenerationJob(user_id=user.id, thread_id=thread.id, kind=kind,
                        status="queued", is_premium=False, params=params)
    db.session.add(job)
    db.session.commit()
    return job


# --- image ----------------------------------------------------------------

def test_run_image_persists(app, db, make_user, fake_redis, monkeypatch):
    import app.chat.images as images
    from app.jobs.worker import run_job
    from app.jobs import read_events

    user = make_user()
    thread = _thread(db, user)
    job = _make_job(db, user, thread, "image", {"prompt": "a cat", "width": 512, "height": 512})
    job_id, tid = job.id, thread.id

    _stub_remote(monkeypatch,
                 events=[{"type": "progress", "stage": "diffusion", "step": 1, "total_steps": 9}],
                 result={"url": "http://api.invalid/outputs/remote.png",
                         "seed": 123, "width": 512, "height": 512})
    monkeypatch.setattr(images, "_save_remote_image", lambda url: ("local_cat.png", "/x"))

    run_job(app, "w-img", job_id)

    types = [e["type"] for _, e in read_events(job_id, last_id="0", block_ms=50)]
    assert "progress" in types and types[-1] == "done"

    with app.app_context():
        img = GeneratedImage.query.filter_by(job_id=job_id).one()
        assert img.filename == "local_cat.png" and img.seed == 123 and img.width == 512
        msg = Message.query.filter_by(job_id=job_id, role="assistant").one()
        assert msg.message_type == "mixed" and "local_cat.png" in msg.content
        assert img.message_id == msg.id
        assert db.session.get(GenerationJob, job_id).status == "done"


def test_image_idempotent(app, db, make_user, fake_redis, monkeypatch):
    import app.chat.images as images
    from app.jobs.worker import run_job

    user = make_user()
    thread = _thread(db, user)
    job = _make_job(db, user, thread, "image", {"prompt": "x", "width": 512, "height": 512})
    job_id, tid = job.id, thread.id

    _stub_remote(monkeypatch,
                 events=[],
                 result={"url": "http://api.invalid/outputs/r.png",
                         "seed": 1, "width": 512, "height": 512})
    monkeypatch.setattr(images, "_save_remote_image", lambda url: ("once.png", "/x"))

    run_job(app, "w1", job_id)
    run_job(app, "w1", job_id)  # terminal -> no-op

    with app.app_context():
        assert GeneratedImage.query.filter_by(thread_id=tid).count() == 1
        assert Message.query.filter_by(thread_id=tid, role="assistant").count() == 1


# --- edit -----------------------------------------------------------------

def test_run_edit_persists(app, db, make_user, fake_redis, monkeypatch):
    import app.chat.images as images
    from app.jobs.worker import run_job

    user = make_user()
    thread = _thread(db, user)
    job = _make_job(db, user, thread, "edit",
                    {"prompt": "make it blue", "image": "data:...", "size": 512})
    job_id, tid = job.id, thread.id

    _stub_remote(monkeypatch,
                 events=[],
                 result={"url": "http://api.invalid/outputs/edited.png", "seed": 9})
    monkeypatch.setattr(images, "_save_remote_image", lambda url: ("local_edit.png", "/x"))

    run_job(app, "w-edit", job_id)

    with app.app_context():
        img = GeneratedImage.query.filter_by(job_id=job_id).one()
        assert img.filename == "local_edit.png" and img.prompt == "make it blue"
        assert Message.query.filter_by(job_id=job_id).count() == 1
        assert db.session.get(GenerationJob, job_id).status == "done"


# --- video ----------------------------------------------------------------

def test_run_video_persists(app, db, make_user, fake_redis, monkeypatch, tmp_path):
    import app.jobs.handlers.media as media
    import app.chat as chatmod
    from app.jobs.worker import run_job
    from app.jobs import read_events

    monkeypatch.setattr(chatmod, "UPLOAD_FOLDER", str(tmp_path))

    user = make_user()
    thread = _thread(db, user)
    job = _make_job(db, user, thread, "video", {"prompt": "a wave", "width": 480, "height": 480, "frames": 33})
    job_id, tid = job.id, thread.id

    _stub_remote(monkeypatch,
                 events=[{"type": "progress", "stage": "sampling", "step": 2}],
                 result={"url": "http://api.invalid/outputs/remote.mp4",
                         "seed": 7, "fps": 16, "elapsed_s": 12})
    # _fetch_artifact downloads the finished .mp4 with media.requests
    monkeypatch.setattr(media, "requests", FakeRequests([
        ("/outputs/", FakeResp(content=b"MP4BYTES", status=200)),
    ]))

    run_job(app, "w-vid", job_id)

    types = [e["type"] for _, e in read_events(job_id, last_id="0", block_ms=50)]
    assert "progress" in types and types[-1] == "done"
    with app.app_context():
        vid = GeneratedVideo.query.filter_by(job_id=job_id).one()
        assert vid.filename.endswith(".mp4") and vid.fps == 16 and vid.frames == 33
        # video persists as a GeneratedVideo (no assistant Message, matching legacy)
        assert Message.query.filter_by(thread_id=tid, role="assistant").count() == 0
        assert db.session.get(GenerationJob, job_id).status == "done"
    # the .mp4 was written into the patched upload dir
    assert any(p.suffix == ".mp4" for p in tmp_path.iterdir())


# --- submit endpoints -----------------------------------------------------

def test_submit_image_enqueues(app, db, make_user, login_as, client, fake_redis):
    user = make_user()
    thread = _thread(db, user)
    login_as(user)
    r = client.post(f"/jobs/image/{thread.id}", json={"prompt": "sunset", "width": 512, "height": 512})
    assert r.status_code == 202
    job_id = r.get_json()["job_id"]
    assert fake_redis.lrange("jobs:queue:free", 0, -1) == [job_id]
    with app.app_context():
        job = db.session.get(GenerationJob, job_id)
        assert job.kind == "image" and job.params["prompt"] == "sunset"


def test_submit_upscale_resolves_next_size(app, db, make_user, login_as, client, fake_redis):
    user = make_user()
    thread = _thread(db, user)
    img = GeneratedImage(user_id=user.id, thread_id=thread.id, prompt="p", seed=5,
                         width=128, height=128, filename="f.png")
    db.session.add(img)
    db.session.commit()
    image_id = img.id
    login_as(user)
    r = client.post(f"/jobs/upscale/{thread.id}", json={"image_id": image_id})
    assert r.status_code == 202
    with app.app_context():
        job = db.session.get(GenerationJob, r.get_json()["job_id"])
        assert job.kind == "upscale" and job.params["next_size"] == 256
        assert job.params["parent_image_id"] == image_id and job.params["seed"] == 5


def test_submit_image_empty_prompt_rejected(app, db, make_user, login_as, client, fake_redis):
    user = make_user()
    thread = _thread(db, user)
    login_as(user)
    assert client.post(f"/jobs/image/{thread.id}", json={"prompt": "  "}).status_code == 400


def test_submit_image_bad_params_rejected(app, db, make_user, login_as, client, fake_redis):
    """#93: malformed numeric params return a clean 400, not a 500."""
    user = make_user(email="badparam@example.com")
    thread = _thread(db, user)
    login_as(user)
    r = client.post(f"/jobs/image/{thread.id}", json={"prompt": "sunset", "width": "banana"})
    assert r.status_code == 400
    assert "width" in r.get_json()["error"]


def test_free_tier_generation_quota(app, db, make_user, login_as, client, fake_redis):
    """#91: media jobs count against a generation quota; upscale included."""
    user = make_user(email="quota@example.com")
    thread = _thread(db, user)
    limit = app.config["RATE_LIMIT_GENERATIONS"]
    for _ in range(limit):
        _make_job(db, user, thread, "image", {"prompt": "p"})
    login_as(user)

    r = client.post(f"/jobs/image/{thread.id}", json={"prompt": "one more"})
    assert r.status_code == 429
    assert r.get_json()["error"] == "rate_limited"

    img = GeneratedImage(user_id=user.id, thread_id=thread.id, prompt="p", seed=1,
                         width=128, height=128, filename="q.png")
    db.session.add(img)
    db.session.commit()
    r = client.post(f"/jobs/upscale/{thread.id}", json={"image_id": img.id})
    assert r.status_code == 429


def test_premium_user_not_generation_limited(app, db, make_user, login_as, client, fake_redis):
    user = make_user(email="quotaprem@example.com", tier="premium",
                     subscription_status="active")
    thread = _thread(db, user)
    for _ in range(app.config["RATE_LIMIT_GENERATIONS"] + 1):
        _make_job(db, user, thread, "image", {"prompt": "p"})
    login_as(user)
    r = client.post(f"/jobs/image/{thread.id}", json={"prompt": "more"})
    assert r.status_code == 202
