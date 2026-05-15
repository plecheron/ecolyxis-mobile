import json
import time
import threading
import math
from datetime import datetime, timezone, timedelta
from functools import wraps
from flask import Blueprint, request, jsonify, Response, current_app
from app.models import ApiKey, ApiUsage, User, Wallet
from app import db

api_bp = Blueprint("api", __name__, url_prefix="/v1")

# --- Pricing ---
PRICE_PER_MTOK = 278  # pence per million tokens (£2.78)

# --- Model aliases -> proxy modes ---
MODEL_ALIASES = {
    "ecolyxis-standard": "standard",
    "ecolyxis-long": "long",
    "ecolyxis-vision": "vision",
    "ecolyxis-precise": "precise",
    "ecolyxis-quick": "quick",
}

# --- In-memory rate limiter ---
_rate_lock = threading.Lock()
_rate_buckets = {}  # key_hash -> {"tokens": float, "last": float}

RATE_REQUESTS_PER_MIN = 30
RATE_MESSAGES_PER_MIN = 60
DAILY_TOKEN_CAP = 100_000_000


def _check_rate_limit(key_hash, limit, window=60):
    """Token bucket rate limiter. Returns (allowed, remaining, retry_after)."""
    now = time.time()
    with _rate_lock:
        bucket = _rate_buckets.get(key_hash)
        if not bucket:
            bucket = {"tokens": float(limit), "last": now}
            _rate_buckets[key_hash] = bucket

        elapsed = now - bucket["last"]
        bucket["tokens"] = min(float(limit), bucket["tokens"] + elapsed * (limit / window))
        bucket["last"] = now

        if bucket["tokens"] >= 1.0:
            bucket["tokens"] -= 1.0
            remaining = int(bucket["tokens"])
            return True, remaining, 0
        else:
            retry_after = int((1.0 - bucket["tokens"]) * (window / limit)) + 1
            return False, 0, retry_after


def _get_daily_usage(api_key_id):
    """Get total tokens used today for an API key."""
    since = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    row = db.session.query(
        db.func.coalesce(db.func.sum(ApiUsage.tokens_prompt + ApiUsage.tokens_completion), 0)
    ).filter(ApiUsage.api_key_id == api_key_id, ApiUsage.created_at >= since).scalar()
    return row or 0


def _tokens_to_pence(tokens):
    """Convert token count to cost in pence (£2.78 per million tokens)."""
    return math.ceil(tokens * PRICE_PER_MTOK / 1_000_000)


def _get_or_create_wallet(user_id):
    """Get or create a wallet for the user."""
    w = Wallet.query.filter_by(user_id=user_id).first()
    if not w:
        w = Wallet(user_id=user_id)
        db.session.add(w)
        db.session.flush()
    return w


def authenticate_api(f):
    """Decorator: validates Bearer token, loads user, checks wallet balance."""
    @wraps(f)
    def decorated(*args, **kwargs):
        auth = request.headers.get("Authorization", "")
        if not auth.startswith("Bearer "):
            return jsonify({"error": {"message": "Missing Authorization header. Use: Bearer ecolyx_...", "type": "auth_error"}}), 401

        token = auth[7:].strip()
        if not token.startswith("ecolyx_"):
            return jsonify({"error": {"message": "Invalid API key format", "type": "auth_error"}}), 401

        key_hash = ApiKey.hash_token(token)
        api_key = ApiKey.query.filter_by(key_hash=key_hash, active=True).first()
        if not api_key:
            return jsonify({"error": {"message": "Invalid or revoked API key", "type": "auth_error"}}), 401

        user = User.query.get(api_key.user_id)
        if not user:
            return jsonify({"error": {"message": "User not found", "type": "auth_error"}}), 401

        # Check wallet balance — need at least 1p credit to make requests
        wallet = _get_or_create_wallet(user.id)
        if wallet.balance_pence <= 0:
            return jsonify({
                "error": {
                    "message": "Insufficient API credits. Top up at https://ecolyxis.co.uk/wallet",
                    "type": "insufficient_credits"
                }
            }), 402

        # Update last_used_at
        api_key.last_used_at = datetime.now(timezone.utc)
        db.session.commit()

        request._api_key = api_key
        request._api_user = user
        request._wallet = wallet
        return f(*args, **kwargs)
    return decorated


def rate_limit(f):
    """Rate limit decorator for API endpoints."""
    @wraps(f)
    def decorated(*args, **kwargs):
        api_key = getattr(request, "_api_key", None)
        if not api_key:
            return jsonify({"error": {"message": "Not authenticated", "type": "auth_error"}}), 401

        allowed, remaining, retry_after = _check_rate_limit(api_key.key_hash, RATE_REQUESTS_PER_MIN)
        if not allowed:
            resp = jsonify({"error": {"message": f"Rate limit exceeded. Retry after {retry_after}s.", "type": "rate_limit_error"}})
            resp.headers["Retry-After"] = str(retry_after)
            return resp, 429

        # Check daily token cap
        daily = _get_daily_usage(api_key.id)
        if daily >= DAILY_TOKEN_CAP:
            return jsonify({"error": {"message": f"Daily token limit ({DAILY_TOKEN_CAP:,}) reached. Resets at midnight UTC.", "type": "rate_limit_error"}}), 429

        return f(*args, **kwargs)
    return decorated


