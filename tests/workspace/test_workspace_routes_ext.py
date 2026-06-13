"""Workspace routes extended tests: CRUD, thread assignment, unassign,
summarize, toggle context, auth scoping."""
import json
import uuid
from unittest.mock import patch, MagicMock
from app.models import Workspace, Thread, Message, User


def _ws(db, user, name="Test WS", desc=None):
    ws = Workspace(user_id=user.id, name=name, description=desc)
    db.session.add(ws)
    db.session.commit()
    return ws


def _thread(db, user, title="Thread", workspace_id=None):
    t = Thread(id=str(uuid.uuid4()), user_id=user.id, title=title, workspace_id=workspace_id)
    db.session.add(t)
    db.session.commit()
    return t


def _msg(db, thread, role="user", content="hello"):
    m = Message(thread_id=thread.id, role=role, content=content)
    db.session.add(m)
    db.session.commit()
    return m


# ─── list_workspaces ───

def test_list_workspaces_empty(app, db, make_user, login_as, client):
    user = make_user()
    login_as(user)
    resp = client.get("/workspaces")
    assert resp.status_code == 200
    assert resp.get_json() == []


def test_list_workspaces_with_data(app, db, make_user, login_as, client):
    user = make_user()
    _ws(db, user, "My WS")
    login_as(user)
    resp = client.get("/workspaces")
    assert resp.status_code == 200
    data = resp.get_json()
    assert len(data) == 1
    assert data[0]["name"] == "My WS"


def test_list_workspaces_scoped(app, db, make_user, login_as, client):
    user1 = make_user()
    user2 = make_user(username="other", email="other@test.com")
    _ws(db, user1, "WS1")
    _ws(db, user2, "WS2")
    login_as(user1)
    resp = client.get("/workspaces")
    data = resp.get_json()
    assert len(data) == 1
    assert data[0]["name"] == "WS1"


# ─── create_workspace ───

def test_create_workspace_success(app, db, make_user, login_as, client):
    user = make_user()
    login_as(user)
    resp = client.post("/workspaces", json={"name": "New WS", "description": "desc"})
    assert resp.status_code == 201
    data = resp.get_json()
    assert data["name"] == "New WS"


def test_create_workspace_no_name(app, db, make_user, login_as, client):
    user = make_user()
    login_as(user)
    resp = client.post("/workspaces", json={"name": ""})
    assert resp.status_code == 400


def test_create_workspace_duplicate(app, db, make_user, login_as, client):
    user = make_user()
    _ws(db, user, "Dup")
    login_as(user)
    resp = client.post("/workspaces", json={"name": "Dup"})
    assert resp.status_code == 409


def test_create_workspace_no_body(app, db, make_user, login_as, client):
    user = make_user()
    login_as(user)
    resp = client.post("/workspaces", content_type="application/json", data="")
    assert resp.status_code == 400


# ─── get_workspace ───

def test_get_workspace_success(app, db, make_user, login_as, client):
    user = make_user()
    ws = _ws(db, user, "Get WS")
    login_as(user)
    resp = client.get(f"/workspaces/{ws.id}")
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["name"] == "Get WS"
    assert "threads" in data


def test_get_workspace_not_owner(app, db, make_user, login_as, client):
    user1 = make_user()
    user2 = make_user(username="other", email="other@test.com")
    ws = _ws(db, user1)
    login_as(user2)
    resp = client.get(f"/workspaces/{ws.id}")
    assert resp.status_code == 404


# ─── update_workspace ───

def test_update_workspace_rename(app, db, make_user, login_as, client):
    user = make_user()
    ws = _ws(db, user, "Old Name")
    login_as(user)
    resp = client.patch(f"/workspaces/{ws.id}", json={"name": "New Name"})
    assert resp.status_code == 200
    assert resp.get_json()["name"] == "New Name"


def test_update_workspace_empty_name(app, db, make_user, login_as, client):
    user = make_user()
    ws = _ws(db, user)
    login_as(user)
    resp = client.patch(f"/workspaces/{ws.id}", json={"name": ""})
    assert resp.status_code == 400


