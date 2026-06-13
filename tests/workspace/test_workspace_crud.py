"""Tests for workspace and thread CRUD — create, read, delete via web routes.

Covers:
  - Workspace: create, list, get, rename, delete, duplicate name rejection
  - Thread: create, delete, bulk-delete (with and without generated content)
  - Workspace–thread: assign, unassign, delete workspace leaves threads intact
  - Cascade: deleting a thread with GeneratedImage/GeneratedVideo/GenerationJob
"""
import pytest
from app.models import (
    Workspace, Thread, Message, User,
    GeneratedImage, GeneratedVideo, GenerationJob,
)


# ── Helpers ──────────────────────────────────────────────────────────

def _json(response, expected_status=200):
    """Assert status and return parsed JSON."""
    assert response.status_code == expected_status, (
        f"Expected {expected_status}, got {response.status_code}: "
        f"{response.data[:300].decode(errors='replace')}"
    )
    return response.get_json()


def _make_thread(db, user, title="Test Chat", workspace=None):
    """Create a thread with a couple of messages."""
    t = Thread(user_id=user.id, title=title, workspace_id=workspace.id if workspace else None)
    db.session.add(t)
    db.session.flush()
    msg1 = Message(thread_id=t.id, role="user", content="Hello")
    msg2 = Message(thread_id=t.id, role="assistant", content="Hi there!")
    db.session.add_all([msg1, msg2])
    db.session.commit()
    return t


# ── Workspace CRUD ──────────────────────────────────────────────────

class TestWorkspaceCreate:
    def test_create_workspace(self, app, client, db, make_user, login_as):
        u = make_user()
        login_as(u)
        resp = client.post("/workspaces", json={"name": "Research", "description": "AI stuff"})
        data = _json(resp, 201)
        assert data["name"] == "Research"
        assert data["description"] == "AI stuff"
        assert "id" in data

    def test_create_workspace_no_name(self, app, client, db, make_user, login_as):
        u = make_user()
        login_as(u)
        resp = client.post("/workspaces", json={"name": ""})
        _json(resp, 400)

    def test_create_workspace_duplicate_name(self, app, client, db, make_user, login_as):
        u = make_user()
        login_as(u)
        client.post("/workspaces", json={"name": "Dup"})
        resp = client.post("/workspaces", json={"name": "Dup"})
        _json(resp, 409)

    def test_create_workspace_requires_login(self, app, client, db):
        resp = client.post("/workspaces", json={"name": "Nope"})
        assert resp.status_code == 401 or resp.status_code == 302

    def test_create_workspace_no_body(self, app, client, db, make_user, login_as):
        u = make_user()
        login_as(u)
        resp = client.post("/workspaces", content_type="application/json")
        _json(resp, 400)


class TestWorkspaceList:
    def test_list_workspaces_empty(self, app, client, db, make_user, login_as):
        u = make_user()
        login_as(u)
        resp = client.get("/workspaces")
        data = _json(resp)
        assert data == []

    def test_list_workspaces(self, app, client, db, make_user, login_as):
        u = make_user()
        login_as(u)
        client.post("/workspaces", json={"name": "WS1"})
        client.post("/workspaces", json={"name": "WS2"})
        resp = client.get("/workspaces")
        data = _json(resp)
        assert len(data) == 2
        names = {w["name"] for w in data}
        assert names == {"WS1", "WS2"}


class TestWorkspaceGet:
    def test_get_workspace(self, app, client, db, make_user, login_as):
        u = make_user()
        login_as(u)
        r = client.post("/workspaces", json={"name": "MyWS"})
        ws_id = r.get_json()["id"]
        resp = client.get(f"/workspaces/{ws_id}")
        data = _json(resp)
        assert data["name"] == "MyWS"
        assert "threads" in data
        assert data["thread_count"] == 0

    def test_get_workspace_not_found(self, app, client, db, make_user, login_as):
        u = make_user()
        login_as(u)
        resp = client.get("/workspaces/nonexistent-id")
        assert resp.status_code == 404