def _rate_headers(api_key, wallet):
    """Build rate limit + billing response headers."""
    daily = _get_daily_usage(api_key.id)
    return {
        "X-RateLimit-Remaining": "0",
        "X-RateLimit-Limit": str(RATE_REQUESTS_PER_MIN),
        "X-RateLimit-Tokens-Used": str(daily),
        "X-RateLimit-Tokens-Cap": str(DAILY_TOKEN_CAP),
        "X-Billing-Balance-Remaining": f"{wallet.balance:.2f}",
    }


@api_bp.route("/models", methods=["GET"])
@authenticate_api
def list_models():
    """OpenAI-compatible GET /v1/models"""
    api_key = request._api_key
    wallet = request._wallet
    base_model = current_app.config.get("LLM_MODEL", "ecolyxis-default")
    now_ts = int(datetime.now(timezone.utc).timestamp())
    models_list = [{"id": base_model, "object": "model", "created": now_ts, "owned_by": "ecolyxis"}]
    for alias in MODEL_ALIASES:
        models_list.append({"id": alias, "object": "model", "created": now_ts, "owned_by": "ecolyxis"})
    result = jsonify({"object": "list", "data": models_list})
    for k, v in _rate_headers(api_key, wallet).items():
        result.headers[k] = v
    return result


@api_bp.route("/balance", methods=["GET"])
@authenticate_api
def get_balance():
    """GET /v1/balance — return current credit balance."""
    wallet = request._wallet
    return jsonify({
        "balance_gbp": wallet.balance,
        "balance_pence": wallet.balance_pence,
        "price_per_mtok_gbp": PRICE_PER_MTOK / 100,
        "url": "https://ecolyxis.co.uk/wallet",
    })


@api_bp.route("/chat/completions", methods=["POST"])
@authenticate_api
def chat_completions():
    """OpenAI-compatible POST /v1/chat/completions"""
    api_key = request._api_key
    user = request._api_user
    wallet = request._wallet

    # Rate limit for message endpoint
    allowed, remaining, retry_after = _check_rate_limit(api_key.key_hash, RATE_MESSAGES_PER_MIN)
    if not allowed:
        resp = jsonify({"error": {"message": f"Rate limit exceeded. Retry after {retry_after}s.", "type": "rate_limit_error"}})
        resp.headers["Retry-After"] = str(retry_after)
        return resp, 429

    # Parse request
    data = request.get_json(silent=True)
    if not data:
        return jsonify({"error": {"message": "Invalid JSON body", "type": "invalid_request_error"}}), 400

    messages = data.get("messages")
    if not messages or not isinstance(messages, list):
        return jsonify({"error": {"message": "'messages' is required and must be an array", "type": "invalid_request_error"}}), 400

    stream = data.get("stream", False)
    model = data.get("model", current_app.config.get("LLM_MODEL", "ecolyxis-default"))
    # Resolve mode from model alias
    mode = MODEL_ALIASES.get(model)
    max_tokens = data.get("max_tokens", 2048)
    temperature = data.get("temperature", 0.7)

    # Validate message roles — accept standard roles + tool calling roles
    # Also validate image content if present
    import base64
    MAX_IMAGE_SIZE = 20 * 1024 * 1024  # 20MB per image
    MAX_IMAGES_PER_REQUEST = 4
    total_images = 0

    valid_roles = ("system", "user", "assistant", "tool")
    for msg in messages:
        role = msg.get("role")
        if role not in valid_roles:
            return jsonify({"error": {"message": f"Invalid role: {role}", "type": "invalid_request_error"}}), 400
        # Tool messages must have a tool_call_id
        if role == "tool" and not msg.get("tool_call_id"):
            return jsonify({"error": {"message": "Tool messages must include 'tool_call_id'", "type": "invalid_request_error"}}), 400

        # Validate image content (OpenAI vision format)
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
                        # Base64 inline image — check size
                        try:
                            b64_data = url.split(",", 1)[1]
                            decoded_size = len(base64.b64decode(b64_data, validate=True))
                            if decoded_size > MAX_IMAGE_SIZE:
                                return jsonify({"error": {"message": f"Image exceeds {MAX_IMAGE_SIZE // 1024 // 1024}MB limit ({decoded_size // 1024 // 1024}MB)", "type": "invalid_request_error"}}), 400
                        except (IndexError, Exception) as e:
                            return jsonify({"error": {"message": f"Invalid base64 image data: {str(e)}", "type": "invalid_request_error"}}), 400

    if total_images > MAX_IMAGES_PER_REQUEST:
        return jsonify({"error": {"message": f"Too many images ({total_images}). Maximum {MAX_IMAGES_PER_REQUEST} per request.", "type": "invalid_request_error"}}), 400

    # Check daily token cap (pre-flight estimate)
    daily = _get_daily_usage(api_key.id)
    if daily >= DAILY_TOKEN_CAP:
        return jsonify({"error": {"message": f"Daily token limit ({DAILY_TOKEN_CAP:,}) reached.", "type": "rate_limit_error"}}), 429

    # Build LLM request
    llm_url = current_app.config["LLM_BASE_URL"] + "/chat/completions"
    llm_payload = {
        "model": current_app.config["LLM_MODEL"],
        "messages": messages,
        "max_tokens": max_tokens,
        "temperature": temperature,
        "chat_template_kwargs": {"enable_thinking": False},
    }

    import requests as http_requests
    import uuid

    completion_id = f"chatcmpl-{uuid.uuid4().hex[:24]}"
    created = int(time.time())

    # Capture app ref + wallet id while inside request context
    app = current_app._get_current_object()
    wallet_id = wallet.id

    # Handle precise mode (multi-stage)
    if mode == "precise":
        from app.llm import LLMClient
        client = LLMClient(
            base_url=current_app.config["LLM_BASE_URL"],
            model=current_app.config["LLM_MODEL"],
            system_prompt="",
            max_history=999,
        )
        from app.chat import _run_precise
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

    # Set X-Context-Mode header for proxy mode switching
    llm_headers = {}
    if mode and mode != "standard":
        llm_headers["X-Context-Mode"] = mode

    # Pass through tools / tool_choice if provided
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


