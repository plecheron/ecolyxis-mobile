"""Durable job path: submit, worker run, resumable streaming, idempotency,
ownership, and crash recovery. Redis is faked so no daemon is needed."""
import json
import uuid

import fakeredis
import pytest

from app.models import Thread, Message, GenerationJob


@pytest.fixture
def fake_redis():
    """Inject a fresh in-memory Redis as the shared client for the test."""
    import app.redis_client as rc
    fake = fakeredis.FakeRedis(decode_responses=True)
    prev = rc._client
    rc._client = fake
    yield fake
    rc._client = prev


class FakeClient:
    """Minimal LLM client: run_chat only needs build_messages on the remote path."""
    def build_messages(self, thread, mode="standard", workspace_context=None):
        return [{"role": "user", "content": "hi"}]


@pytest.fixture
def stub_llm(monkeypatch):
    """Stub the remote ecolyxis-api chat stream with a deterministic token stream."""
    import app.chat as chatmod
    import app.jobs.api_client as api_client

    def fake_stream_remote_job(kind, params, publish, *, client_ref=None):
        assert kind == "chat"
        publish({"type": "thinking_start"})
        publish({"type": "thinking_end"})
        for tok in ["Hello", ", ", "world", "!"]:
            publish({"type": "content", "text": tok})
        return {"text": "Hello, world!", "prompt_tokens": 7,
                "completion_tokens": 4, "reasoning_tokens": 0}

    monkeypatch.setattr(chatmod, "get_client", lambda: FakeClient())
    monkeypatch.setattr(api_client, "stream_remote_job", fake_stream_remote_job)
    return FakeClient


def _thread(db, user):
    t = Thread(id=str(uuid.uuid4()), user_id=user.id, title="Test")
    db.session.add(t)
    db.session.commit()
    return t


# --- submit ---------------------------------------------------------------

def test_submit_creates_job_and_enqueues(app, db, make_user, login_as, client, fake_redis):
    user = make_user()
    thread = _thread(db, user)
    login_as(user)

    resp = client.post(f"/jobs/chat/{thread.id}", json={"content": "hello there"})
    assert resp.status_code == 202
    body = resp.get_json()
    assert body["status"] == "queued"
    assert body["stream_url"] == f"/jobs/{body['job_id']}/stream"

    with app.app_context():
        job = db.session.get(GenerationJob, body["job_id"])
        assert job.kind == "chat" and job.status == "queued"
        # user message persisted
        assert Message.query.filter_by(thread_id=thread.id, role="user").count() == 1

    # enqueued onto the free lane (non-premium user)
    assert fake_redis.lrange("jobs:queue:free", 0, -1) == [body["job_id"]]


def test_submit_empty_rejected(app, db, make_user, login_as, client, fake_redis):
    user = make_user()
    thread = _thread(db, user)
    login_as(user)
    resp = client.post(f"/jobs/chat/{thread.id}", json={"content": "   "})
    assert resp.status_code == 400


def test_submit_premium_uses_premium_lane(app, db, make_user, login_as, client, fake_redis):
    user = make_user(tier="premium", subscription_status="active")
    thread = _thread(db, user)
    login_as(user)
    resp = client.post(f"/jobs/chat/{thread.id}", json={"content": "hi"})
    job_id = resp.get_json()["job_id"]
    assert fake_redis.lrange("jobs:queue:premium", 0, -1) == [job_id]


# --- worker run -----------------------------------------------------------

def test_worker_runs_job_and_persists(app, db, make_user, fake_redis, stub_llm):
    from app.jobs.worker import run_job
    from app.jobs import read_events

    user = make_user()
    thread = _thread(db, user)
    db.session.add(Message(thread_id=thread.id, role="user", content="hi"))
    db.session.commit()
    job = GenerationJob(user_id=user.id, thread_id=thread.id, kind="chat",
                        status="queued", is_premium=False, params={"mode": "standard"})
    db.session.add(job)
    db.session.commit()
    job_id, thread_id = job.id, thread.id

    run_job(app, "w-t0", job_id)

    events = read_events(job_id, last_id="0", block_ms=50)
    types = [e["type"] for _, e in events]
    assert types[0] == "stream_start" and types[-1] == "done"
    assert "thinking_start" in types and "thinking_end" in types
    text = "".join(e["text"] for _, e in events if e["type"] == "content")
    assert text == "Hello, world!"

    with app.app_context():
        msg = Message.query.filter_by(thread_id=thread_id, role="assistant").one()
        assert msg.content == "Hello, world!" and msg.job_id == job_id
        assert db.session.get(GenerationJob, job_id).status == "done"


