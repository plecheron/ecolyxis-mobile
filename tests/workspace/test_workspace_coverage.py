"""Additional workspace route tests (#123: improve coverage)."""
import pytest
import json
from app.models import Workspace, Thread, Message


def _user(db, suffix=""):
    from app.models import User
    import time
    u = User(username=f"ws-{int(time.time()*1000)}{suffix}", email=f"ws{suffix}@t.com", password_hash="x")
    db.session.add(u)
    db.session.commit()
    return u


def _workspace(db, user, name="TestWS"):
    ws = Workspace(user_id=user.id, name=name)
    db.session.add(ws)
    db.session.commit()
    return ws


def _thread(db, user, workspace=None):
    import uuid
    t = Thread(id=str(uuid.uuid4()), user_id=user.id, title="Thread", workspace_id=workspace.id if workspace else None)
    db.session.add(t)
    db.session.commit()
    return t


class TestWorkspaceAuthScoping:
    """Test that workspace routes enforce user ownership."""

    def test_delete_workspace_not_owner(self, app, db, client, login_as):
        user1 = _user(db)
        user2 = _user(db, "b")
        ws = _workspace(db, user1)
        login_as(user2)
        resp = client.delete(f"/workspaces/{ws.id}")
        assert resp.status_code == 404

    def test_get_workspace_not_owner(self, app, db, client, login_as):
        user1 = _user(db)
        user2 = _user(db, "b")
        ws = _workspace(db, user1)
        login_as(user2)
        resp = client.get(f"/workspaces/{ws.id}")
        assert resp.status_code == 404

    def test_update_workspace_not_owner(self, app, db, client, login_as):
        user1 = _user(db)
        user2 = _user(db, "b")
        ws = _workspace(db, user1)
        login_as(user2)
        resp = client.patch(
            f"/workspaces/{ws.id}",
            data=json.dumps({"name": "Hacked"}),
            content_type="application/json",
        )
        assert resp.status_code == 404

    def test_list_workspaces_not_scoped(self, app, db, client, login_as):
        """User1 workspaces should not appear in user2 list."""
        user1 = _user(db)
        user2 = _user(db, "b")
        _workspace(db, user1, "User1WS")
        login_as(user2)
        resp = client.get("/workspaces")
        data = resp.get_json()
        names = [w["name"] for w in data]
        assert "User1WS" not in names


class TestWorkspaceContextHelper:
    """Test _get_workspace_context_by_id helper."""

    def test_context_by_id_found(self, app, db):
        user = _user(db)
        ws = _workspace(db, user)
        ws.description = "A test workspace for ML experiments."
        db.session.commit()
        from app.workspace.routes import _get_workspace_context_by_id
        with app.app_context():
            result = _get_workspace_context_by_id(ws.id)
            assert "ML experiments" in result

    def test_context_by_id_not_found(self, app):
        from app.workspace.routes import _get_workspace_context_by_id
        with app.app_context():
            result = _get_workspace_context_by_id("nonexistent-id")
            assert result is None or result == ""

    def test_context_by_id_with_description(self, app, db):
        """Verify the helper returns workspace description."""
        user = _user(db)
        ws = _workspace(db, user)
        ws.description = "X" * 5000  # Very long description
        db.session.commit()
        from app.workspace.routes import _get_workspace_context_by_id
        with app.app_context():
            result = _get_workspace_context_by_id(ws.id)
            assert result is not None
            assert "TestWS" in result


class TestToggleContext:
    """Test toggle_workspace_context endpoint edge cases."""

    def test_toggle_context_off_then_on(self, app, db, client, login_as):
        user = _user(db)
        ws = _workspace(db, user)
        thread = _thread(db, user, ws)
        login_as(user)

        # Default is True; toggle off
        resp1 = client.post(f"/workspaces/threads/{thread.id}/toggle-context")
        assert resp1.status_code == 200
        assert resp1.get_json()["use_workspace_context"] is False

        # Toggle back on
        resp2 = client.post(f"/workspaces/threads/{thread.id}/toggle-context")
        assert resp2.status_code == 200
        assert resp2.get_json()["use_workspace_context"] is True

    def test_toggle_context_not_owner(self, app, db, client, login_as):
        user1 = _user(db)
        user2 = _user(db, "b")
        ws = _workspace(db, user1)
        thread = _thread(db, user1, ws)
        login_as(user2)
        resp = client.post(f"/workspaces/threads/{thread.id}/toggle-context")
        assert resp.status_code == 404

    def test_toggle_context_not_found(self, app, db, client, login_as):
        user = _user(db)
        login_as(user)
        resp = client.post("/workspaces/threads/nonexistent-id/toggle-context")
        assert resp.status_code == 404


class TestWorkspaceThreadListing:
    """Test list_workspace_threads edge cases."""

    def test_list_threads_empty_workspace(self, app, db, client, login_as):
        user = _user(db)
        ws = _workspace(db, user)
        login_as(user)
        resp = client.get(f"/workspaces/{ws.id}/threads")
        assert resp.status_code == 200
        assert resp.get_json() == []

    def test_list_threads_with_messages(self, app, db, client, login_as):
        user = _user(db)
        ws = _workspace(db, user)
        thread = _thread(db, user, ws)
        msg = Message(thread_id=thread.id, role="user", content="Hello", tokens_used=5)
        db.session.add(msg)
        db.session.commit()
        login_as(user)
        resp = client.get(f"/workspaces/{ws.id}/threads")
        data = resp.get_json()
        assert len(data) == 1
        assert data[0]["message_count"] == 1


class TestEphemeralChat:
    """Test ephemeral_chat endpoint basic behavior."""

    def test_ephemeral_chat_not_owner(self, app, db, client, login_as):
        user1 = _user(db)
        user2 = _user(db, "b")
        ws = _workspace(db, user1)
        login_as(user2)
        resp = client.post(
            f"/workspaces/{ws.id}/ephemeral-chat",
            data=json.dumps({"prompt": "Hi"}),
            content_type="application/json",
        )
        assert resp.status_code == 404

    def test_ephemeral_chat_empty_prompt(self, app, db, client, login_as):
        user = _user(db)
        ws = _workspace(db, user)
        login_as(user)
        resp = client.post(
            f"/workspaces/{ws.id}/ephemeral-chat",
            data=json.dumps({"prompt": ""}),
            content_type="application/json",
        )
        # Should handle empty prompt gracefully (stream error or 400)
        assert resp.status_code in (200, 400)


class TestWorkspaceDeleteCascade:
    """Test delete workspace cascade behavior."""

    def test_delete_with_multiple_threads(self, app, db, client, login_as):
        user = _user(db)
        ws = _workspace(db, user)
        t1 = _thread(db, user, ws)
        t2 = _thread(db, user, ws)
        t3 = _thread(db, user, ws)
        login_as(user)
        resp = client.delete(f"/workspaces/{ws.id}")
        assert resp.status_code == 200
        # Threads should still exist but be unassigned
        assert t1.workspace_id is None
        assert t2.workspace_id is None
        assert t3.workspace_id is None

    def test_delete_already_deleted(self, app, db, client, login_as):
        user = _user(db)
        ws = _workspace(db, user)
        login_as(user)
        client.delete(f"/workspaces/{ws.id}")
        resp = client.delete(f"/workspaces/{ws.id}")
        assert resp.status_code == 404
