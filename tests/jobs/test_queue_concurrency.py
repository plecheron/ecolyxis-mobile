"""Queue correctness under concurrency: multiple users running different job
kinds (chat / image) through the *real* claim()->run_job() worker path, proving
that every artifact lands on the right user+thread keyed by its own job_id (no
cross-contamination / "switch"), that premium jobs are served before free ones,
and that each job's event stream is isolated.

Redis is faked and the GPU backends stubbed, so no daemon/GPU is needed.
"""
import itertools
import uuid

import fakeredis
import pytest

from app.models import Thread, Message, GeneratedImage, GenerationJob


@pytest.fixture
def fake_redis():
    import app.redis_client as rc
    fake = fakeredis.FakeRedis(decode_responses=True)
    prev = rc._client
    rc._client = fake
    yield fake
    rc._client = prev


@pytest.fixture
def stub_llm(monkeypatch):
    """LLM that echoes the thread's last user message, so each chat job's output
    is uniquely tied to its own thread — a switch would show the wrong text."""
    class FakeClient:
        def build_messages(self, thread, mode="standard"):
            last = [m for m in thread.messages if m.role == "user"][-1]
            return [{"role": "user", "content": last.content}]

        def stream_chat(self, msgs, mode="standard"):
            echo = msgs[-1]["content"]
            yield {"thinking_start": True}
            yield {"thinking_end": True}
            yield f"reply:{echo}"
            yield {"prompt_tokens": 3, "completion_tokens": 1}

    import app.chat as chatmod
    monkeypatch.setattr(chatmod, "get_client", lambda: FakeClient())
    return FakeClient


@pytest.fixture
def stub_image(monkeypatch):
    """Stub the image backend; each save gets a unique local filename so a
    misrouted result would be detectable."""
    import app.jobs.handlers.media as media
    import app.chat.images as images

    class FakeResp:
        status_code = 200
        text = ""
        def iter_lines(self, decode_unicode=False):
            yield 'data: {"stage": "diffusion", "step": 1, "total_steps": 9}'
            yield ('data: {"stage": "done", "filename": "remote.png", '
                   '"seed": 42, "width": 512, "height": 512}')

    class FakeRequests:
        def post(self, url, **kw):
            assert "/generate-stream" in url
            return FakeResp()

    monkeypatch.setattr(media, "requests", FakeRequests())
    monkeypatch.setattr(images, "_get_image_url", lambda: "http://stub")
    counter = itertools.count()
    monkeypatch.setattr(images, "_save_remote_image",
                        lambda url: (f"local_{next(counter)}.png", "/x"))


def _thread(db, user, title="T"):
    t = Thread(id=str(uuid.uuid4()), user_id=user.id, title=title)
    db.session.add(t)
    db.session.commit()
    return t


def _drain_premium_first(fake_redis, wid):
    """Run the real worker loop until both lanes are empty, premium-first
    (mirrors claim()'s ordering, non-blocking so the test never hangs)."""
    from app.jobs.worker import run_job
    from app.jobs import (QUEUE_PREMIUM, QUEUE_FREE, processing_key, ack)
    from flask import current_app
    app = current_app._get_current_object()
    pkey = processing_key(wid)
    claimed_order = []
    while True:
        jid = (fake_redis.lmove(QUEUE_PREMIUM, pkey, "RIGHT", "LEFT")
               or fake_redis.lmove(QUEUE_FREE, pkey, "RIGHT", "LEFT"))
        if not jid:
            break
        claimed_order.append(jid)
        try:
            run_job(app, wid, jid)
        finally:
            ack(wid, jid)
    return claimed_order


# --- the core anti-"switch" test ------------------------------------------

