"""Health endpoint: DB + GPU-API probes (ECOLYXIS_API_URL is set in conftest)."""
from unittest.mock import patch, Mock


def test_health_ok_when_gpu_api_reachable(client, db):
    fake = Mock(ok=True, status_code=200)
    with patch("app.health.requests.get", return_value=fake):
        resp = client.get("/health")
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["status"] == "ok"
    assert body["checks"]["database"] == "ok"
    assert body["checks"]["gpu_api"] == "ok"


def test_health_degraded_when_gpu_api_unreachable(client, db):
    import requests
    with patch("app.health.requests.get", side_effect=requests.ConnectionError("nope")):
        resp = client.get("/health")
    assert resp.status_code == 503
    body = resp.get_json()
    assert body["status"] == "degraded"
    assert "error" in body["checks"]["gpu_api"]


def test_health_degraded_on_gpu_api_5xx(client, db):
    fake = Mock(ok=False, status_code=500)
    with patch("app.health.requests.get", return_value=fake):
        resp = client.get("/health")
    assert resp.status_code == 503
    body = resp.get_json()
    assert body["status"] == "degraded"
    assert "HTTP 500" in body["checks"]["gpu_api"]
