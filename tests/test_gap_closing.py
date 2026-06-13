"""Coverage gap tests: api/routes, health, webauthn, worker."""
import json
import time
import base64
from datetime import datetime, timezone
from unittest.mock import patch, MagicMock, PropertyMock

import pytest
import requests as req_mod
from app.models import User, Thread, Message, GenerationJob, WebAuthnCredential, Wallet


# ═══════════════════════════════════════════════════════════════
# api/routes.py — GET /v1/models, GET /v1/balance
# ═══════════════════════════════════════════════════════════════

class TestApiRoutes:
    """API routes use a real Bearer token validated via sha256 lookup.
    We seed a genuine ApiKey row and pass the real token."""

    _TOKEN = "ecolyx_testtoken1234567890"

    def _seed_key(self, db):
        from app.models import Wallet, ApiKey
        user = User(username=f"api-{int(time.time()*1000)}", email=f"api-{int(time.time())}@test.com", password_hash="x")
        db.session.add(user)
        db.session.flush()
        wallet = Wallet(user_id=user.id, balance_pence=50000)
        db.session.add(wallet)
        key = ApiKey(
            user_id=user.id,
            name="test",
            key_hash=ApiKey.hash_token(self._TOKEN),
            key_prefix=self._TOKEN[-4:],
        )
        db.session.add(key)
        db.session.commit()
        return key

    def test_list_models_unauthorized(self, app, client):
        resp = client.get("/v1/models")
        assert resp.status_code == 401

    def test_get_balance_unauthorized(self, app, client):
        resp = client.get("/v1/balance")
        assert resp.status_code == 401

    def test_list_models_authorized(self, app, db, client):
        self._seed_key(db)
        resp = client.get("/v1/models", headers={"Authorization": f"Bearer {self._TOKEN}"})
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["object"] == "list"

    def test_get_balance_authorized(self, app, db, client):
        self._seed_key(db)
        resp = client.get("/v1/balance", headers={"Authorization": f"Bearer {self._TOKEN}"})
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["balance_pence"] == 50000


# ═══════════════════════════════════════════════════════════════
# health.py — _check_backend + /health endpoint
# ═══════════════════════════════════════════════════════════════

class TestHealth:
    def test_check_backend_ok(self):
        from app.health import _check_backend
        with patch("app.health.requests.get") as mock_get:
            mock_resp = MagicMock()
            mock_resp.ok = True
            mock_resp.json.return_value = {"status": "ready"}
            mock_get.return_value = mock_resp
            assert _check_backend("http://gpu:8000") == "ok"

    def test_check_backend_loading(self):
        from app.health import _check_backend
        with patch("app.health.requests.get") as mock_get:
            mock_resp = MagicMock()
            mock_resp.ok = True
            mock_resp.json.return_value = {"status": "loading"}
            mock_get.return_value = mock_resp
            assert _check_backend("http://gpu:8000") == "loading"

    def test_check_backend_error_status(self):
        from app.health import _check_backend
        with patch("app.health.requests.get") as mock_get:
            mock_resp = MagicMock()
            mock_resp.ok = True
            mock_resp.json.return_value = {"status": "error"}
            mock_get.return_value = mock_resp
            result = _check_backend("http://gpu:8000")
            assert "error" in result

    def test_check_backend_http_error(self):
        from app.health import _check_backend
        with patch("app.health.requests.get") as mock_get:
            mock_resp = MagicMock()
            mock_resp.ok = False
            mock_resp.status_code = 503
            mock_get.return_value = mock_resp
            result = _check_backend("http://gpu:8000")
            assert "503" in result

    def test_check_backend_connection_refused(self):
        from app.health import _check_backend
        with patch("app.health.requests.get", side_effect=req_mod.ConnectionError("refused")):
            result = _check_backend("http://gpu:8000")
            assert "connection refused" in result

    def test_check_backend_timeout(self):
        from app.health import _check_backend
        with patch("app.health.requests.get", side_effect=req_mod.Timeout("timed out")):
            result = _check_backend("http://gpu:8000")
            assert "timeout" in result

    def test_check_backend_generic_error(self):
        from app.health import _check_backend
        with patch("app.health.requests.get", side_effect=ValueError("weird")):
            result = _check_backend("http://gpu:8000")
            assert "error" in result

    def test_health_endpoint_all_ok(self, app, client):
        with patch("app.health.requests.get") as mock_get, \
             patch("app.redis_client.get_redis") as mock_redis:
            mock_resp = MagicMock()
            mock_resp.ok = True
            mock_resp.json.return_value = {"status": "ready"}
            mock_resp.status_code = 200
            mock_get.return_value = mock_resp
            r = MagicMock()
            r.ping.return_value = True
            r.scan_iter.return_value = iter([b"worker:host-123-t0:alive"])
            r.ttl.return_value = 15
            mock_redis.return_value = r
            resp = client.get("/health")
            assert resp.status_code == 200

    def test_health_endpoint_db_fail(self, app, db, client):
        with patch("app.health.requests.get") as mock_get, \
             patch("app.redis_client.get_redis") as mock_redis, \
             patch("app.health.db.session.execute", side_effect=Exception("DB down")):
            mock_resp = MagicMock()
            mock_resp.ok = True
            mock_resp.json.return_value = {"status": "ready"}
            mock_get.return_value = mock_resp
            r = MagicMock()
            r.ping.return_value = True
            r.scan_iter.return_value = iter([])
            mock_redis.return_value = r
            resp = client.get("/health")
            assert resp.status_code == 503
            assert resp.get_json()["status"] == "degraded"

    def test_health_endpoint_redis_fail(self, app, client):
        with patch("app.health.requests.get") as mock_get, \
             patch("app.redis_client.get_redis") as mock_redis:
            mock_resp = MagicMock()
            mock_resp.ok = True
            mock_resp.json.return_value = {"status": "ready"}
            mock_get.return_value = mock_resp
            mock_redis.side_effect = Exception("Redis down")
            resp = client.get("/health")
            assert resp.status_code == 503
            data = resp.get_json()
            assert "error" in data["checks"]["redis"]