class TestWorkspaceUpdate:
    def test_rename_workspace(self, app, client, db, make_user, login_as):
        u = make_user()
        login_as(u)
        r = client.post("/workspaces", json={"name": "Old"})
        ws_id = r.get_json()["id"]
        resp = client.patch(f"/workspaces/{ws_id}", json={"name": "New"})
        data = _json(resp)
        assert data["name"] == "New"

    def test_update_description(self, app, client, db, make_user, login_as):
        u = make_user()
        login_as(u)
        r = client.post("/workspaces", json={"name": "WS"})
        ws_id = r.get_json()["id"]
        resp = client.patch(f"/workspaces/{ws_id}", json={"description": "Updated desc"})
        data = _json(resp)
        assert data["description"] == "Updated desc"

    def test_rename_to_duplicate(self, app, client, db, make_user, login_as):
        u = make_user()
        login_as(u)
        client.post("/workspaces", json={"name": "A"})
        r = client.post("/workspaces", json={"name": "B"})
        ws_b = r.get_json()["id"]
        resp = client.patch(f"/workspaces/{ws_b}", json={"name": "A"})
        _json(resp, 409)


class TestWorkspaceDelete:
    def test_delete_workspace(self, app, client, db, make_user, login_as):
        u = make_user()
        login_as(u)
        r = client.post("/workspaces", json={"name": "Bye"})
        ws_id = r.get_json()["id"]
        resp = client.delete(f"/workspaces/{ws_id}")
        _json(resp)
        assert resp.get_json()["success"] is True
        # Verify gone
        assert Workspace.query.get(ws_id) is None

    def test_delete_workspace_unassigns_threads(self, app, client, db, make_user, login_as):
        """Deleting a workspace should set threads' workspace_id to NULL, not delete them."""
        u = make_user()
        login_as(u)
        r = client.post("/workspaces", json={"name": "WS"})
        ws_id = r.get_json()["id"]
        t = _make_thread(db, u, workspace=Workspace.query.get(ws_id))
        assert t.workspace_id == ws_id

        client.delete(f"/workspaces/{ws_id}")
        # Thread still exists but unassigned
        db.session.refresh(t)
        assert t.workspace_id is None
        assert Thread.query.get(t.id) is not None

    def test_delete_workspace_not_found(self, app, client, db, make_user, login_as):
        u = make_user()
        login_as(u)
        resp = client.delete("/workspaces/nope")
        assert resp.status_code == 404


# ── Thread CRUD ──────────────────────────────────────────────────────

class TestThreadCreate:
    def test_create_thread(self, app, client, db, make_user, login_as):
        u = make_user()
        login_as(u)
        resp = client.post("/threads", data={"title": "New"}, follow_redirects=True)
        assert resp.status_code == 200
        threads = Thread.query.filter_by(user_id=u.id).all()
        assert len(threads) == 1
        assert threads[0].title == "New Chat"

    def test_create_thread_in_workspace(self, app, client, db, make_user, login_as):
        u = make_user()
        login_as(u)
        r = client.post("/workspaces", json={"name": "WS"})
        ws_id = r.get_json()["id"]
        resp = client.post("/threads", data={"workspace_id": ws_id}, follow_redirects=True)
        assert resp.status_code == 200
        t = Thread.query.filter_by(user_id=u.id).first()
        assert t.workspace_id == ws_id


