"""Admin module tests: __init__ (admin_required, _MetricsSampler), routes,
metrics helpers, and tests parser."""
import json
import time
from datetime import datetime, timezone
from unittest.mock import patch, MagicMock

import pytest
from app.models import User, Thread, Message


# ─── Helpers ───

def _make_admin(db, username="admin"):
    u = User(username=username, email=f"{username}@test.com", password_hash="x", is_admin=True)
    db.session.add(u)
    db.session.commit()
    return u

def _make_user(db, username="regular"):
    u = User(username=username, email=f"{username}@test.com", password_hash="x", is_admin=False)
    db.session.add(u)
    db.session.commit()
    return u

def _make_thread(db, user, title="Test"):
    t = Thread(user_id=user.id, title=title)
    db.session.add(t)
    db.session.commit()
    return t

def _make_msg(db, thread, role="user", content="hi", tokens=10):
    m = Message(thread_id=thread.id, role=role, content=content, tokens_used=tokens)
    db.session.add(m)
    db.session.commit()
    return m


# ═══════════════════════════════════════════════════════════════
# admin/__init__.py — admin_required decorator
# ═══════════════════════════════════════════════════════════════

def test_admin_required_allows_admin(app, db, login_as, client):
    user = _make_admin(db)
    login_as(user)
    with patch("app.admin.routes._token_chart_data", return_value=[]), \
         patch("app.admin.routes._user_chart_data", return_value=[]), \
         patch("app.admin.routes._top_users", return_value=[]):
        resp = client.get("/admin/")
    assert resp.status_code == 200


def test_admin_required_blocks_non_admin(app, db, login_as, client):
    user = _make_user(db)
    login_as(user)
    resp = client.get("/admin/")
    assert resp.status_code == 403


def test_admin_required_redirects_anon(app, client):
    resp = client.get("/admin/")
    assert resp.status_code in (302, 403)  # redirect to login or 403


def test_admin_required_allows_admin_username(app, db, login_as, client):
    """User without is_admin flag but with admin username should be allowed."""
    user = User(username="ashley", email="ashley@test.com", password_hash="x", is_admin=False)
    db.session.add(user)
    db.session.commit()
    login_as(user)
    with patch("app.admin.routes._token_chart_data", return_value=[]), \
         patch("app.admin.routes._user_chart_data", return_value=[]), \
         patch("app.admin.routes._top_users", return_value=[]):
        resp = client.get("/admin/")
    assert resp.status_code == 200


# ═══════════════════════════════════════════════════════════════
# admin/__init__.py — _MetricsSampler
# ═══════════════════════════════════════════════════════════════

def test_metrics_sampler_get_range_empty():
    from app.admin import _metrics_sampler
    result = _metrics_sampler.get_range(60)
    assert isinstance(result, list)


def test_metrics_sampler_get_range_with_data():
    from app.admin import _metrics_sampler
    with _metrics_sampler._lock:
        _metrics_sampler._data.append((time.time(), 10.5, 20.0, 1, 0))
    result = _metrics_sampler.get_range(60)
    assert len(result) >= 1
    assert result[-1][1] == 10.5


# ═══════════════════════════════════════════════════════════════
# admin/routes.py — HTTP endpoints
# ═══════════════════════════════════════════════════════════════

def test_admin_index(app, db, login_as, client):
    user = _make_admin(db)
    login_as(user)
    with patch("app.admin.routes._token_chart_data", return_value=[]), \
         patch("app.admin.routes._user_chart_data", return_value=[]), \
         patch("app.admin.routes._top_users", return_value=[]):
        resp = client.get("/admin/")
    assert resp.status_code == 200


def test_admin_api_stats(app, db, login_as, client):
    user = _make_admin(db)
    login_as(user)
    resp = client.get("/admin/api/stats")
    assert resp.status_code == 200
    data = resp.get_json()
    assert "system" in data
    assert "app" in data
    assert "llm" in data


def test_admin_api_llm_metrics(app, db, login_as, client):
    user = _make_admin(db)
    login_as(user)
    resp = client.get("/admin/api/llm-metrics")
    assert resp.status_code == 200
    assert "status" in resp.get_json()


def test_admin_api_errors(app, db, login_as, client):
    user = _make_admin(db)
    login_as(user)
    resp = client.get("/admin/api/errors")
    assert resp.status_code == 200
    data = resp.get_json()
    assert "last_hour" in data


