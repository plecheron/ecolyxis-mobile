"""Lightweight API endpoints: GET /v1/models and GET /v1/balance."""
from datetime import datetime, timezone
from flask import request, jsonify, current_app

from app.api import (
    api_bp,
    authenticate_api,
    _rate_headers,
    MODEL_ALIASES,
    PRICE_PER_MTOK,
)


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