class TestThreadDelete:
    def test_delete_thread(self, app, client, db, make_user, login_as):
        u = make_user()
        login_as(u)
        t = _make_thread(db, u)
        resp = client.delete(f"/threads/{t.id}", headers={"HX-Request": "true"})
        assert resp.status_code == 200
        assert Thread.query.get(t.id) is None
        assert Message.query.filter_by(thread_id=t.id).count() == 0

    def test_delete_thread_not_found(self, app, client, db, make_user, login_as):
        u = make_user()
        login_as(u)
        resp = client.delete("/threads/nonexistent")
        assert resp.status_code == 404

    def test_delete_thread_other_user(self, app, client, db, make_user, login_as):
        u1 = make_user(email="a@b.com")
        u2 = make_user(email="c@d.com")
        t = _make_thread(db, u1)
        login_as(u2)
        resp = client.delete(f"/threads/{t.id}", headers={"HX-Request": "true"})
        assert resp.status_code == 404
        assert Thread.query.get(t.id) is not None

    def test_delete_thread_with_messages(self, app, client, db, make_user, login_as):
        u = make_user()
        login_as(u)
        t = _make_thread(db, u)
        assert Message.query.filter_by(thread_id=t.id).count() == 2
        client.delete(f"/threads/{t.id}")
        assert Message.query.filter_by(thread_id=t.id).count() == 0

    def test_delete_thread_with_generated_image(self, app, client, db, make_user, login_as):
        """Cascade-delete should remove GeneratedImage rows."""
        u = make_user()
        login_as(u)
        t = _make_thread(db, u)
        img = GeneratedImage(
            user_id=u.id, thread_id=t.id, prompt="a cat",
            seed=42, width=512, height=512, filename="cat.png",
        )
        db.session.add(img)
        db.session.commit()
        assert GeneratedImage.query.filter_by(thread_id=t.id).count() == 1

        resp = client.delete(f"/threads/{t.id}", headers={"HX-Request": "true"})
        assert resp.status_code == 200
        assert GeneratedImage.query.filter_by(thread_id=t.id).count() == 0
        assert Thread.query.get(t.id) is None

    def test_delete_thread_with_generated_video(self, app, client, db, make_user, login_as):
        """Cascade-delete should remove GeneratedVideo rows."""
        u = make_user()
        login_as(u)
        t = _make_thread(db, u)
        vid = GeneratedVideo(
            user_id=u.id, thread_id=t.id, prompt="a sunset",
            seed=99, width=480, height=480, filename="sunset.mp4",
        )
        db.session.add(vid)
        db.session.commit()
        assert GeneratedVideo.query.filter_by(thread_id=t.id).count() == 1

        resp = client.delete(f"/threads/{t.id}", headers={"HX-Request": "true"})
        assert resp.status_code == 200
        assert GeneratedVideo.query.filter_by(thread_id=t.id).count() == 0
        assert Thread.query.get(t.id) is None

    def test_delete_thread_with_generation_job(self, app, client, db, make_user, login_as):
        """Cascade-delete should remove GenerationJob rows."""
        u = make_user()
        login_as(u)
        t = _make_thread(db, u)
        job = GenerationJob(
            user_id=u.id, thread_id=t.id, kind="image",
            status="done",
        )
        db.session.add(job)
        db.session.commit()
        assert GenerationJob.query.filter_by(thread_id=t.id).count() == 1

        resp = client.delete(f"/threads/{t.id}", headers={"HX-Request": "true"})
        assert resp.status_code == 200
        assert GenerationJob.query.filter_by(thread_id=t.id).count() == 0
        assert Thread.query.get(t.id) is None

    def test_delete_thread_with_all_generated_types(self, app, client, db, make_user, login_as):
        """Thread with image + video + job should all cascade-delete cleanly."""
        u = make_user()
        login_as(u)
        t = _make_thread(db, u)
        img = GeneratedImage(
            user_id=u.id, thread_id=t.id, prompt="a cat",
            seed=1, width=512, height=512, filename="cat.png",
        )
        vid = GeneratedVideo(
            user_id=u.id, thread_id=t.id, prompt="a dog",
            seed=2, width=480, height=480, filename="dog.mp4",
        )
        job = GenerationJob(
            user_id=u.id, thread_id=t.id, kind="video",
            status="queued",
        )
        db.session.add_all([img, vid, job])
        db.session.commit()

        resp = client.delete(f"/threads/{t.id}", headers={"HX-Request": "true"})
        assert resp.status_code == 200
        assert Thread.query.get(t.id) is None
        assert Message.query.filter_by(thread_id=t.id).count() == 0
        assert GeneratedImage.query.filter_by(thread_id=t.id).count() == 0
        assert GeneratedVideo.query.filter_by(thread_id=t.id).count() == 0
        assert GenerationJob.query.filter_by(thread_id=t.id).count() == 0


