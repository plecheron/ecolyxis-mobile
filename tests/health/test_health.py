"""Health endpoint: DB + GPU-API probes + per-kind generation checks."""
from unittest.mock import patch, Mock


def _fake_redis_with_worker():
    """Return a mock redis that reports one alive worker."""
    r = Mock()
    r.ping.return_value = True
    alive_key = Mock()
    r.scan_iter.return_value = [alive_key]
    r.ttl.return_value = 30  # worker heartbeat still valid
    return r


def test_health_ok_when_all_checks_pass(client, db):
    """All backends ready, worker alive."""
    fake_ready = Mock(
        json=lambda: {"status": "ready"},
        ok=True,
        status_code=200,
    )
    fake_redis = _fake_redis_with_worker()
    with patch("app.health.requests.get", return_value=fake_ready), \
         patch("app.redis_client.get_redis", return_value=fake_redis):
        resp = client.get("/health")
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["status"] == "ok"
    assert body["checks"]["database"] == "ok"
    assert body["checks"]["redis"] == "ok"


def test_health_degraded_when_backends_loading(client, db):
    """Backends still loading should report ok (loading is informational, not degraded)."""
    fake_loading = Mock(
        json=lambda: {"status": "loading"},
        ok=True,
        status_code=200,
    )
    fake_redis = _fake_redis_with_worker()
    with patch("app.health.requests.get", return_value=fake_loading), \
         patch("app.redis_client.get_redis", return_value=fake_redis):
        resp = client.get("/health")
    # Loading backends don't cause degraded — only errors do
    body = resp.get_json()
    assert body["status"] == "ok"


def test_health_degraded_when_gpu_api_unreachable(client, db):
    import requests
    fake_redis = _fake_redis_with_worker()
    with patch("app.health.requests.get", side_effect=requests.ConnectionError("nope")), \
         patch("app.redis_client.get_redis", return_value=fake_redis):
        resp = client.get("/health")
    assert resp.status_code == 503
    body = resp.get_json()
    assert body["status"] == "degraded"
