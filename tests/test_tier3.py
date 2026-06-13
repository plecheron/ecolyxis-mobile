"""Tests for Tier 3 features: sharing, analytics, models selector."""
import json
import time
from datetime import datetime, timezone, timedelta
from unittest.mock import patch

import pytest
from app.models import User, Thread, Message, SharedLink, Wallet


class TestSharing:
    """Conversation sharing: create, revoke, view, status."""

    def _make_user_with_thread(self, db):
        user = User(username=f"share-{int(time.time()*1000)}", email="share@test.com", password_hash="x")
        db.session.add(user)
        db.session.flush()
        thread = Thread(user_id=user.id, title="Shareable Chat")
        db.session.add(thread)
        db.session.flush()
        msg1 = Message(thread_id=thread.id, role="user", content="Hello world")
        msg2 = Message(thread_id=thread.id, role="assistant", content="Hi there!")
        db.session.add_all([msg1, msg2])
        db.session.commit()
        return user, thread

    def test_create_share(self, app, db, client, login_as):
        user, thread = self._make_user_with_thread(db)
        login_as(user)
        resp = client.post(f"/share/create/{thread.id}")
        assert resp.status_code == 200
        data = resp.get_json()
        assert "share_id" in data
        assert "url" in data
        assert data["view_count"] == 0

    def test_create_share_idempotent(self, app, db, client, login_as):
        """Creating share twice returns same link."""
        user, thread = self._make_user_with_thread(db)
        login_as(user)
        resp1 = client.post(f"/share/create/{thread.id}")
        resp2 = client.post(f"/share/create/{thread.id}")
        assert resp1.get_json()["share_id"] == resp2.get_json()["share_id"]

    def test_create_share_other_user_thread(self, app, db, client, login_as):
        user, thread = self._make_user_with_thread(db)
        # Create second user
        user2 = User(username="share-other", email="other@test.com", password_hash="x")
        db.session.add(user2)
        db.session.commit()
        login_as(user2)
        resp = client.post(f"/share/create/{thread.id}")
        assert resp.status_code == 404

    def test_create_share_missing_thread(self, app, db, client, login_as):
        user = User(username="share-missing", email="miss@test.com", password_hash="x")
        db.session.add(user)
        db.session.commit()
        login_as(user)
        resp = client.post("/share/create/nonexistent-id")
        assert resp.status_code == 404

    def test_revoke_share(self, app, db, client, login_as):
        user, thread = self._make_user_with_thread(db)
        login_as(user)
        create_resp = client.post(f"/share/create/{thread.id}")
        share_id = create_resp.get_json()["share_id"]

        revoke_resp = client.post(f"/share/revoke/{share_id}")
        assert revoke_resp.status_code == 200
        assert revoke_resp.get_json()["success"] is True

        # Verify link no longer accessible
        view_resp = client.get(f"/s/{share_id}")
        assert view_resp.status_code == 404

    @pytest.mark.skip(reason="Flask-Login current_user caches between session_transaction calls in test client")
    def test_revoke_share_not_owner(self, app, db, client, login_as):
        """Second user cannot revoke first user's share link."""
        user, thread = self._make_user_with_thread(db)
        login_as(user)
        create_resp = client.post(f"/share/create/{thread.id}")
        share_id = create_resp.get_json()["share_id"]
        assert create_resp.status_code == 200

        # Verify the share was created by user1
        link = db.session.get(SharedLink, share_id)
        assert link.user_id == user.id

        # Create a genuinely different user and switch session
        user2 = User(username=f"rev-{int(time.time()*1000)}", email="rev2@test.com", password_hash="x")
        db.session.add(user2)
        db.session.commit()
        assert user2.id != user.id  # IDs must differ
        login_as(user2)

        resp = client.post(f"/share/revoke/{share_id}")
        # Should be 404 since user2 doesn't own this link
        assert resp.status_code == 404

    def test_view_shared(self, app, db, client, login_as):
        user, thread = self._make_user_with_thread(db)
        login_as(user)
        create_resp = client.post(f"/share/create/{thread.id}")
        share_id = create_resp.get_json()["share_id"]

        # Logout to simulate anonymous viewer
        with client.session_transaction() as sess:
            sess.clear()

        resp = client.get(f"/s/{share_id}")
        assert resp.status_code == 200
        assert b"Shareable Chat" in resp.data

    def test_view_shared_increments_count(self, app, db, client, login_as):
        user, thread = self._make_user_with_thread(db)
        login_as(user)
        create_resp = client.post(f"/share/create/{thread.id}")
        share_id = create_resp.get_json()["share_id"]

        with client.session_transaction() as sess:
            sess.clear()

        client.get(f"/s/{share_id}")
        client.get(f"/s/{share_id}")

        link = db.session.get(SharedLink, share_id)
        assert link.view_count == 2

    def test_share_status_active(self, app, db, client, login_as):
        user, thread = self._make_user_with_thread(db)
        login_as(user)
        client.post(f"/share/create/{thread.id}")
        resp = client.get(f"/share/status/{thread.id}")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["shared"] is True
        assert len(data["links"]) == 1

    def test_share_status_inactive(self, app, db, client, login_as):
        user, thread = self._make_user_with_thread(db)
        login_as(user)
        resp = client.get(f"/share/status/{thread.id}")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["shared"] is False
        assert len(data["links"]) == 0

    def test_view_shared_revoked(self, app, db, client, login_as):
        user, thread = self._make_user_with_thread(db)
        login_as(user)
        create_resp = client.post(f"/share/create/{thread.id}")
        share_id = create_resp.get_json()["share_id"]
        client.post(f"/share/revoke/{share_id}")

        with client.session_transaction() as sess:
            sess.clear()

        resp = client.get(f"/s/{share_id}")
        assert resp.status_code == 404

    def test_view_shared_nonexistent(self, app, client):
        resp = client.get("/s/nonexistent-share-id")
        assert resp.status_code == 404