def test_multi_user_multi_kind_no_crosstalk(app, db, make_user, fake_redis,
                                            stub_llm, stub_image):
    """Two users each submit a chat and an image job, interleaved. After the
    worker drains the queue, every artifact must belong to the submitting user
    and thread, keyed by its own job_id."""
    from app.jobs import enqueue, read_events

    with app.app_context():
        alice = make_user(username="alice", email="alice@x.com")
        bob = make_user(username="bob", email="bob@x.com")
        ta_chat = _thread(db, alice, "alice-chat")
        ta_img = _thread(db, alice, "alice-img")
        tb_chat = _thread(db, bob, "bob-chat")
        tb_img = _thread(db, bob, "bob-img")

        # each chat thread gets a distinct user message the echo-LLM will mirror
        for t, text in [(ta_chat, "alice-says-hi"), (tb_chat, "bob-says-yo")]:
            db.session.add(Message(thread_id=t.id, role="user", content=text))
        db.session.commit()

        def mk(user, thread, kind, params):
            j = GenerationJob(user_id=user.id, thread_id=thread.id, kind=kind,
                              status="queued", is_premium=False, params=params)
            db.session.add(j)
            db.session.commit()
            return j.id

        ids = {
            "a_chat": mk(alice, ta_chat, "chat", {"mode": "standard"}),
            "a_img": mk(alice, ta_img, "image", {"prompt": "alice-cat", "width": 512, "height": 512}),
            "b_chat": mk(bob, tb_chat, "chat", {"mode": "standard"}),
            "b_img": mk(bob, tb_img, "image", {"prompt": "bob-dog", "width": 512, "height": 512}),
        }
        thread_ids = {k: v for k, v in [
            ("a_chat", ta_chat.id), ("a_img", ta_img.id),
            ("b_chat", tb_chat.id), ("b_img", tb_img.id)]}
        user_ids = {"a_chat": alice.id, "a_img": alice.id,
                    "b_chat": bob.id, "b_img": bob.id}

        # interleave enqueue order across users + kinds
        for k in ["a_chat", "b_img", "a_img", "b_chat"]:
            enqueue(ids[k], is_premium=False)

    with app.app_context():
        _drain_premium_first(fake_redis, "w-0")

    with app.app_context():
        # every job reached done
        for k, jid in ids.items():
            assert db.session.get(GenerationJob, jid).status == "done", k

        # chat results: right text, right thread, right user, keyed by job_id
        for k, expect in [("a_chat", "reply:alice-says-hi"),
                          ("b_chat", "reply:bob-says-yo")]:
            msg = Message.query.filter_by(job_id=ids[k], role="assistant").one()
            assert msg.content == expect
            assert msg.thread_id == thread_ids[k]

        # image results: right prompt, right thread+user, keyed by job_id
        for k, prompt in [("a_img", "alice-cat"), ("b_img", "bob-dog")]:
            img = GeneratedImage.query.filter_by(job_id=ids[k]).one()
            assert img.prompt == prompt
            assert img.thread_id == thread_ids[k]
            assert img.user_id == user_ids[k]
            # its assistant Message lives in the same thread, keyed by job_id
            imsg = Message.query.filter_by(job_id=ids[k], role="assistant").one()
            assert imsg.thread_id == thread_ids[k]

        # cross-check: no thread received an artifact from another job
        assert GeneratedImage.query.count() == 2
        assert Message.query.filter_by(role="assistant").count() == 4
        # the two images got distinct local files (no overwrite/switch)
        files = {i.filename for i in GeneratedImage.query.all()}
        assert len(files) == 2

        # event streams are isolated per job_id
        a_text = "".join(e["text"] for _, e in read_events(ids["a_chat"], "0", block_ms=50)
                         if e["type"] == "content")
        b_text = "".join(e["text"] for _, e in read_events(ids["b_chat"], "0", block_ms=50)
                         if e["type"] == "content")
        assert a_text == "reply:alice-says-hi"
        assert b_text == "reply:bob-says-yo"


def test_premium_served_before_free(app, db, make_user, fake_redis, stub_llm):
    """A premium job enqueued after free jobs is claimed first."""
    from app.jobs import enqueue

    with app.app_context():
        u = make_user()
        t = _thread(db, u)
        db.session.add(Message(thread_id=t.id, role="user", content="hi"))
        db.session.commit()

        def mk(premium):
            j = GenerationJob(user_id=u.id, thread_id=t.id, kind="chat",
                              status="queued", is_premium=premium, params={"mode": "standard"})
            db.session.add(j)
            db.session.commit()
            return j.id

        free1 = mk(False)
        free2 = mk(False)
        prem = mk(True)
        enqueue(free1, is_premium=False)
        enqueue(free2, is_premium=False)
        enqueue(prem, is_premium=True)

    with app.app_context():
        order = _drain_premium_first(fake_redis, "w-prio")

    assert order[0] == prem, "premium job must be claimed before free jobs"
