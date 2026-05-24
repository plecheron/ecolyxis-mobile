"""Public REST API (`/v1/...`) — OpenAI-compatible.

Shared decorators (authenticate_api, rate_limit), token-bucket rate
limiter state, pricing constants, and the wallet/usage debit helper
live here. Route handlers are in routes.py and completions.py.
"""
import threading
import time
import math
from datetime import datetime, timezone
from functools import wraps
from flask import Blueprint, request, jsonify

from app import db
from app.models import ApiKey, ApiUsage, User, Wallet, Transaction

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

        user = db.session.get(User, api_key.user_id)
        if not user:
            return jsonify({"error": {"message": "User not found", "type": "auth_error"}}), 401

        wallet = _get_or_create_wallet(user.id)
        if wallet.balance_pence <= 0:
            return jsonify({
                "error": {
                    "message": "Insufficient API credits. Top up at https://ecolyxis.co.uk/wallet",
                    "type": "insufficient_credits"
                }
            }), 402

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


def _estimate_tokens(text):
    """Rough server-side token estimate (~4 chars per token)."""
    if not text:
        return 0
    return max(1, len(text) // 4)


def _apply_token_floor(reported, estimated):
    """Use reported tokens, but floor to 50% of estimated if backend under-reports."""
    if reported > 0:
        return reported
    if estimated > 0:
        return max(1, estimated // 2)
    return 0


def _log_usage_and_debit(app, api_key_id, wallet_id, endpoint, model, prompt_tokens, completion_tokens):
    """Log API usage and debit wallet. Runs inside an explicit app context."""
    with app.app_context():
        try:
            prompt_tokens = max(prompt_tokens, 1)  # floor at 1 to avoid zero-cost logging
            completion_tokens = max(completion_tokens, 1)
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
                    txn = Transaction(
                        wallet_id=wallet_id,
                        type="usage",
                        amount_pence=-cost_pence,
                        description=f"API usage: {total_tokens:,} tokens ({prompt_tokens:,} prompt + {completion_tokens:,} completion)",
                        api_key_id=api_key_id,
                    )
                    db.session.add(txn)
                elif wallet.balance_pence > 0:
                    # Partial debit: drain remaining balance rather than giving free inference
                    partial = wallet.balance_pence
                    wallet.balance_pence = 0
                    txn = Transaction(
                        wallet_id=wallet_id,
                        type="usage",
                        amount_pence=-partial,
                        description=f"API usage (partial debit): {total_tokens:,} tokens, charged {partial}p of {cost_pence}p",
                        api_key_id=api_key_id,
                    )
                    db.session.add(txn)
                    app.logger.warning(
                        f"Wallet {wallet_id} partially debited {partial}p of {cost_pence}p "
                        f"for {total_tokens} tokens. Balance now zero."
                    )
                else:
                    app.logger.warning(
                        f"Wallet {wallet_id} zero balance, skipped debit of {cost_pence}p "
                        f"for {total_tokens} tokens (streaming request started before balance exhausted)."
                    )

            db.session.commit()
        except Exception:
            db.session.rollback()
            app.logger.exception("Failed to log usage/debit wallet")


from app.api import routes, completions  # noqa: E402,F401 — register routes