# ═══════════════════════════════════════════════════════════════
# auth/webauthn.py — full endpoint coverage
# ═══════════════════════════════════════════════════════════════

class TestWebAuthn:
    """WebAuthn endpoints. verify_* functions are imported locally inside
    each view, so we patch webauthn.verify_* at the source module."""

    def _make_user(self, db):
        u = User(username="wapass", email="wa@test.com", password_hash="x")
        db.session.add(u)
        db.session.commit()
        return u

    def test_register_begin(self, app, db, client, login_as):
        user = self._make_user(db)
        login_as(user)
        resp = client.post("/webauthn/register-begin")
        assert resp.status_code == 200
        data = resp.get_json()
        assert "challenge" in data

    def test_register_finish_no_challenge(self, app, db, client, login_as):
        user = self._make_user(db)
        login_as(user)
        resp = client.post("/webauthn/register-finish",
                          json={"credential": {}, "name": "Test"})
        assert resp.status_code == 400

    def test_register_finish_with_mock(self, app, db, client, login_as):
        user = self._make_user(db)
        login_as(user)
        with client.session_transaction() as sess:
            sess["webauthn_reg_challenge"] = "dGVzdA"

        with patch("webauthn.verify_registration_response") as mock_verify:
            mock_verification = MagicMock()
            mock_verification.credential_id = b"cred-id-123"
            mock_verification.credential_public_key = b"pub-key"
            mock_verification.sign_count = 0
            mock_verify.return_value = mock_verification

            resp = client.post("/webauthn/register-finish",
                              json={"credential": {"id": "x", "transports": ["internal"]}, "name": "MacBook"})
            assert resp.status_code == 200
            data = resp.get_json()
            assert data["success"] is True
            assert data["name"] == "MacBook"

    def test_register_finish_duplicate(self, app, db, client, login_as):
        user = self._make_user(db)
        login_as(user)
        existing = WebAuthnCredential(
            user_id=user.id, credential_id=b"dup-id", public_key=b"pk",
            sign_count=0, name="Existing"
        )
        db.session.add(existing)
        db.session.commit()

        with client.session_transaction() as sess:
            sess["webauthn_reg_challenge"] = "dGVzdA"

        with patch("webauthn.verify_registration_response") as mock_verify:
            mock_verification = MagicMock()
            mock_verification.credential_id = b"dup-id"
            mock_verification.credential_public_key = b"pub-key"
            mock_verification.sign_count = 0
            mock_verify.return_value = mock_verification

            resp = client.post("/webauthn/register-finish",
                              json={"credential": {"id": "x"}, "name": "Dup"})
            assert resp.status_code == 409

    def test_register_finish_verification_fails(self, app, db, client, login_as):
        user = self._make_user(db)
        login_as(user)
        with client.session_transaction() as sess:
            sess["webauthn_reg_challenge"] = "dGVzdA"

        with patch("webauthn.verify_registration_response",
                      side_effect=ValueError("bad attestation")):
            resp = client.post("/webauthn/register-finish",
                              json={"credential": {"id": "x"}, "name": "Bad"})
            assert resp.status_code == 400

    def test_authenticate_begin(self, app, client):
        resp = client.post("/webauthn/authenticate-begin")
        assert resp.status_code == 200
        data = resp.get_json()
        assert "challenge" in data
        assert data["allowCredentials"] == []

    def test_authenticate_finish_no_challenge(self, app, client):
        resp = client.post("/webauthn/authenticate-finish",
                          json={"credential": {"id": "x"}})
        assert resp.status_code == 400

    def test_authenticate_finish_unknown_cred(self, app, client):
        with client.session_transaction() as sess:
            sess["webauthn_auth_challenge"] = "dGVzdA"
        valid_b64 = base64.urlsafe_b64encode(b"unknown-cred").decode().rstrip("=")
        resp = client.post("/webauthn/authenticate-finish",
                          json={"credential": {"id": valid_b64}})
        assert resp.status_code == 404

    def test_authenticate_finish_success(self, app, db, client):
        user = self._make_user(db)
        cred = WebAuthnCredential(
            user_id=user.id, credential_id=b"valid-cred",
            public_key=b"pub-key", sign_count=0, name="Test Key"
        )
        db.session.add(cred)
        db.session.commit()

        cred_b64 = base64.urlsafe_b64encode(b"valid-cred").decode().rstrip("=")
        with client.session_transaction() as sess:
            sess["webauthn_auth_challenge"] = "dGVzdA"

        with patch("webauthn.verify_authentication_response") as mock_verify:
            mock_result = MagicMock()
            mock_result.new_sign_count = 1
            mock_verify.return_value = mock_result

            resp = client.post("/webauthn/authenticate-finish",
                              json={"credential": {"id": cred_b64, "rawId": cred_b64,
                                                    "response": {"authenticatorData": "x"},
                                                    "type": "public-key"}})
            assert resp.status_code == 200
            data = resp.get_json()
            assert data["success"] is True
            assert data["username"] == "wapass"

    def test_authenticate_finish_verification_fails(self, app, db, client):
        user = self._make_user(db)
        cred = WebAuthnCredential(
            user_id=user.id, credential_id=b"bad-cred",
            public_key=b"pub-key", sign_count=0, name="Bad"
        )
        db.session.add(cred)
        db.session.commit()

        cred_b64 = base64.urlsafe_b64encode(b"bad-cred").decode().rstrip("=")
        with client.session_transaction() as sess:
            sess["webauthn_auth_challenge"] = "dGVzdA"

        with patch("webauthn.verify_authentication_response",
                      side_effect=ValueError("bad sig")):
            resp = client.post("/webauthn/authenticate-finish",
                              json={"credential": {"id": cred_b64}})
            assert resp.status_code == 401

    def test_list_credentials(self, app, db, client, login_as):
        user = self._make_user(db)
        login_as(user)
        cred = WebAuthnCredential(
            user_id=user.id, credential_id=b"list-id",
            public_key=b"pk", sign_count=0, name="My Key"
        )
        db.session.add(cred)
        db.session.commit()
        resp = client.get("/webauthn/credentials")
        assert resp.status_code == 200
        data = resp.get_json()
        assert len(data) == 1
        assert data[0]["name"] == "My Key"

    def test_list_credentials_empty(self, app, db, client, login_as):
        user = self._make_user(db)
        login_as(user)
        resp = client.get("/webauthn/credentials")
        assert resp.status_code == 200
        assert resp.get_json() == []

    def test_delete_credential(self, app, db, client, login_as):
        user = self._make_user(db)
        login_as(user)
        cred = WebAuthnCredential(
            user_id=user.id, credential_id=b"del-id",
            public_key=b"pk", sign_count=0, name="Delete Me"
        )
        db.session.add(cred)
        db.session.commit()
        resp = client.delete(f"/webauthn/credentials/{cred.id}")
        assert resp.status_code == 200
        assert resp.get_json()["success"] is True

    def test_delete_credential_not_found(self, app, db, client, login_as):
        user = self._make_user(db)
        login_as(user)
        resp = client.delete("/webauthn/credentials/99999")
        assert resp.status_code == 404