class TestBulkDeleteThreads:
    def test_bulk_delete(self, app, client, db, make_user, login_as):
        u = make_user()
        login_as(u)
        t1 = _make_thread(db, u, title="T1")
        t2 = _make_thread(db, u, title="T2")
        t3 = _make_thread(db, u, title="T3")
        resp = client.post(
            "/threads/bulk-delete",
            json={"thread_ids": [t1.id, t3.id]},
        )
        data = _json(resp)
        assert data["deleted"] == 2
        assert Thread.query.get(t1.id) is None
        assert Thread.query.get(t2.id) is not None
        assert Thread.query.get(t3.id) is None

    def test_bulk_delete_empty_list(self, app, client, db, make_user, login_as):
        u = make_user()
        login_as(u)
        resp = client.post(
            "/threads/bulk-delete",
            json={"thread_ids": []},
        )
        _json(resp, 400)

    def test_bulk_delete_only_own_threads(self, app, client, db, make_user, login_as):
        u1 = make_user(email="a@b.com")
        t1 = _make_thread(db, u1, title="U1_T")
        u2 = make_user(email="c@d.com")
        login_as(u2)
        # u2 tries to delete u1's thread
        resp = client.post(
            "/threads/bulk-delete",
            json={"thread_ids": [t1.id]},
        )
        data = _json(resp)
        assert data["deleted"] == 0
        assert Thread.query.get(t1.id) is not None

    def test_bulk_delete_with_generated_content(self, app, client, db, make_user, login_as):
        """Bulk delete should cascade through generated content."""
        u = make_user()
        login_as(u)
        t1 = _make_thread(db, u, title="WithImg")
        img = GeneratedImage(
            user_id=u.id, thread_id=t1.id, prompt="a cat",
            seed=1, width=512, height=512, filename="cat.png",
        )
        job = GenerationJob(
            user_id=u.id, thread_id=t1.id, kind="image",
            status="done",
        )
        db.session.add_all([img, job])
        db.session.commit()

        resp = client.post(
            "/threads/bulk-delete",
            json={"thread_ids": [t1.id]},
        )
        data = _json(resp)
        assert data["deleted"] >= 1
        assert Thread.query.get(t1.id) is None
        assert GeneratedImage.query.filter_by(thread_id=t1.id).count() == 0
        assert GenerationJob.query.filter_by(thread_id=t1.id).count() == 0


# ── Workspace–Thread assignment ─────────────────────────────────────

class TestWorkspaceThreadAssignment:
    def test_assign_thread_to_workspace(self, app, client, db, make_user, login_as):
        u = make_user()
        login_as(u)
        r = client.post("/workspaces", json={"name": "WS"})
        ws_id = r.get_json()["id"]
        t = _make_thread(db, u)

        resp = client.put(f"/workspaces/{ws_id}/threads/{t.id}")
        data = _json(resp)
        assert data["success"] is True
        db.session.refresh(t)
        assert t.workspace_id == ws_id

    def test_unassign_thread(self, app, client, db, make_user, login_as):
        u = make_user()
        login_as(u)
        r = client.post("/workspaces", json={"name": "WS"})
        ws_id = r.get_json()["id"]
        ws = Workspace.query.get(ws_id)
        t = _make_thread(db, u, workspace=ws)
        assert t.workspace_id == ws_id

        resp = client.delete(f"/workspaces/threads/{t.id}")
        data = _json(resp)
        assert data["success"] is True
        db.session.refresh(t)
        assert t.workspace_id is None

    def test_list_workspace_threads(self, app, client, db, make_user, login_as):
        u = make_user()
        login_as(u)
        r = client.post("/workspaces", json={"name": "WS"})
        ws_id = r.get_json()["id"]
        ws = Workspace.query.get(ws_id)
        _make_thread(db, u, title="In WS", workspace=ws)
        _make_thread(db, u, title="Outside")

        resp = client.get(f"/workspaces/{ws_id}/threads")
        data = _json(resp)
        assert len(data) == 1
        assert data[0]["title"] == "In WS"

    def test_assign_other_users_thread_fails(self, app, client, db, make_user, login_as):
        u1 = make_user(email="a@b.com")
        t_u1 = _make_thread(db, u1, title="U1 Thread")
        u2 = make_user(email="c@d.com")
        login_as(u2)
        r = client.post("/workspaces", json={"name": "U2 WS"})
        ws_id = r.get_json()["id"]
        resp = client.put(f"/workspaces/{ws_id}/threads/{t_u1.id}")
        assert resp.status_code == 404