def test_admin_api_llm_errors(app, db, login_as, client):
    user = _make_admin(db)
    login_as(user)
    resp = client.get("/admin/api/llm-errors")
    assert resp.status_code == 200


def test_admin_api_llm_history(app, db, login_as, client):
    user = _make_admin(db)
    login_as(user)
    resp = client.get("/admin/api/llm-history?minutes=1")
    assert resp.status_code == 200
    assert "points" in resp.get_json()


def test_admin_tests_page(app, db, login_as, client):
    user = _make_admin(db)
    login_as(user)
    resp = client.get("/admin/tests")
    assert resp.status_code == 200


def test_admin_non_admin_blocked_all_endpoints(app, db, login_as, client):
    user = _make_user(db)
    login_as(user)
    for path in ["/admin/", "/admin/api/stats", "/admin/api/errors", "/admin/tests"]:
        resp = client.get(path)
        assert resp.status_code == 403, f"{path} should be 403"


# ═══════════════════════════════════════════════════════════════
# admin/metrics.py — helper functions
# ═══════════════════════════════════════════════════════════════

def test_system_stats(app):
    from app.admin.metrics import _system_stats
    with app.app_context():
        stats = _system_stats()
        assert "uptime_seconds" in stats
        assert "memory_total_mb" in stats
        assert "disk_total_gb" in stats


def test_format_uptime():
    from app.admin.metrics import _format_uptime
    assert _format_uptime(0) == "0m"
    assert _format_uptime(3600) == "1h 0m"
    assert _format_uptime(90061) == "1d 1h 1m"


def test_service_status(app):
    from app.admin.metrics import _service_status
    with app.app_context():
        result = _service_status("ecolyxis")
        assert isinstance(result, str)


def test_service_status_unknown(app):
    from app.admin.metrics import _service_status
    with app.app_context():
        result = _service_status("nonexistent-service-xyz")
        assert result == "inactive"


def test_app_stats(app, db):
    from app.admin.metrics import _app_stats
    user = _make_user(db)
    thread = _make_thread(db, user)
    _make_msg(db, thread, tokens=42)
    with app.app_context():
        stats = _app_stats()
        assert stats["total_users"] >= 1
        assert stats["total_messages"] >= 1
        assert stats["total_tokens"] >= 42


def test_app_stats_empty(app, db):
    from app.admin.metrics import _app_stats
    with app.app_context():
        stats = _app_stats()
        assert stats["total_users"] == 0
        assert stats["avg_messages_per_user"] == 0


def test_llm_health_online(app):
    from app.admin.metrics import _llm_health
    with app.app_context():
        with patch("requests.get") as mock_get:
            mock_resp = MagicMock()
            mock_resp.status_code = 200
            mock_resp.json.return_value = {"data": [{"id": "test-model"}]}
            mock_get.return_value = mock_resp
            result = _llm_health()
            assert result["status"] == "online"
            assert "test-model" in result["models"]


def test_llm_health_error(app):
    from app.admin.metrics import _llm_health
    with app.app_context():
        with patch("requests.get") as mock_get:
            mock_resp = MagicMock()
            mock_resp.status_code = 500
            mock_get.return_value = mock_resp
            result = _llm_health()
            assert result["status"] == "error"


def test_llm_health_offline(app):
    from app.admin.metrics import _llm_health
    with app.app_context():
        with patch("requests.get", side_effect=ConnectionError("refused")):
            result = _llm_health()
            assert result["status"] == "offline"


def test_llm_metrics_online(app):
    from app.admin.metrics import _llm_metrics
    with app.app_context():
        with patch("requests.get") as mock_get:
            mock_resp = MagicMock()
            mock_resp.status_code = 200
            mock_resp.text = (
                "# comment line\n"
                "llamacpp:prompt_tokens_seconds 150.5\n"
                "llamacpp:predicted_tokens_seconds 80.3\n"
                "llamacpp:requests_processing 2\n"
            )
            mock_get.return_value = mock_resp
            result = _llm_metrics()
            assert result["status"] == "online"
            assert result["prompt_tokens_per_sec"] == 150.5


