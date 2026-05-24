"""Tests for POST /v1/chat/completions — the main inference endpoint."""
import json
import time
import requests
from unittest.mock import patch, MagicMock

from app.models import ApiKey, ApiUsage, User, Wallet, Transaction


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_auth_header(raw_key):
    return {"Authorization": f"Bearer {raw_key}"}


def _setup_user_with_key(db, balance_pence=10000):
    """Create a user, wallet, and API key. Returns (user, wallet, api_key, raw_token)."""
    raw, hashed, prefix = ApiKey.generate_key()
    user = User(username=f"apiuser_{prefix}", email=f"api_{prefix}@test.com")
    user.set_password("password123")
    db.session.add(user)
    db.session.flush()

    wallet = Wallet(user_id=user.id, balance_pence=balance_pence)
    db.session.add(wallet)
    db.session.flush()

    api_key = ApiKey(
        user_id=user.id,
        name="test-key",
        key_hash=hashed,
        key_prefix=prefix,
    )
    db.session.add(api_key)
    db.session.commit()
    return user, wallet, api_key, raw


def _mock_llm_sync_response(content="Hello!", prompt_tokens=50, completion_tokens=10):
    """Create a mock non-streaming LLM response."""
    resp = MagicMock()
    resp.status_code = 200
    resp.raise_for_status = MagicMock()
    resp.json.return_value = {
        "choices": [{"message": {"role": "assistant", "content": content}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": prompt_tokens, "completion_tokens": completion_tokens},
    }
    return resp


def _mock_llm_stream_response(content="Hello!", prompt_tokens=50, completion_tokens=10):
    """Create a mock streaming LLM response with SSE chunks."""
    resp = MagicMock()
    resp.status_code = 200
    resp.raise_for_status = MagicMock()

    lines = []
    # Content chunk
    chunk = {
        "choices": [{"delta": {"content": content}, "finish_reason": None}],
    }
    lines.append(f"data: {json.dumps(chunk)}".encode())
    # Usage chunk
    usage_chunk = {"usage": {"prompt_tokens": prompt_tokens, "completion_tokens": completion_tokens}}
    lines.append(f"data: {json.dumps(usage_chunk)}".encode())
    # Done
    lines.append(b"data: [DONE]")

    resp.iter_lines.return_value = lines
    return resp


# ---------------------------------------------------------------------------
# Auth tests
# ---------------------------------------------------------------------------

class TestCompletionsAuth:

    def test_missing_auth_header(self, client, db):
        resp = client.post("/v1/chat/completions", json={"messages": [{"role": "user", "content": "hi"}]})
        assert resp.status_code == 401
        data = resp.get_json()
        assert "auth_error" in data["error"]["type"]

    def test_wrong_auth_format(self, client, db):
        resp = client.post(
            "/v1/chat/completions",
            json={"messages": [{"role": "user", "content": "hi"}]},
            headers={"Authorization": "Basic abc123"},
        )
        assert resp.status_code == 401

    def test_invalid_api_key(self, client, db):
        resp = client.post(
            "/v1/chat/completions",
            json={"messages": [{"role": "user", "content": "hi"}]},
            headers=_make_auth_header("ecolyx_invalidkey_invalidkey_invalidkey"),
        )
        assert resp.status_code == 401
        data = resp.get_json()
        assert "Invalid" in data["error"]["message"]

    def test_revoked_api_key(self, client, db):
        _, _, api_key, raw = _setup_user_with_key(db, balance_pence=1000)
        api_key.active = False
        db.session.commit()

        resp = client.post(
            "/v1/chat/completions",
            json={"messages": [{"role": "user", "content": "hi"}]},
            headers=_make_auth_header(raw),
        )
        assert resp.status_code == 401

    def test_non_ecolyx_prefix_rejected(self, client, db):
        resp = client.post(
            "/v1/chat/completions",
            json={"messages": [{"role": "user", "content": "hi"}]},
            headers=_make_auth_header("sk-someopenaikey"),
        )
        assert resp.status_code == 401


# ---------------------------------------------------------------------------
# Wallet / balance tests
# ---------------------------------------------------------------------------

class TestCompletionsWallet:

    def test_zero_balance_returns_402(self, client, db):
        _, _, api_key, raw = _setup_user_with_key(db, balance_pence=0)

        resp = client.post(
            "/v1/chat/completions",
            json={"messages": [{"role": "user", "content": "hi"}]},
            headers=_make_auth_header(raw),
        )
        assert resp.status_code == 402
        data = resp.get_json()
        assert "insufficient_credits" in data["error"]["type"]

    def test_negative_balance_returns_402(self, client, db):
        _, _, _, raw = _setup_user_with_key(db, balance_pence=-100)

        resp = client.post(
            "/v1/chat/completions",
            json={"messages": [{"role": "user", "content": "hi"}]},
            headers=_make_auth_header(raw),
        )
        assert resp.status_code == 402


# ---------------------------------------------------------------------------
# Request validation tests
# ---------------------------------------------------------------------------

class TestCompletionsValidation:

    def test_missing_messages(self, client, db):
        _, _, _, raw = _setup_user_with_key(db)
        resp = client.post(
            "/v1/chat/completions",
            json={"model": "test"},
            headers=_make_auth_header(raw),
        )
        assert resp.status_code == 400
        data = resp.get_json()
        assert "messages" in data["error"]["message"]

    def test_empty_messages(self, client, db):
        _, _, _, raw = _setup_user_with_key(db)
        resp = client.post(
            "/v1/chat/completions",
            json={"messages": []},
            headers=_make_auth_header(raw),
        )
        assert resp.status_code == 400

    def test_invalid_role(self, client, db):
        _, _, _, raw = _setup_user_with_key(db)
        resp = client.post(
            "/v1/chat/completions",
            json={"messages": [{"role": "system_admin", "content": "hi"}]},
            headers=_make_auth_header(raw),
        )
        assert resp.status_code == 400
        data = resp.get_json()
        assert "Invalid role" in data["error"]["message"]

    def test_invalid_json_body(self, client, db):
        _, _, _, raw = _setup_user_with_key(db)
        resp = client.post(
            "/v1/chat/completions",
            data="not json",
            content_type="application/json",
            headers=_make_auth_header(raw),
        )
        assert resp.status_code == 400

    def test_tool_message_without_tool_call_id(self, client, db):
        _, _, _, raw = _setup_user_with_key(db)
        resp = client.post(
            "/v1/chat/completions",
            json={"messages": [{"role": "tool", "content": "result"}]},
            headers=_make_auth_header(raw),
        )
        assert resp.status_code == 400
        data = resp.get_json()
        assert "tool_call_id" in data["error"]["message"]


# ---------------------------------------------------------------------------
# Image validation tests
# ---------------------------------------------------------------------------

class TestCompletionsImages:

    def test_too_many_images(self, client, db):
        _, _, _, raw = _setup_user_with_key(db)
        messages = [{"role": "user", "content": [
            {"type": "text", "text": "describe these"},
        ] + [
            {"type": "image_url", "image_url": {"url": f"data:image/png;base64,aA=="}}
            for _ in range(5)
        ]}]
        resp = client.post(
            "/v1/chat/completions",
            json={"messages": messages},
            headers=_make_auth_header(raw),
        )
        assert resp.status_code == 400
        data = resp.get_json()
        assert "Too many images" in data["error"]["message"]

    def test_image_too_large(self, client, db):
        _, _, _, raw = _setup_user_with_key(db)
        # 21MB of valid base64 data (over 20MB limit)
        # Use padding-safe base64: 'A' * n encodes to 'QQ==' etc, use raw bytes
        import base64 as b64mod
        big_bytes = b'\x00' * (21 * 1024 * 1024)  # 21MB raw bytes
        big_b64 = b64mod.b64encode(big_bytes).decode('ascii')
        messages = [{"role": "user", "content": [
            {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{big_b64}"}}
        ]}]
        resp = client.post(
            "/v1/chat/completions",
            json={"messages": messages},
            headers=_make_auth_header(raw),
        )
        assert resp.status_code == 400
        data = resp.get_json()
        assert "exceeds" in data["error"]["message"].lower() or "limit" in data["error"]["message"].lower()


# ---------------------------------------------------------------------------
# Rate limiting tests
# ---------------------------------------------------------------------------

class TestCompletionsRateLimit:

    def test_rate_limit_headers_on_success(self, client, db):
        _, _, _, raw = _setup_user_with_key(db)
        with patch("app.api.completions.http_requests.post") as mock_post:
            mock_post.return_value = _mock_llm_sync_response()
            resp = client.post(
                "/v1/chat/completions",
                json={"messages": [{"role": "user", "content": "hi"}]},
                headers=_make_auth_header(raw),
            )
            assert resp.status_code == 200
            assert "X-RateLimit-Limit" in resp.headers
            assert "X-Billing-Balance-Remaining" in resp.headers


# ---------------------------------------------------------------------------
# Sync response tests
# ---------------------------------------------------------------------------

class TestCompletionsSync:

    def test_sync_response_format(self, client, db):
        _, _, api_key, raw = _setup_user_with_key(db)

        with patch("app.api.completions.http_requests.post") as mock_post:
            mock_post.return_value = _mock_llm_sync_response(
                content="Test response", prompt_tokens=100, completion_tokens=20
            )
            resp = client.post(
                "/v1/chat/completions",
                json={"messages": [{"role": "user", "content": "hello"}]},
                headers=_make_auth_header(raw),
            )

        assert resp.status_code == 200
        data = resp.get_json()
        assert data["object"] == "chat.completion"
        assert data["choices"][0]["message"]["role"] == "assistant"
        assert data["choices"][0]["message"]["content"] == "Test response"
        assert data["choices"][0]["finish_reason"] == "stop"
        assert data["usage"]["prompt_tokens"] == 100
        assert data["usage"]["completion_tokens"] == 20
        assert data["usage"]["total_tokens"] == 120

    def test_sync_logs_usage(self, client, db):
        _, wallet, api_key, raw = _setup_user_with_key(db, balance_pence=50000)

        with patch("app.api.completions.http_requests.post") as mock_post:
            mock_post.return_value = _mock_llm_sync_response(
                prompt_tokens=200, completion_tokens=50
            )
            resp = client.post(
                "/v1/chat/completions",
                json={"messages": [{"role": "user", "content": "hello"}]},
                headers=_make_auth_header(raw),
            )

        assert resp.status_code == 200

        # Check ApiUsage
        usage = ApiUsage.query.filter_by(api_key_id=api_key.id).first()
        assert usage is not None
        assert usage.tokens_prompt == 200
        assert usage.tokens_completion == 50
        assert usage.endpoint == "/v1/chat/completions"

    def test_sync_debits_wallet(self, client, db):
        _, wallet, api_key, raw = _setup_user_with_key(db, balance_pence=50000)
        initial_balance = wallet.balance_pence

        with patch("app.api.completions.http_requests.post") as mock_post:
            mock_post.return_value = _mock_llm_sync_response(
                prompt_tokens=200, completion_tokens=50
            )
            client.post(
                "/v1/chat/completions",
                json={"messages": [{"role": "user", "content": "hello"}]},
                headers=_make_auth_header(raw),
            )

        from app.api import _tokens_to_pence
        expected_cost = _tokens_to_pence(250)  # 200 + 50

        db.session.refresh(wallet)
        assert wallet.balance_pence == initial_balance - expected_cost

        # Check transaction
        txn = Transaction.query.filter_by(wallet_id=wallet.id).first()
        assert txn is not None
        assert txn.type == "usage"
        assert txn.amount_pence == -expected_cost

    def test_sync_llm_error_returns_502(self, client, db):
        _, _, _, raw = _setup_user_with_key(db)

        with patch("app.api.completions.http_requests.post") as mock_post:
            mock_post.side_effect = requests.ConnectionError("Connection refused")
            resp = client.post(
                "/v1/chat/completions",
                json={"messages": [{"role": "user", "content": "hello"}]},
                headers=_make_auth_header(raw),
            )

        assert resp.status_code == 502


# ---------------------------------------------------------------------------
# Streaming response tests
# ---------------------------------------------------------------------------

class TestCompletionsStreaming:

    def test_streaming_response_format(self, client, db):
        _, _, _, raw = _setup_user_with_key(db)

        with patch("app.api.completions.http_requests.post") as mock_post:
            mock_post.return_value = _mock_llm_stream_response(
                content="Hello world!", prompt_tokens=30, completion_tokens=5
            )
            resp = client.post(
                "/v1/chat/completions",
                json={"messages": [{"role": "user", "content": "hi"}], "stream": True},
                headers=_make_auth_header(raw),
            )

        assert resp.status_code == 200
        assert resp.content_type.startswith("text/event-stream")
        assert resp.headers["X-Accel-Buffering"] == "no"

        # Parse SSE data
        full_text = resp.get_data(as_text=True)
        chunks = [line for line in full_text.strip().split("\n") if line.startswith("data: ")]
        assert len(chunks) >= 2  # at least content chunk + [DONE]

        # First chunk should have content
        first_data = json.loads(chunks[0][6:])
        assert first_data["object"] == "chat.completion.chunk"
        assert first_data["choices"][0]["delta"]["content"] == "Hello world!"
        assert first_data["model"] is not None

        # Last chunk should be [DONE]
        assert chunks[-1] == "data: [DONE]"

    def test_streaming_logs_usage(self, client, db):
        _, _, api_key, raw = _setup_user_with_key(db, balance_pence=50000)

        with patch("app.api.completions.http_requests.post") as mock_post:
            mock_post.return_value = _mock_llm_stream_response(
                prompt_tokens=40, completion_tokens=8
            )
            resp = client.post(
                "/v1/chat/completions",
                json={"messages": [{"role": "user", "content": "hi"}], "stream": True},
                headers=_make_auth_header(raw),
            )

        # Consume the full response to trigger usage logging
        _ = resp.get_data()

        usage = ApiUsage.query.filter_by(api_key_id=api_key.id).first()
        assert usage is not None
        assert usage.tokens_prompt == 40
        assert usage.tokens_completion == 8


# ---------------------------------------------------------------------------
# Model alias tests
# ---------------------------------------------------------------------------

class TestCompletionsModelAliases:

    def test_long_model_sends_context_header(self, client, db):
        _, _, _, raw = _setup_user_with_key(db)

        with patch("app.api.completions.http_requests.post") as mock_post:
            mock_post.return_value = _mock_llm_sync_response()
            client.post(
                "/v1/chat/completions",
                json={"messages": [{"role": "user", "content": "hi"}], "model": "ecolyxis-long"},
                headers=_make_auth_header(raw),
            )

        mock_post.assert_called_once()
        call_kwargs = mock_post.call_args
        headers = call_kwargs[1].get("headers") or call_kwargs.kwargs.get("headers") or {}
        if headers is None:
            headers = {}
        # Should have X-Context-Mode: long
        assert headers.get("X-Context-Mode") == "long"

    def test_standard_model_no_extra_header(self, client, db):
        _, _, _, raw = _setup_user_with_key(db)

        with patch("app.api.completions.http_requests.post") as mock_post:
            mock_post.return_value = _mock_llm_sync_response()
            client.post(
                "/v1/chat/completions",
                json={"messages": [{"role": "user", "content": "hi"}], "model": "some-model"},
                headers=_make_auth_header(raw),
            )

        mock_post.assert_called_once()
        call_kwargs = mock_post.call_args
        headers = call_kwargs[1].get("headers") or {}
        if headers is None:
            headers = {}
        assert "X-Context-Mode" not in headers

    def test_vision_model_sends_context_header(self, client, db):
        _, _, _, raw = _setup_user_with_key(db)

        with patch("app.api.completions.http_requests.post") as mock_post:
            mock_post.return_value = _mock_llm_sync_response()
            client.post(
                "/v1/chat/completions",
                json={"messages": [{"role": "user", "content": "hi"}], "model": "ecolyxis-vision"},
                headers=_make_auth_header(raw),
            )

        mock_post.assert_called_once()
        call_kwargs = mock_post.call_args
        headers = call_kwargs[1].get("headers") or {}
        if headers is None:
            headers = {}
        assert headers.get("X-Context-Mode") == "vision"


# ---------------------------------------------------------------------------
# Tool call forwarding tests
# ---------------------------------------------------------------------------

class TestCompletionsToolCalls:

    def test_tools_forwarded_to_llm(self, client, db):
        _, _, _, raw = _setup_user_with_key(db)

        with patch("app.api.completions.http_requests.post") as mock_post:
            mock_post.return_value = _mock_llm_sync_response()
            client.post(
                "/v1/chat/completions",
                json={
                    "messages": [{"role": "user", "content": "use a tool"}],
                    "tools": [{"type": "function", "function": {"name": "get_weather", "parameters": {}}}],
                    "tool_choice": "auto",
                },
                headers=_make_auth_header(raw),
            )

        mock_post.assert_called_once()
        payload = mock_post.call_args[1].get("json") or mock_post.call_args.kwargs.get("json")
        assert "tools" in payload
        assert payload["tools"][0]["function"]["name"] == "get_weather"
        assert payload["tool_choice"] == "auto"


# ---------------------------------------------------------------------------
# Daily token cap test
# ---------------------------------------------------------------------------

class TestCompletionsDailyCap:

    def test_daily_cap_exceeded(self, client, db):
        _, _, api_key, raw = _setup_user_with_key(db, balance_pence=100000)

        # Pre-populate usage at the daily cap
        from app.api import DAILY_TOKEN_CAP
        usage = ApiUsage(
            api_key_id=api_key.id,
            endpoint="/v1/chat/completions",
            model="test",
            tokens_prompt=DAILY_TOKEN_CAP,
            tokens_completion=0,
        )
        db.session.add(usage)
        db.session.commit()

        resp = client.post(
            "/v1/chat/completions",
            json={"messages": [{"role": "user", "content": "hi"}]},
            headers=_make_auth_header(raw),
        )
        assert resp.status_code == 429
        data = resp.get_json()
        assert "Daily token limit" in data["error"]["message"]
