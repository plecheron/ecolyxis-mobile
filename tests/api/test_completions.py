"""Tests for POST /v1/chat/completions endpoint."""
import json
import hashlib
import time
from unittest.mock import patch, MagicMock
import pytest
from app.models import ApiKey, Wallet, Transaction, ApiUsage


BASIC_MESSAGES = [{"role": "user", "content": "Hello"}]
MOCK_LLM_RESPONSE = {
    "choices": [{"message": {"role": "assistant", "content": "Hi there!"}, "finish_reason": "stop"}],
    "usage": {"prompt_tokens": 10, "completion_tokens": 5},
}


def _auth_header(key):
    return {"Authorization": f"Bearer {key}"}


class TestAuth:
    def test_no_api_key_returns_401(self, client, db):
        resp = client.post("/v1/chat/completions", json={"messages": BASIC_MESSAGES})
        assert resp.status_code == 401

    def test_invalid_key_returns_401(self, client, db, make_api_key):
        resp = client.post(
            "/v1/chat/completions",
            json={"messages": BASIC_MESSAGES},
            headers=_auth_header("invalid-key"),
        )
        assert resp.status_code == 401

    def test_revoked_key_returns_401(self, client, db, make_api_key):
        raw_key, api_key, wallet, user = make_api_key()
        api_key.active = False
        db.session.commit()

        resp = client.post(
            "/v1/chat/completions",
            json={"messages": BASIC_MESSAGES},
            headers=_auth_header(raw_key),
        )
        assert resp.status_code == 401


class TestInputValidation:
    @patch("app.api.completions.http_requests.post")
    def test_valid_sync_request(self, mock_post, client, db, make_api_key):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = MOCK_LLM_RESPONSE
        mock_resp.raise_for_status = MagicMock()
        mock_post.return_value = mock_resp

        raw_key, *_ = make_api_key()
        resp = client.post(
            "/v1/chat/completions",
            json={"messages": BASIC_MESSAGES},
            headers=_auth_header(raw_key),
        )
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["object"] == "chat.completion"
        assert data["choices"][0]["message"]["content"] == "Hi there!"
        assert data["usage"]["total_tokens"] == 15

    def test_missing_body_returns_400(self, client, db, make_api_key):
        raw_key, *_ = make_api_key()
        resp = client.post(
            "/v1/chat/completions",
            headers=_auth_header(raw_key),
            content_type="application/json",
        )
        assert resp.status_code == 400

    def test_missing_messages_returns_400(self, client, db, make_api_key):
        raw_key, *_ = make_api_key()
        resp = client.post(
            "/v1/chat/completions",
            json={"model": "ecolyxis-standard"},
            headers=_auth_header(raw_key),
        )
        assert resp.status_code == 400

    def test_empty_messages_returns_400(self, client, db, make_api_key):
        raw_key, *_ = make_api_key()
        resp = client.post(
            "/v1/chat/completions",
            json={"messages": []},
            headers=_auth_header(raw_key),
        )
        assert resp.status_code == 400

    def test_invalid_role_returns_400(self, client, db, make_api_key):
        raw_key, *_ = make_api_key()
        resp = client.post(
            "/v1/chat/completions",
            json={"messages": [{"role": "hacker", "content": "hi"}]},
            headers=_auth_header(raw_key),
        )
        assert resp.status_code == 400

    def test_tool_message_without_call_id_returns_400(self, client, db, make_api_key):
        raw_key, *_ = make_api_key()
        resp = client.post(
            "/v1/chat/completions",
            json={"messages": [{"role": "tool", "content": "result"}]},
            headers=_auth_header(raw_key),
        )
        assert resp.status_code == 400

    def test_valid_tool_message_passes(self, client, db, make_api_key):
        """Tool messages WITH tool_call_id should be accepted (up to LLM)."""
        raw_key, *_ = make_api_key()
        with patch("app.api.completions.http_requests.post") as mock_post:
            mock_resp = MagicMock()
            mock_resp.status_code = 200
            mock_resp.json.return_value = MOCK_LLM_RESPONSE
            mock_resp.raise_for_status = MagicMock()
            mock_post.return_value = mock_resp

            resp = client.post(
                "/v1/chat/completions",
                json={"messages": [
                    {"role": "user", "content": "go"},
                    {"role": "assistant", "tool_calls": [{"id": "tc1", "type": "function", "function": {"name": "f", "arguments": "{}"}}]},
                    {"role": "tool", "tool_call_id": "tc1", "content": "done"},
                ]},
                headers=_auth_header(raw_key),
            )
            assert resp.status_code == 200