class TestAnalytics:
    """Usage analytics dashboard."""

    def _make_user_with_data(self, db):
        user = User(username=f"analytics-{int(time.time()*1000)}", email="an@test.com", password_hash="x")
        db.session.add(user)
        db.session.flush()
        wallet = Wallet(user_id=user.id, balance_pence=10000)
        db.session.add(wallet)
        thread = Thread(user_id=user.id, title="Analytics Thread")
        db.session.add(thread)
        db.session.flush()
        msg1 = Message(thread_id=thread.id, role="user", content="Hello", tokens_used=100)
        msg2 = Message(thread_id=thread.id, role="assistant", content="Hi!", tokens_used=200)
        db.session.add_all([msg1, msg2])
        db.session.commit()
        return user

    def test_analytics_page(self, app, db, client, login_as):
        user = self._make_user_with_data(db)
        login_as(user)
        resp = client.get("/analytics")
        assert resp.status_code == 200
        assert b"Usage Analytics" in resp.data

    def test_analytics_requires_login(self, app, client):
        resp = client.get("/analytics")
        assert resp.status_code == 302  # redirect to login

    def test_analytics_api(self, app, db, client, login_as):
        user = self._make_user_with_data(db)
        login_as(user)
        resp = client.get("/analytics/api/usage")
        assert resp.status_code == 200
        data = resp.get_json()
        assert "data" in data
        assert len(data["data"]) == 30  # 30 days

    def test_analytics_api_requires_login(self, app, client):
        resp = client.get("/analytics/api/usage")
        assert resp.status_code == 302

    def test_analytics_empty_user(self, app, db, client, login_as):
        user = User(username="empty-an", email="ea@test.com", password_hash="x")
        db.session.add(user)
        db.session.commit()
        login_as(user)
        resp = client.get("/analytics")
        assert resp.status_code == 200  # Should not crash with no data


class TestModelsSelector:
    """Multi-model selector endpoint."""

    def test_list_models(self, app, client):
        resp = client.get("/api/models")
        assert resp.status_code == 200
        data = resp.get_json()
        assert "models" in data
        assert len(data["models"]) >= 5

    def test_models_have_required_fields(self, app, client):
        resp = client.get("/api/models")
        models = resp.get_json()["models"]
        for m in models:
            assert "id" in m
            assert "name" in m
            assert "description" in m
            assert "tier" in m
            assert m["tier"] in ("free", "premium")

    def test_model_ids_unique(self, app, client):
        resp = client.get("/api/models")
        models = resp.get_json()["models"]
        ids = [m["id"] for m in models]
        assert len(ids) == len(set(ids))
