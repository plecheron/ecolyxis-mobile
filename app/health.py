import os

import requests
from flask import Blueprint, jsonify, current_app
from sqlalchemy import text

from app import db

health_bp = Blueprint("health", __name__)


@health_bp.route("/health")
def check():
    status = {"status": "ok", "checks": {}}

    try:
        db.session.execute(text("SELECT 1"))
        status["checks"]["database"] = "ok"
    except Exception as e:
        status["checks"]["database"] = f"error: {e}"
        status["status"] = "degraded"

    api_url = (
        os.environ.get("ECOLYXIS_API_URL")
        or current_app.config.get("ECOLYXIS_API_URL")
        or ""
    ).rstrip("/")
    if api_url:
        try:
            resp = requests.get(f"{api_url}/health", timeout=5)
            if resp.ok:
                status["checks"]["gpu_api"] = "ok"
            else:
                status["checks"]["gpu_api"] = f"error: HTTP {resp.status_code}"
                status["status"] = "degraded"
        except Exception as e:
            status["checks"]["gpu_api"] = f"error: {e}"
            status["status"] = "degraded"
    else:
        try:
            resp = requests.get(
                current_app.config["LLM_BASE_URL"].rstrip("/") + "/models",
                timeout=5,
            )
            if resp.ok:
                status["checks"]["llm_api"] = "ok"
            else:
                status["checks"]["llm_api"] = f"error: HTTP {resp.status_code}"
                status["status"] = "degraded"
        except Exception as e:
            status["checks"]["llm_api"] = f"error: {e}"
            status["status"] = "degraded"

    try:
        from app.redis_client import get_redis
        get_redis().ping()
        status["checks"]["redis"] = "ok"
    except Exception as e:
        status["checks"]["redis"] = f"error: {e}"
        status["status"] = "degraded"

    code = 200 if status["status"] == "ok" else 503
    return jsonify(status), code
