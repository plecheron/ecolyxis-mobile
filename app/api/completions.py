"""POST /v1/chat/completions — the main inference endpoint.

Supports streaming (SSE) and sync responses, plus a 'precise' mode that
runs a multi-stage plan/generate/refine pipeline via app.chat._run_precise.
"""
import base64
import json
import time
import uuid
from flask import request, jsonify, Response, current_app
import requests as http_requests

from app.models import Wallet
from app.api import (
    api_bp,
    authenticate_api,
    _check_rate_limit,
    _get_daily_usage,
    _log_usage_and_debit,
    _rate_headers,
    MODEL_ALIASES,
    RATE_MESSAGES_PER_MIN,
    DAILY_TOKEN_CAP,
)


MAX_IMAGE_SIZE = 20 * 1024 * 1024  # 20MB per image
MAX_IMAGES_PER_REQUEST = 4


def _build_message(backing_message):
    """Build an OpenAI-compatible message dict, including tool_calls when present."""
    msg = {"role": backing_message.get("role", "assistant")}
    content = backing_message.get("content")
    msg["content"] = content if content is not None else None
    tool_calls = backing_message.get("tool_calls")
    if tool_calls:
        msg["tool_calls"] = tool_calls
    return msg


@api_bp.route("/chat/completions", methods=["POST"])
@authenticate_api
def chat_completions():
    """OpenAI-compatible POST /v1/chat/completions"""
    api_key = request._api_key
    wallet = request._wallet

    allowed, _, retry_after = _check_rate_limit(api_key.key_hash, RATE_MESSAGES_PER_MIN)
    if not allowed:
        resp = jsonify({"error": {"message": f"Rate limit exceeded. Retry after {retry_after}s.", "type": "rate_limit_error"}})
        resp.headers["Retry-After"] = str(retry_after)
        return resp, 429

    data = request.get_json(silent=True)
    if not data:
        return jsonify({"error": {"message": "Invalid JSON body", "type": "invalid_request_error"}}), 400

    messages = data.get("messages")
    if not messages or not isinstance(messages, list):
        return jsonify({"error": {"message": "'messages' is required and must be an array", "type": "invalid_request_error"}}), 400

    stream = data.get("stream", False)
    model = data.get("model", current_app.config.get("LLM_MODEL", "ecolyxis-default"))
    mode = MODEL_ALIASES.get(model)
    max_tokens = data.get("max_tokens", 2048)
    temperature = data.get("temperature", 0.7)

    total_images = 0
    valid_roles = ("system", "user", "assistant", "tool")
    for msg in messages:
        role = msg.get("role")
        if role not in valid_roles:
            return jsonify({"error": {"message": f"Invalid role: {role}", "type": "invalid_request_error"}}), 400
        if role == "tool" and not msg.get("tool_call_id"):
            return jsonify({"error": {"message": "Tool messages must include 'tool_call_id'", "type": "invalid_request_error"}}), 400

        content = msg.get("content")
        if isinstance(content, list):
            for part in content:
                if not isinstance(part, dict):
                    continue
                if part.get("type") == "image_url":
                    total_images += 1
                    image_url = part.get("image_url", {})
                    url = image_url.get("url", "")
                    if url.startswith("data:image/"):
                        try:
                            b64_data = url.split(",", 1)[1]
                            decoded_size = len(base64.b64decode(b64_data, validate=True))
                            if decoded_size > MAX_IMAGE_SIZE:
                                return jsonify({"error": {"message": f"Image exceeds {MAX_IMAGE_SIZE // 1024 // 1024}MB limit ({decoded_size // 1024 // 1024}MB)", "type": "invalid_request_error"}}), 400
                        except (IndexError, Exception) as e:
                            return jsonify({"error": {"message": f"Invalid base64 image data: {str(e)}", "type": "invalid_request_error"}}), 400

    if total_images > MAX_IMAGES_PER_REQUEST:
        return jsonify({"error": {"message": f"Too many images ({total_images}). Maximum {MAX_IMAGES_PER_REQUEST} per request.", "type": "invalid_request_error"}}), 400

    daily = _get_daily_usage(api_key.id)
    if daily >= DAILY_TOKEN_CAP:
        return jsonify({"error": {"message": f"Daily token limit ({DAILY_TOKEN_CAP:,}) reached.", "type": "rate_limit_error"}}), 429

    llm_url = current_app.config["LLM_BASE_URL"] + "/chat/completions"
    llm_payload = {
        "model": current_app.config["LLM_MODEL"],
        "messages": messages,
        "max_tokens": max_tokens,
        "temperature": temperature,
        "chat_template_kwargs": {"enable_thinking": False},
    }

    completion_id = f"chatcmpl-{uuid.uuid4().hex[:24]}"
    created = int(time.time())

    app = current_app._get_current_object()
    wallet_id = wallet.id

    # Precise mode: multi-stage plan/generate/refine
    if mode == "precise":
        from app.llm import LLMClient
        from app.chat import _run_precise
        client = LLMClient(
            base_url=current_app.config["LLM_BASE_URL"],
            model=current_app.config["LLM_MODEL"],
            system_prompt="",
            max_history=999,
        )
        final, prompt_tokens, completion_tokens = _run_precise(client, messages, "standard")
        result = jsonify({
            "id": completion_id,
            "object": "chat.completion",
            "created": created,
            "model": model,
            "choices": [{"index": 0, "message": {"role": "assistant", "content": final}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": prompt_tokens, "completion_tokens": completion_tokens, "total_tokens": prompt_tokens + completion_tokens},
        })
        _log_usage_and_debit(app, api_key.id, wallet_id, "/v1/chat/completions", model, prompt_tokens, completion_tokens)
        for k, v in _rate_headers(api_key, wallet).items():
            result.headers[k] = v
        return result

    llm_headers = {}
    if mode and mode != "standard":
        llm_headers["X-Context-Mode"] = mode

    if data.get("tools"):
        llm_payload["tools"] = data["tools"]
    if data.get("tool_choice"):
        llm_payload["tool_choice"] = data["tool_choice"]

    if stream:
        llm_payload["stream"] = True
        llm_payload["stream_options"] = {"include_usage": True}
        return _stream_response(llm_url, llm_payload, completion_id, created, model, api_key, wallet_id, app, llm_headers)
    else:
        llm_payload["stream"] = False
        return _sync_response(llm_url, llm_payload, completion_id, created, model, api_key, wallet_id, max_tokens, app, llm_headers)


def _sync_response(llm_url, payload, completion_id, created, model, api_key, wallet_id, max_tokens, app, llm_headers=None):
    """Non-streaming completion."""
    try:
        resp = http_requests.post(llm_url, json=payload, timeout=120, headers=llm_headers or {})
        resp.raise_for_status()
        data = resp.json()
    except http_requests.RequestException as e:
        return jsonify({"error": {"message": f"LLM backend error: {str(e)}", "type": "server_error"}}), 502

    choice = data.get("choices", [{}])[0]
    message = choice.get("message", {})
    usage = data.get("usage", {})

    prompt_tokens = usage.get("prompt_tokens", 0)
    completion_tokens = usage.get("completion_tokens", 0)

    _log_usage_and_debit(app, api_key.id, wallet_id, "/v1/chat/completions", model, prompt_tokens, completion_tokens)

    with app.app_context():
        wallet = db.session.get(Wallet, wallet_id)
        headers = _rate_headers(api_key, wallet)

    result = jsonify({
        "id": completion_id,
        "object": "chat.completion",
        "created": created,
        "model": model,
        "choices": [
            {
                "index": 0,
                "message": _build_message(message),
                "finish_reason": choice.get("finish_reason", "stop"),
            }
        ],
        "usage": {
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": prompt_tokens + completion_tokens,
        }
    })
    for k, v in headers.items():
        result.headers[k] = v
    return result


def _stream_response(llm_url, payload, completion_id, created, model, api_key, wallet_id, app, llm_headers=None):
    """Streaming completion using SSE."""
    def generate():
        total_prompt = 0
        total_completion = 0
        try:
            resp = http_requests.post(llm_url, json=payload, stream=True, timeout=120, headers=llm_headers or {})
            resp.raise_for_status()

            for raw_line in resp.iter_lines():
                if not raw_line:
                    continue
                line = raw_line.decode("utf-8") if isinstance(raw_line, bytes) else raw_line
                if not line.startswith("data: "):
                    continue
                data_str = line[6:]
                if data_str.strip() == "[DONE]":
                    usage_chunk = {
                        "id": completion_id,
                        "object": "chat.completion.chunk",
                        "created": created,
                        "model": model,
                        "choices": [],
                        "usage": {
                            "prompt_tokens": total_prompt,
                            "completion_tokens": total_completion,
                            "total_tokens": total_prompt + total_completion,
                        }
                    }
                    yield f"data: {json.dumps(usage_chunk)}\n\n"
                    yield "data: [DONE]\n\n"
                    break

                try:
                    chunk = json.loads(data_str)
                    usage = chunk.get("usage")
                    if usage:
                        total_prompt = usage.get("prompt_tokens", 0)
                        total_completion = usage.get("completion_tokens", 0)
                        continue

                    choice_data = chunk.get("choices", [{}])[0]
                    delta = choice_data.get("delta", {})
                    content = delta.get("content") or delta.get("reasoning_content", "")

                    out_delta = {}
                    if content:
                        out_delta["content"] = content
                    if delta.get("tool_calls"):
                        out_delta["tool_calls"] = delta["tool_calls"]

                    out_chunk = {
                        "id": completion_id,
                        "object": "chat.completion.chunk",
                        "created": created,
                        "model": model,
                        "choices": [
                            {
                                "index": 0,
                                "delta": out_delta,
                                "finish_reason": choice_data.get("finish_reason"),
                            }
                        ]
                    }
                    yield f"data: {json.dumps(out_chunk)}\n\n"
                except json.JSONDecodeError:
                    continue

        except http_requests.RequestException as e:
            error_chunk = {
                "id": completion_id,
                "object": "chat.completion.chunk",
                "created": created,
                "model": model,
                "choices": [{"index": 0, "delta": {"content": f"\n\n⚠️ LLM backend error: {str(e)}"}, "finish_reason": None}]
            }
            yield f"data: {json.dumps(error_chunk)}\n\n"
            yield "data: [DONE]\n\n"

        _log_usage_and_debit(app, api_key.id, wallet_id, "/v1/chat/completions", model, total_prompt, total_completion)

    wallet = db.session.get(Wallet, wallet_id)
    headers = _rate_headers(api_key, wallet)
    headers["X-Accel-Buffering"] = "no"
    headers["Cache-Control"] = "no-cache"
    headers["Content-Type"] = "text/event-stream"

    return Response(generate(), mimetype="text/event-stream", headers=headers)