def test_thinking_progress_emitted_and_persisted(app, db, make_user, fake_redis, monkeypatch):
    """The live thinking-token count is forwarded as events and the final count
    is persisted on the assistant message (text is never stored)."""
    from app.jobs.worker import run_job
    from app.jobs import read_events

    import app.chat as chatmod
    import app.jobs.api_client as api_client

    def thinking_stream(kind, params, publish, *, client_ref=None):
        publish({"type": "thinking_start"})
        publish({"type": "thinking_progress", "tokens": 12})
        publish({"type": "thinking_progress", "tokens": 31})
        publish({"type": "thinking_end", "tokens": 42})
        publish({"type": "content", "text": "Answer"})
        return {"text": "Answer", "prompt_tokens": 9,
                "completion_tokens": 1, "reasoning_tokens": 42}

    monkeypatch.setattr(chatmod, "get_client", lambda: FakeClient())
    monkeypatch.setattr(api_client, "stream_remote_job", thinking_stream)

    user = make_user()
    thread = _thread(db, user)
    db.session.add(Message(thread_id=thread.id, role="user", content="hi"))
    db.session.commit()
    job = GenerationJob(user_id=user.id, thread_id=thread.id, kind="chat",
                        status="queued", is_premium=False, params={"mode": "standard"})
    db.session.add(job)
    db.session.commit()
    job_id, thread_id = job.id, thread.id

    run_job(app, "w-t0", job_id)

    events = [e for _, e in read_events(job_id, last_id="0", block_ms=50)]
    progress = [e["tokens"] for e in events if e["type"] == "thinking_progress"]
    assert progress == [12, 31]  # forwarded, monotonic
    end = [e for e in events if e["type"] == "thinking_end"]
    assert end and end[0]["tokens"] == 42
    done = [e for e in events if e["type"] == "done"][0]
    assert done["reasoning_tokens"] == 42

    with app.app_context():
        msg = Message.query.filter_by(thread_id=thread_id, role="assistant").one()
        assert msg.content == "Answer"
        assert msg.reasoning_tokens == 42


def test_persist_is_idempotent(app, db, make_user, fake_redis, stub_llm):
    from app.jobs.worker import run_job
    user = make_user()
    thread = _thread(db, user)
    db.session.add(Message(thread_id=thread.id, role="user", content="hi"))
    db.session.commit()
    job = GenerationJob(user_id=user.id, thread_id=thread.id, kind="chat",
                        status="queued", is_premium=False, params={})
    db.session.add(job)
    db.session.commit()
    job_id, thread_id = job.id, thread.id

    run_job(app, "w-t0", job_id)
    run_job(app, "w-t0", job_id)  # terminal -> no-op

    with app.app_context():
        assert Message.query.filter_by(thread_id=thread_id, role="assistant").count() == 1


# --- active jobs ----------------------------------------------------------

def test_active_jobs_scoped_and_filtered(app, db, make_user, login_as, client, fake_redis):
    me = make_user(email="me@example.com", username="me")
    other = make_user(email="other@example.com", username="other")
    t1, t2 = _thread(db, me), _thread(db, me)

    running = GenerationJob(user_id=me.id, thread_id=t1.id, kind="chat",
                            status="running", is_premium=False, params={})
    queued = GenerationJob(user_id=me.id, thread_id=t2.id, kind="image",
                           status="queued", is_premium=False, params={})
    done = GenerationJob(user_id=me.id, thread_id=t1.id, kind="chat",
                         status="done", is_premium=False, params={})
    others = GenerationJob(user_id=other.id, thread_id=_thread(db, other).id, kind="chat",
                           status="running", is_premium=False, params={})
    db.session.add_all([running, queued, done, others])
    db.session.commit()
    running_id, queued_id = running.id, queued.id

    login_as(me)
    body = client.get("/jobs/active").get_json()
    ids = {j["job_id"] for j in body["jobs"]}
    # Only my non-terminal jobs — excludes my terminal job and the other user's.
    assert ids == {running_id, queued_id}
    kinds = {j["job_id"]: j["kind"] for j in body["jobs"]}
    assert kinds[queued_id] == "image"