# ═══════════════════════════════════════════════════════════════
# jobs/worker.py — run_job, _requeue_dead, _new_worker_id
# ═══════════════════════════════════════════════════════════════

class TestWorker:
    def _make_job(self, db, kind="chat", status="queued"):
        # Need a user and thread to satisfy NOT NULL constraints
        user = User(username=f"worker-test-{time.time()}", email="wt@test.com", password_hash="x")
        db.session.add(user)
        db.session.flush()
        thread = Thread(user_id=user.id, title="Worker Test")
        db.session.add(thread)
        db.session.flush()
        job = GenerationJob(
            kind=kind,
            status=status,
            is_premium=False,
            user_id=user.id,
            thread_id=thread.id,
        )
        db.session.add(job)
        db.session.commit()
        return job

    def test_new_worker_id(self):
        from app.jobs.worker import _new_worker_id
        wid = _new_worker_id()
        assert isinstance(wid, str)
        assert "-" in wid

    def test_run_job_missing(self, app):
        import app.jobs.worker as wmod
        with app.app_context():
            wmod.run_job(app, "test-wid", 99999)

    def test_run_job_already_terminal(self, app, db):
        import app.jobs.worker as wmod
        with app.app_context():
            job = self._make_job(db, status="done")
            wmod.run_job(app, "test-wid", job.id)
            db.session.expire_all()
            updated = db.session.get(GenerationJob, job.id)
            assert updated.status == "done"

    def test_run_job_success(self, app, db):
        import app.jobs.worker as wmod
        with app.app_context():
            job = self._make_job(db, kind="chat", status="queued")
            mock_handler = MagicMock(return_value={"text": "Hello!"})
            with patch.dict(wmod.HANDLERS, {"chat": mock_handler}):
                with patch.object(wmod, "publish_event"):
                    with patch.object(wmod, "expire_events"):
                        wmod.run_job(app, "test-wid", job.id)
            db.session.expire_all()
            updated = db.session.get(GenerationJob, job.id)
            assert updated.status == "done"
            assert updated.result is not None

    def test_run_job_error(self, app, db):
        import app.jobs.worker as wmod
        with app.app_context():
            job = self._make_job(db, kind="chat", status="queued")
            mock_handler = MagicMock(side_effect=RuntimeError("GPU offline"))
            with patch.dict(wmod.HANDLERS, {"chat": mock_handler}):
                with patch.object(wmod, "publish_event"):
                    with patch.object(wmod, "expire_events"):
                        wmod.run_job(app, "test-wid", job.id)
            db.session.expire_all()
            updated = db.session.get(GenerationJob, job.id)
            assert updated.status == "error"
            assert "GPU offline" in updated.error

    def test_run_job_no_handler(self, app, db):
        import app.jobs.worker as wmod
        with app.app_context():
            job = self._make_job(db, kind="unknown_kind", status="queued")
            with patch.object(wmod, "publish_event"):
                with patch.object(wmod, "expire_events"):
                    wmod.run_job(app, "test-wid", job.id)
            db.session.expire_all()
            updated = db.session.get(GenerationJob, job.id)
            assert updated.status == "error"
            assert "no handler" in updated.error

    def test_requeue_dead_reenqueues(self, app, db):
        import app.jobs.worker as wmod
        with app.app_context():
            job = self._make_job(db, kind="chat", status="running")
            job.worker_id = "dead-worker-t0"
            db.session.commit()

            with patch.object(wmod, "get_redis") as mock_redis_fn, \
                 patch.object(wmod, "worker_is_alive", return_value=False), \
                 patch.object(wmod, "enqueue") as mock_enqueue:
                r = MagicMock()
                pkey = f"{wmod.PROCESSING_PREFIX}dead-worker-t0"
                r.scan_iter.return_value = iter([pkey.encode()])
                r.rpop.side_effect = [job.id, None]
                mock_redis_fn.return_value = r
                wmod._requeue_dead(app)
                mock_enqueue.assert_called_once()
                db.session.expire_all()
                updated = db.session.get(GenerationJob, job.id)
                assert updated.status == "queued"
                assert updated.worker_id is None

    def test_requeue_dead_skips_alive(self, app, db):
        import app.jobs.worker as wmod
        with app.app_context():
            with patch.object(wmod, "get_redis") as mock_redis_fn, \
                 patch.object(wmod, "worker_is_alive", return_value=True):
                r = MagicMock()
                pkey = f"{wmod.PROCESSING_PREFIX}alive-worker-t0"
                r.scan_iter.return_value = iter([pkey.encode()])
                mock_redis_fn.return_value = r
                wmod._requeue_dead(app)
                r.rpop.assert_not_called()

    def test_requeue_dead_skips_terminal(self, app, db):
        import app.jobs.worker as wmod
        with app.app_context():
            job = self._make_job(db, kind="chat", status="done")
            job.worker_id = "dead-worker-t0"
            db.session.commit()

            with patch.object(wmod, "get_redis") as mock_redis_fn, \
                 patch.object(wmod, "worker_is_alive", return_value=False), \
                 patch.object(wmod, "enqueue") as mock_enqueue:
                r = MagicMock()
                pkey = f"{wmod.PROCESSING_PREFIX}dead-worker-t0"
                r.scan_iter.return_value = iter([pkey.encode()])
                r.rpop.side_effect = [job.id, None]
                mock_redis_fn.return_value = r
                wmod._requeue_dead(app)
                mock_enqueue.assert_not_called()