def test_update_workspace_duplicate_name(app, db, make_user, login_as, client):
    user = make_user()
    ws1 = _ws(db, user, "WS-A")
    ws2 = _ws(db, user, "WS-B")
    login_as(user)
    resp = client.patch(f"/workspaces/{ws2.id}", json={"name": "WS-A"})
    assert resp.status_code == 409


def test_update_workspace_description(app, db, make_user, login_as, client):
    user = make_user()
    ws = _ws(db, user)
    login_as(user)
    resp = client.patch(f"/workspaces/{ws.id}", json={"description": "new desc"})
    assert resp.status_code == 200


# ─── delete_workspace ───

def test_delete_workspace(app, db, make_user, login_as, client):
    user = make_user()
    ws = _ws(db, user, "Delete Me")
    login_as(user)
    resp = client.delete(f"/workspaces/{ws.id}")
    assert resp.status_code == 200
    assert db.session.get(Workspace, ws.id) is None


def test_delete_workspace_unassigns_threads(app, db, make_user, login_as, client):
    user = make_user()
    ws = _ws(db, user)
    thread = _thread(db, user, workspace_id=ws.id)
    login_as(user)
    client.delete(f"/workspaces/{ws.id}")
    db.session.refresh(thread)
    assert thread.workspace_id is None


# ─── list_workspace_threads ───

def test_list_workspace_threads(app, db, make_user, login_as, client):
    user = make_user()
    ws = _ws(db, user)
    t1 = _thread(db, user, "T1", workspace_id=ws.id)
    t2 = _thread(db, user, "T2", workspace_id=ws.id)
    login_as(user)
    resp = client.get(f"/workspaces/{ws.id}/threads")
    assert resp.status_code == 200
    data = resp.get_json()
    assert len(data) == 2


# ─── assign_thread ───

def test_assign_thread(app, db, make_user, login_as, client):
    user = make_user()
    ws = _ws(db, user)
    thread = _thread(db, user)
    login_as(user)
    with patch("app.workspace.routes.generate_thread_summary", return_value="summary"):
        resp = client.put(f"/workspaces/{ws.id}/threads/{thread.id}")
    assert resp.status_code == 200


def test_assign_thread_not_owner(app, db, make_user, login_as, client):
    user1 = make_user()
    user2 = make_user(username="other", email="other@test.com")
    ws = _ws(db, user1)
    thread = _thread(db, user1)
    login_as(user2)
    resp = client.put(f"/workspaces/{ws.id}/threads/{thread.id}")
    assert resp.status_code == 404


# ─── unassign_thread ───

def test_unassign_thread(app, db, make_user, login_as, client):
    user = make_user()
    ws = _ws(db, user)
    thread = _thread(db, user, workspace_id=ws.id)
    login_as(user)
    resp = client.delete(f"/workspaces/threads/{thread.id}")
    assert resp.status_code == 200
    db.session.refresh(thread)
    assert thread.workspace_id is None


# ─── toggle_context ───

def test_toggle_context(app, db, make_user, login_as, client):
    user = make_user()
    ws = _ws(db, user)
    thread = _thread(db, user, workspace_id=ws.id)
    login_as(user)
    db.session.refresh(thread)
    before = thread.use_workspace_context
    resp = client.post(f"/workspaces/threads/{thread.id}/toggle-context")
    assert resp.status_code == 200
    assert resp.get_json()["use_workspace_context"] == (not before)


# ─── summarize_thread ───

def test_summarize_thread_success(app, db, make_user, login_as, client):
    user = make_user()
    thread = _thread(db, user)
    _msg(db, thread, "user", "What is AI?")
    _msg(db, thread, "assistant", "AI is...")
    login_as(user)
    with patch("app.workspace.routes.generate_thread_summary", return_value="AI summary"):
        resp = client.post(f"/workspaces/threads/{thread.id}/summarize")
    assert resp.status_code == 200


def test_summarize_thread_failure(app, db, make_user, login_as, client):
    user = make_user()
    thread = _thread(db, user)
    login_as(user)
    with patch("app.workspace.routes.generate_thread_summary", return_value=None):
        resp = client.post(f"/workspaces/threads/{thread.id}/summarize")
    assert resp.status_code == 500
