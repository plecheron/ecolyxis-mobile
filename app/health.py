from flask import Blueprint, jsonify
from app import db
from sqlalchemy import text
import requests

health_bp = Blueprint("health", __name__)


@health_bp.route("/health")
def check():
    status = {"status": "ok", "checks": {}}

    # Database connectivity
    try:
        db.session.execute(text("SELECT 1"))
        status["checks"]["database"] = "ok"
    except Exception as e:
        status["checks"]["database"] = f"error: {e}"
        status["status"] = "degraded"

    # LLM API connectivity
    try:
        from flask import current_app
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

    code = 200 if status["status"] == "ok" else 503
    return jsonify(status), code