def _build_message(backing_message):
    """Build an OpenAI-compatible message dict, including tool_calls when present."""
    msg = {"role": backing_message.get("role", "assistant")}
    content = backing_message.get("content")
    if content is not None:
        msg["content"] = content
    else:
        msg["content"] = None
    tool_calls = backing_message.get("tool_calls")
    if tool_calls:
        msg["tool_calls"] = tool_calls
    return msg


def _sync_response(llm_url, payload, completion_id, created, model, api_key, wallet_id, max_tokens, app, llm_headers=None):
    """Non-streaming completion."""
    import requests as http_requests

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

    # Log usage + debit wallet
    _log_usage_and_debit(app, api_key.id, wallet_id, "/v1/chat/completions", model, prompt_tokens, completion_tokens)

    # Refresh wallet for headers
    with app.app_context():
        wallet = Wallet.query.get(wallet_id)
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
    import requests as http_requests

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

        # Log usage + debit wallet after streaming completes
        _log_usage_and_debit(app, api_key.id, wallet_id, "/v1/chat/completions", model, total_prompt, total_completion)

    wallet = Wallet.query.get(wallet_id)
    headers = _rate_headers(api_key, wallet)
    headers["X-Accel-Buffering"] = "no"
    headers["Cache-Control"] = "no-cache"
    headers["Content-Type"] = "text/event-stream"

    return Response(generate(), mimetype="text/event-stream", headers=headers)


def _log_usage_and_debit(app, api_key_id, wallet_id, endpoint, model, prompt_tokens, completion_tokens):
    """Log API usage and debit wallet. Runs inside an explicit app context."""
    with app.app_context():
        try:
            usage = ApiUsage(
                api_key_id=api_key_id,
                endpoint=endpoint,
                model=model,
                tokens_prompt=prompt_tokens,
                tokens_completion=completion_tokens,
            )
            db.session.add(usage)

            total_tokens = prompt_tokens + completion_tokens
            if total_tokens > 0:
                cost_pence = _tokens_to_pence(total_tokens)
                wallet = db.session.query(Wallet).with_for_update().filter_by(id=wallet_id).one()
                if wallet.balance_pence >= cost_pence:
                    wallet.balance_pence -= cost_pence
                    from app.models import Transaction
                    txn = Transaction(
                        wallet_id=wallet_id,
                        type="usage",
                        amount_pence=-cost_pence,
                        description=f"API usage: {total_tokens:,} tokens ({prompt_tokens:,} prompt + {completion_tokens:,} completion)",
                        api_key_id=api_key_id,
                    )
                    db.session.add(txn)
                else:
                    app.logger.warning(
                        f"Wallet {wallet_id} insufficient balance for {cost_pence}p debit "
                        f"(had {wallet.balance_pence}p). Tokens: {total_tokens}"
                    )

            db.session.commit()
        except Exception:
            db.session.rollback()
            app.logger.exception("Failed to log usage/debit wallet")
