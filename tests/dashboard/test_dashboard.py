"""Dashboard routes tests: index, search, workspace_detail, create/delete/bulk-delete threads."""
import uuid
from datetime import datetime, timezone, timedelta
from app.models import Thread, Message, Workspace


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


def _ws(db, user, name="Test WS", desc=None):
    w = Workspace(id=str(uuid.uuid4()), user_id=user.id, name=name, description=desc)
    db.session.add(w)
    db.session.commit()
    return w


# ─── index ───

def test_dashboard_requires_login(client):
    resp = client.get("/dashboard", follow_redirects=False)
    assert resp.status_code in (301, 302)


def test_dashboard_empty(app, db, make_user, login_as, client):
    user = make_user()
    login_as(user)
    resp = client.get("/dashboard")
    assert resp.status_code == 200


def test_dashboard_shows_threads_with_messages(app, db, make_user, login_as, client):
    user = make_user()
    thread = _thread(db, user, title="My Chat")
    _msg(db, thread, "user", "Hello world", 0)
    _msg(db, thread, "assistant", "Hi!", 1)
    login_as(user)
    resp = client.get("/dashboard")
    assert resp.status_code == 200
    assert b"My Chat" in resp.data


def test_dashboard_hides_empty_threads(app, db, make_user, login_as, client):
    user = make_user()
    empty = _thread(db, user, title="Empty Thread")
    with_msgs = _thread(db, user, title="Has Messages")
    _msg(db, with_msgs, "user", "Hi", 0)
    login_as(user)
    resp = client.get("/dashboard")
    assert b"Has Messages" in resp.data
    assert b"Empty Thread" not in resp.data


def test_dashboard_search(app, db, make_user, login_as, client):
    user = make_user()
    t1 = _thread(db, user, title="Alpha")
    _msg(db, t1, "user", "machine learning topic", 0)
    t2 = _thread(db, user, title="Beta")
    _msg(db, t2, "user", "cooking recipes", 0)
    login_as(user)
    resp = client.get("/dashboard?q=learning")
    assert resp.status_code == 200
    assert b"Alpha" in resp.data
    assert b"Beta" not in resp.data


def test_dashboard_search_too_short(app, db, make_user, login_as, client):
    """Single-char search should be ignored."""
    user = make_user()
    t1 = _thread(db, user, title="Alpha")
    _msg(db, t1, "user", "abc", 0)
    t2 = _thread(db, user, title="Beta")
    _msg(db, t2, "user", "xyz", 0)
    login_as(user)
    resp = client.get("/dashboard?q=a")
    # Short query ignored — both threads visible
    assert b"Alpha" in resp.data
    assert b"Beta" in resp.data


def test_dashboard_hides_workspace_threads(app, db, make_user, login_as, client):
    user = make_user()
    ws = _ws(db, user)
    ws_thread = _thread(db, user, title="WS Thread", workspace_id=ws.id)
    _msg(db, ws_thread, "user", "Hi", 0)
    free_thread = _thread(db, user, title="Free Thread")
    _msg(db, free_thread, "user", "Hi", 0)
    login_as(user)
    resp = client.get("/dashboard")
    assert b"Free Thread" in resp.data
    assert b"WS Thread" not in resp.data


# ─── workspace_detail ───

def test_workspace_detail_requires_login(client):
    resp = client.get("/dashboard/workspace/123", follow_redirects=False)
    assert resp.status_code in (301, 302)


def test_workspace_detail_404_for_nonexistent(app, db, make_user, login_as, client):
    user = make_user()
    login_as(user)
    resp = client.get(f"/dashboard/workspace/{uuid.uuid4()}")
    assert resp.status_code == 404


def test_workspace_detail_shows_threads(app, db, make_user, login_as, client):
    user = make_user()
    ws = _ws(db, user, name="Project")
    t1 = _thread(db, user, title="Thread A", workspace_id=ws.id)
    _msg(db, t1, "user", "Content A", 0)
    login_as(user)
    resp = client.get(f"/dashboard/workspace/{ws.id}")
    assert resp.status_code == 200
    assert b"Project" in resp.data
    assert b"Thread A" in resp.data


def test_workspace_detail_context_budget(app, db, make_user, login_as, client):
    user = make_user()
    ws = _ws(db, user)
    t1 = _thread(db, user, title="T1", workspace_id=ws.id, summary="Some summary text")
    _msg(db, t1, "user", "Hello", 0)
    login_as(user)
    resp = client.get(f"/dashboard/workspace/{ws.id}")
    assert resp.status_code == 200


# ─── create_thread ───

def test_create_thread(app, db, make_user, login_as, client):
    user = make_user()
    login_as(user)
    resp = client.post("/threads", follow_redirects=False)
    assert resp.status_code == 303
    thread = Thread.query.filter_by(user_id=user.id).first()
    assert thread is not None
    assert thread.title == "New Chat"


def test_create_thread_with_workspace(app, db, make_user, login_as, client):
    user = make_user()
    ws = _ws(db, user, name="My WS")
    login_as(user)
    resp = client.post("/threads", data={"workspace_id": ws.id}, follow_redirects=False)
    assert resp.status_code == 303
    thread = Thread.query.filter_by(user_id=user.id).first()
    assert thread.workspace_id == ws.id


def test_create_thread_invalid_workspace_ignored(app, db, make_user, login_as, client):
    user = make_user()
    login_as(user)
    resp = client.post("/threads", data={"workspace_id": "fake-id-12345"}, follow_redirects=False)
    assert resp.status_code == 303
    thread = Thread.query.filter_by(user_id=user.id).first()
    assert thread.workspace_id is None


# ─── delete_thread ───

def test_delete_thread(app, db, make_user, login_as, client):
    user = make_user()
    thread = _thread(db, user, title="Delete Me")
    login_as(user)
    resp = client.delete(f"/threads/{thread.id}", headers={"HX-Request": "true"})
    assert resp.status_code == 200
    assert Thread.query.get(thread.id) is None


def test_delete_thread_not_owner(app, db, make_user, login_as, client):
    user1 = make_user()
    user2 = make_user(username="other", email="other@example.com")
    thread = _thread(db, user1)
    login_as(user2)
    resp = client.delete(f"/threads/{thread.id}")
    assert resp.status_code == 404


def test_delete_thread_htx(app, db, make_user, login_as, client):
    user = make_user()
    thread = _thread(db, user)
    login_as(user)
    resp = client.delete(f"/threads/{thread.id}", headers={"HX-Request": "true"})
    assert resp.status_code == 200
    assert resp.data == b""


# ─── bulk_delete ───

def test_bulk_delete_threads(app, db, make_user, login_as, client):
    user = make_user()
    t1 = _thread(db, user, title="A")
    t2 = _thread(db, user, title="B")
    t3 = _thread(db, user, title="C")
    login_as(user)
    resp = client.post("/threads/bulk-delete", json={"thread_ids": [t1.id, t2.id]})
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["deleted"] == 2
    assert Thread.query.filter_by(user_id=user.id).count() == 1


def test_bulk_delete_no_ids(app, db, make_user, login_as, client):
    user = make_user()
    login_as(user)
    resp = client.post("/threads/bulk-delete", json={})
    assert resp.status_code == 400


def test_bulk_delete_only_own_threads(app, db, make_user, login_as, client):
    user1 = make_user()
    user2 = make_user(username="other2", email="other2@example.com")
    t1 = _thread(db, user1)
    t2 = _thread(db, user2)
    login_as(user1)
    resp = client.post("/threads/bulk-delete", json={"thread_ids": [t1.id, t2.id]})
    data = resp.get_json()
    assert data["deleted"] == 1  # only own thread