def test_llm_metrics_error(app):
    from app.admin.metrics import _llm_metrics
    with app.app_context():
        with patch("requests.get") as mock_get:
            mock_resp = MagicMock()
            mock_resp.status_code = 503
            mock_get.return_value = mock_resp
            result = _llm_metrics()
            assert result["status"] == "error"


def test_error_stats(app):
    from app.admin.metrics import _error_stats
    with app.app_context():
        with patch("subprocess.run") as mock_run:
            mock_result = MagicMock()
            mock_result.stdout = ""
            mock_run.return_value = mock_result
            result = _error_stats()
            assert "last_hour" in result
            assert "recent" in result


def test_llm_error_stats(app):
    from app.admin.metrics import _llm_error_stats
    with app.app_context():
        with patch("subprocess.run") as mock_run:
            mock_result = MagicMock()
            mock_result.stdout = ""
            mock_run.return_value = mock_result
            result = _llm_error_stats()
            assert "last_hour" in result


def test_top_users(app, db):
    from app.admin.metrics import _top_users
    user = _make_user(db)
    thread = _make_thread(db, user)
    _make_msg(db, thread, tokens=500)
    with app.app_context():
        data = _top_users(20)
        assert isinstance(data, list)
        assert len(data) >= 1


# ═══════════════════════════════════════════════════════════════
# admin/metrics.py — chart helpers (date_trunc is PG-only, skip on SQLite)
# ═══════════════════════════════════════════════════════════════

@pytest.mark.skip(reason="date_trunc is PostgreSQL-only, not available in SQLite test DB")
def test_token_chart_data(app, db):
    from app.admin.metrics import _token_chart_data
    user = _make_user(db)
    thread = _make_thread(db, user)
    _make_msg(db, thread, tokens=100)
    with app.app_context():
        data = _token_chart_data(30)
        assert isinstance(data, list)


@pytest.mark.skip(reason="date_trunc is PostgreSQL-only, not available in SQLite test DB")
def test_user_chart_data(app, db):
    from app.admin.metrics import _user_chart_data
    user = _make_user(db)
    thread = _make_thread(db, user)
    _make_msg(db, thread, tokens=10)
    with app.app_context():
        data = _user_chart_data(30)
        assert len(data) == 30


# ═══════════════════════════════════════════════════════════════
# admin/tests.py — pytest output parser
# ═══════════════════════════════════════════════════════════════

def test_parse_pytest_output_all_pass():
    from app.admin.tests import _parse_pytest_output
    stdout = """tests/chat/test_foo.py::test_one PASSED [ 50%]
tests/chat/test_foo.py::test_two PASSED [100%]
============================== 2 passed in 3.50s ==============================
"""
    summary, failures, suites = _parse_pytest_output(stdout)
    assert summary["passed"] == 2
    assert summary["failed"] == 0
    assert summary["duration"] == 3.5
    assert len(failures) == 0
    assert "chat" in suites


def test_parse_pytest_output_with_failures():
    from app.admin.tests import _parse_pytest_output
    stdout = """tests/chat/test_foo.py::test_one PASSED [ 50%]
tests/chat/test_foo.py::test_two FAILED [100%]
FAILED tests/chat/test_foo.py::test_two - assert 1 == 2
============================== 1 failed, 1 passed in 4.0s ==============================
"""
    summary, failures, suites = _parse_pytest_output(stdout)
    assert summary["passed"] == 1
    assert summary["failed"] == 1
    assert len(failures) == 1
    assert "test_two" in failures[0]["test"]


def test_parse_pytest_output_empty():
    from app.admin.tests import _parse_pytest_output
    summary, failures, suites = _parse_pytest_output("")
    assert summary["passed"] == 0
    assert failures == []
    assert suites == {}


def test_parse_pytest_output_skipped_xfail():
    from app.admin.tests import _parse_pytest_output
    stdout = """tests/x/test_a.py::test_a PASSED [ 33%]
tests/x/test_a.py::test_b SKIPPED [ 66%]
tests/x/test_a.py::test_c XFAILED [100%]
============================== 1 passed, 1 skipped, 1 xfailed in 2.0s ==============================
"""
    summary, failures, suites = _parse_pytest_output(stdout)
    assert summary["passed"] == 1
    assert summary["skipped"] == 1
    assert summary["xfailed"] == 1


def test_get_last_run():
    from app.admin.tests import get_last_run
    result = get_last_run()
    assert result is None or isinstance(result, dict)