class TestImageValidation:
    def _b64_image(self, size_bytes):
        """Generate a minimal base64 image string of approximate decoded size."""
        import base64
        # Pad to approximate size (base64 inflates ~4/3)
        raw = b"\xff" * size_bytes
        return f"data:image/png;base64,{base64.b64encode(raw).decode()}"

    def test_image_over_20mb_returns_400(self, client, db, make_api_key):
        raw_key, *_ = make_api_key()
        big_img = self._b64_image(21 * 1024 * 1024)
        resp = client.post(
            "/v1/chat/completions",
            json={"messages": [{"role": "user", "content": [
                {"type": "text", "text": "describe"},
                {"type": "image_url", "image_url": {"url": big_img}},
            ]}]},
            headers=_auth_header(raw_key),
        )
        assert resp.status_code == 400
        assert "20MB" in resp.get_json()["error"]["message"]

    def test_too_many_images_returns_400(self, client, db, make_api_key):
        raw_key, *_ = make_api_key()
        small_img = self._b64_image(100)
        content = [{"type": "text", "text": "describe"}]
        for _ in range(5):
            content.append({"type": "image_url", "image_url": {"url": small_img}})
        resp = client.post(
            "/v1/chat/completions",
            json={"messages": [{"role": "user", "content": content}]},
            headers=_auth_header(raw_key),
        )
        assert resp.status_code == 400
        assert "Too many images" in resp.get_json()["error"]["message"]

    def test_invalid_base64_returns_400(self, client, db, make_api_key):
        raw_key, *_ = make_api_key()
        resp = client.post(
            "/v1/chat/completions",
            json={"messages": [{"role": "user", "content": [
                {"type": "text", "text": "describe"},
                {"type": "image_url", "image_url": {"url": "data:image/png;base64,!!!invalid!!!"}},
            ]}]},
            headers=_auth_header(raw_key),
        )
        assert resp.status_code == 400


class TestRateLimiting:
    def test_rate_limit_returns_429(self, client, db, make_api_key):
        """Exhaust the token bucket then verify 429."""
        raw_key, *_ = make_api_key()
        with patch("app.api.completions.http_requests.post") as mock_post:
            mock_resp = MagicMock()
            mock_resp.status_code = 200
            mock_resp.json.return_value = MOCK_LLM_RESPONSE
            mock_resp.raise_for_status = MagicMock()
            mock_post.return_value = mock_resp

            # Exhaust bucket (60 messages/min limit)
            for _ in range(65):
                client.post(
                    "/v1/chat/completions",
                    json={"messages": BASIC_MESSAGES},
                    headers=_auth_header(raw_key),
                )

        # Next request should be rate limited
        resp = client.post(
            "/v1/chat/completions",
            json={"messages": BASIC_MESSAGES},
            headers=_auth_header(raw_key),
        )
        assert resp.status_code == 429


class TestStreaming:
    def test_streaming_response_format(self, client, db, make_api_key):
        raw_key, *_ = make_api_key()
        sse_chunks = [
            b'data: {"choices":[{"delta":{"content":"Hello"}}],"usage":null}\n\n',
            b'data: {"choices":[{"delta":{"content":" world"}}],"usage":null}\n\n',
            b'data: {"choices":[{"delta":{}}],"usage":{"prompt_tokens":5,"completion_tokens":2}}\n\n',
            b"data: [DONE]\n\n",
        ]

        with patch("app.api.completions.http_requests.post") as mock_post:
            mock_resp = MagicMock()
            mock_resp.status_code = 200
            mock_resp.raise_for_status = MagicMock()
            mock_resp.iter_lines.return_value = iter(sse_chunks)
            mock_post.return_value = mock_resp

            resp = client.post(
                "/v1/chat/completions",
                json={"messages": BASIC_MESSAGES, "stream": True},
                headers=_auth_header(raw_key),
            )
            assert resp.status_code == 200
            assert resp.content_type.startswith("text/event-stream")

            # Collect all SSE data
            data = resp.get_data(as_text=True)
            assert "Hello" in data
            assert "world" in data
            assert "[DONE]" in data


