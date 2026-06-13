"""Jobs API client tests: submit_job, get_job, stream_remote_job, config helpers."""
import json
from unittest.mock import patch, MagicMock
import requests
from app.jobs.api_client import submit_job, get_job, stream_remote_job, _cfg, _base_url, _headers


def test_cfg_from_env(app):
    with app.app_context():
        with patch.dict("os.environ", {"TEST_KEY": "val"}):
            assert _cfg("TEST_KEY") == "val"


def test_cfg_missing_raises(app):
    with app.app_context():
        with patch.dict("os.environ", {}, clear=False):
            import os
            os.environ.pop("MISSING_KEY_12345", None)
            try:
                _cfg("MISSING_KEY_12345")
                assert False, "Should have raised RuntimeError"
            except RuntimeError as e:
                assert "MISSING_KEY_12345" in str(e)


def test_base_url(app):
    with app.app_context():
        with patch.dict("os.environ", {"ECOLYXIS_API_URL": "http://api.test/v1"}):
            assert _base_url() == "http://api.test/v1"


def test_base_url_strips_trailing_slash(app):
    with app.app_context():
        with patch.dict("os.environ", {"ECOLYXIS_API_URL": "http://api.test/v1/"}):
            assert _base_url() == "http://api.test/v1"


def test_headers(app):
    with app.app_context():
        with patch.dict("os.environ", {"ECOLYXIS_INTERNAL_TOKEN": "secret123"}):
            h = _headers()
            assert h["X-Ecolyxis-Internal"] == "secret123"


# ─── submit_job ───

def test_submit_job_success(app):
    mock_resp = MagicMock()
    mock_resp.status_code = 202
    mock_resp.json.return_value = {"job_id": "api-job-123"}
    with app.app_context():
        with patch("app.jobs.api_client.requests.post", return_value=mock_resp), \
             patch.dict("os.environ", {"ECOLYXIS_API_URL": "http://api.test", "ECOLYXIS_INTERNAL_TOKEN": "tok"}):
            job_id = submit_job("image", {"prompt": "cat"})
    assert job_id == "api-job-123"


def test_submit_job_with_client_ref(app):
    mock_resp = MagicMock()
    mock_resp.status_code = 202
    mock_resp.json.return_value = {"job_id": "j1"}
    with app.app_context():
        with patch("app.jobs.api_client.requests.post", return_value=mock_resp) as mock_post, \
             patch.dict("os.environ", {"ECOLYXIS_API_URL": "http://api.test", "ECOLYXIS_INTERNAL_TOKEN": "tok"}):
            submit_job("video", {"prompt": "dog"}, client_ref="ref-1")
            payload = mock_post.call_args[1]["json"]
            assert payload["client_ref"] == "ref-1"


def test_submit_job_non_202_raises(app):
    mock_resp = MagicMock()
    mock_resp.status_code = 500
    mock_resp.text = "Internal error"
    with app.app_context():
        with patch("app.jobs.api_client.requests.post", return_value=mock_resp), \
             patch.dict("os.environ", {"ECOLYXIS_API_URL": "http://api.test", "ECOLYXIS_INTERNAL_TOKEN": "tok"}):
            try:
                submit_job("image", {"prompt": "cat"})
                assert False, "Should have raised"
            except RuntimeError as e:
                assert "500" in str(e)


# ─── get_job ───

def test_get_job_success(app):
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {"status": "done", "result": {"url": "http://img.png"}}
    mock_resp.raise_for_status = MagicMock()
    with app.app_context():
        with patch("app.jobs.api_client.requests.get", return_value=mock_resp), \
             patch.dict("os.environ", {"ECOLYXIS_API_URL": "http://api.test", "ECOLYXIS_INTERNAL_TOKEN": "tok"}):
            result = get_job("job-1")
    assert result["status"] == "done"


def test_get_job_raises_on_error(app):
    mock_resp = MagicMock()
    mock_resp.raise_for_status.side_effect = requests.HTTPError("404 Not Found")
    with app.app_context():
        with patch("app.jobs.api_client.requests.get", return_value=mock_resp), \
             patch.dict("os.environ", {"ECOLYXIS_API_URL": "http://api.test", "ECOLYXIS_INTERNAL_TOKEN": "tok"}):
            try:
                get_job("bad-id")
                assert False, "Should have raised"
            except requests.HTTPError:
                pass


# ─── stream_remote_job ───

def test_stream_remote_job_success(app):
    """Verify events are forwarded to publish() and done event returns result."""
    submit_mock = MagicMock(return_value="api-job-1")
    stream_resp = MagicMock()
    stream_resp.status_code = 200
    stream_resp.iter_lines.return_value = iter([
        "data: " + json.dumps({"type": "assigned"}),
        "data: " + json.dumps({"type": "queued"}),
        "data: " + json.dumps({"type": "progress", "value": 0.5}),
        "data: " + json.dumps({"type": "done", "url": "http://result.png"}),
    ])

    published = []
    with app.app_context():
        with patch("app.jobs.api_client.submit_job", submit_mock), \
             patch("app.jobs.api_client.requests.get", return_value=stream_resp), \
             patch.dict("os.environ", {"ECOLYXIS_API_URL": "http://api.test", "ECOLYXIS_INTERNAL_TOKEN": "tok"}):
            result = stream_remote_job("image", {"prompt": "x"}, published.append)

    assert result["type"] == "done"
    assert result["url"] == "http://result.png"
    # assigned/queued should be filtered out, progress + done forwarded
    types = [e["type"] for e in published]
    assert "assigned" not in types
    assert "queued" not in types
    assert "progress" in types
    assert "done" in types


def test_stream_remote_job_error_event(app):
    submit_mock = MagicMock(return_value="api-job-err")
    stream_resp = MagicMock()
    stream_resp.status_code = 200
    stream_resp.iter_lines.return_value = iter([
        "data: " + json.dumps({"type": "error", "message": "GPU OOM"}),
    ])
    with app.app_context():
        with patch("app.jobs.api_client.submit_job", submit_mock), \
             patch("app.jobs.api_client.requests.get", return_value=stream_resp), \
             patch.dict("os.environ", {"ECOLYXIS_API_URL": "http://api.test", "ECOLYXIS_INTERNAL_TOKEN": "tok"}):
            try:
                stream_remote_job("image", {"prompt": "x"}, lambda e: None)
                assert False, "Should have raised"
            except RuntimeError as e:
                assert "GPU OOM" in str(e)


def test_stream_remote_job_non_200(app):
    submit_mock = MagicMock(return_value="api-job-x")
    stream_resp = MagicMock()
    stream_resp.status_code = 500
    with app.app_context():
        with patch("app.jobs.api_client.submit_job", submit_mock), \
             patch("app.jobs.api_client.requests.get", return_value=stream_resp), \
             patch.dict("os.environ", {"ECOLYXIS_API_URL": "http://api.test", "ECOLYXIS_INTERNAL_TOKEN": "tok"}):
            try:
                stream_remote_job("image", {"prompt": "x"}, lambda e: None)
                assert False, "Should have raised"
            except RuntimeError as e:
                assert "500" in str(e)