def test_active_jobs_requires_login(app, db, client, fake_redis):
    resp = client.get("/jobs/active")
    assert resp.status_code in (302, 401)


# --- resumable streaming --------------------------------------------------

def test_stream_replays_and_resumes(app, db, make_user, login_as, client, fake_redis):
    from app.jobs import publish_event
    user = make_user()
    thread = _thread(db, user)
    job = GenerationJob(user_id=user.id, thread_id=thread.id, kind="chat",
                        status="running", is_premium=False, params={})
    db.session.add(job)
    db.session.commit()
    job_id = job.id

    publish_event(job_id, {"type": "stream_start"})
    seq2 = publish_event(job_id, {"type": "content", "text": "Hello"})
    publish_event(job_id, {"type": "content", "text": " world"})
    publish_event(job_id, {"type": "done", "message_id": 1})

    login_as(user)

    # full replay from the start
    full = client.get(f"/jobs/{job_id}/stream").get_data(as_text=True)
    assert "Hello" in full and "world" in full and '"type": "done"' in full

    # resume after the 2nd event -> only the tail, no first "Hello"
    tail = client.get(f"/jobs/{job_id}/stream?last_id={seq2}").get_data(as_text=True)
    assert "world" in tail and '"type": "done"' in tail
    assert '"text": "Hello"' not in tail


def test_stream_synthesizes_when_log_expired(app, db, make_user, login_as, client, fake_redis):
    """Job finished and its event log is gone -> stream still emits the outcome."""
    user = make_user()
    thread = _thread(db, user)
    job = GenerationJob(user_id=user.id, thread_id=thread.id, kind="chat",
                        status="done", is_premium=False, result={"message_id": 5})
    db.session.add(job)
    db.session.commit()
    # no events in redis for this job
    login_as(user)
    out = client.get(f"/jobs/{job.id}/stream").get_data(as_text=True)
    assert '"type": "done"' in out and '"message_id": 5' in out


# --- ownership ------------------------------------------------------------

def test_cannot_access_other_users_job(app, db, make_user, login_as, client, fake_redis):
    owner = make_user(email="owner@x.com", username="owner")
    other = make_user(email="other@x.com", username="other")
    thread = _thread(db, owner)
    job = GenerationJob(user_id=owner.id, thread_id=thread.id, kind="chat", status="running")
    db.session.add(job)
    db.session.commit()
    job_id = job.id

    login_as(other)
    assert client.get(f"/jobs/{job_id}").status_code == 404
    assert client.get(f"/jobs/{job_id}/stream").status_code == 404


# --- crash recovery -------------------------------------------------------

def test_reaper_requeues_stranded_job(app, db, make_user, fake_redis):
    from app.jobs import processing_key
    from app.jobs.worker import _requeue_dead
    user = make_user()
    thread = _thread(db, user)
    job = GenerationJob(user_id=user.id, thread_id=thread.id, kind="chat",
                        status="running", is_premium=True, worker_id="dead")
    db.session.add(job)
    db.session.commit()
    job_id = job.id

    # stranded in a dead worker's processing list (no alive key set)
    fake_redis.lpush(processing_key("dead"), job_id)
    _requeue_dead(app)

    assert fake_redis.llen(processing_key("dead")) == 0
    assert fake_redis.lrange("jobs:queue:premium", 0, -1) == [job_id]
    with app.app_context():
        assert db.session.get(GenerationJob, job_id).status == "queued"