class TestBillingIntegration:
    @patch("app.api.completions.http_requests.post")
    def test_sync_request_debits_wallet(self, mock_post, app, client, db, make_api_key):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = MOCK_LLM_RESPONSE
        mock_resp.raise_for_status = MagicMock()
        mock_post.return_value = mock_resp

        raw_key, api_key, wallet, user = make_api_key(balance_pence=100_000)
        resp = client.post(
            "/v1/chat/completions",
            json={"messages": BASIC_MESSAGES},
            headers=_auth_header(raw_key),
        )
        assert resp.status_code == 200

        with app.app_context():
            w = db.session.get(Wallet, wallet.id)
            assert w.balance_pence < 100_000
            # Usage record created
            usage = ApiUsage.query.filter_by(api_key_id=api_key.id).one()
            assert usage.tokens_prompt == 10
            assert usage.tokens_completion == 5
            # Transaction created
            txn = Transaction.query.filter_by(wallet_id=wallet.id, type="usage").one()
            assert txn.amount_pence < 0

    @patch("app.api.completions.http_requests.post")
    def test_response_includes_billing_headers(self, mock_post, client, db, make_api_key):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = MOCK_LLM_RESPONSE
        mock_resp.raise_for_status = MagicMock()
        mock_post.return_value = mock_resp

        raw_key, *_ = make_api_key()
        resp = client.post(
            "/v1/chat/completions",
            json={"messages": BASIC_MESSAGES},
            headers=_auth_header(raw_key),
        )
        assert resp.status_code == 200
        assert "X-RateLimit-Tokens-Used" in resp.headers
        assert "X-Billing-Balance-Remaining" in resp.headers


class TestModelAliases:
    @patch("app.api.completions.http_requests.post")
    def test_long_mode_sends_header(self, mock_post, client, db, make_api_key):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = MOCK_LLM_RESPONSE
        mock_resp.raise_for_status = MagicMock()
        mock_post.return_value = mock_resp

        raw_key, *_ = make_api_key()
        resp = client.post(
            "/v1/chat/completions",
            json={"messages": BASIC_MESSAGES, "model": "ecolyxis-long"},
            headers=_auth_header(raw_key),
        )
        assert resp.status_code == 200
        # Verify the LLM backend was called with the right header
        call_headers = mock_post.call_args[1].get("headers", {})
        assert call_headers.get("X-Context-Mode") == "long"

    @patch("app.api.completions.http_requests.post")
    def test_standard_mode_no_special_header(self, mock_post, client, db, make_api_key):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = MOCK_LLM_RESPONSE
        mock_resp.raise_for_status = MagicMock()
        mock_post.return_value = mock_resp

        raw_key, *_ = make_api_key()
        resp = client.post(
            "/v1/chat/completions",
            json={"messages": BASIC_MESSAGES, "model": "ecolyxis-standard"},
            headers=_auth_header(raw_key),
        )
        assert resp.status_code == 200
        call_headers = mock_post.call_args[1].get("headers", {})
        assert "X-Context-Mode" not in call_headers


class TestToolCalls:
    @patch("app.api.completions.http_requests.post")
    def test_tools_forwarded_to_llm(self, mock_post, client, db, make_api_key):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "choices": [{"message": {"role": "assistant", "content": None, "tool_calls": [
                {"id": "tc1", "type": "function", "function": {"name": "get_weather", "arguments": '{"city":"London"}'}}
            ]}, "finish_reason": "tool_calls"}],
            "usage": {"prompt_tokens": 20, "completion_tokens": 10},
        }
        mock_resp.raise_for_status = MagicMock()
        mock_post.return_value = mock_resp

        raw_key, *_ = make_api_key()
        tools = [{"type": "function", "function": {"name": "get_weather", "parameters": {}}}]
        resp = client.post(
            "/v1/chat/completions",
            json={"messages": BASIC_MESSAGES, "tools": tools, "tool_choice": "auto"},
            headers=_auth_header(raw_key),
        )
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["choices"][0]["message"]["tool_calls"] is not None

        # Verify tools were forwarded to LLM
        payload = mock_post.call_args[1]["json"]
        assert payload["tools"] == tools
        assert payload["tool_choice"] == "auto"
