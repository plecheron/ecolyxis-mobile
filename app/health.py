import os

import requests
from flask import Blueprint, jsonify, current_app
from sqlalchemy import text

from app import db

health_bp = Blueprint("health", __name__)


def _check_backend(url, timeout=5):
    """Check a GPU backend's /health endpoint. Returns status string."""
    try:
        resp = requests.get(f"{url}/health", timeout=timeout)
        if not resp.ok:
            return f"error: HTTP {resp.status_code}"
        data = resp.json()
        # Backends report {"status": "ready"|"loading"|"error", "mode": ...}
        backend_status = data.get("status", "unknown")
        if backend_status == "ready":
            return "ok"
        elif backend_status == "loading":
            return "loading"
        else:
            return f"error: backend status={backend_status}"
    except requests.ConnectionError:
        return "error: connection refused"
    except requests.Timeout:
        return "error: timeout"
    except Exception as e:
        return f"error: {e}"


@health_bp.route("/health")
def check():
    status = {"status": "ok", "checks": {}}

    # --- Database ---
    try:
        db.session.execute(text("SELECT 1"))
        status["checks"]["database"] = "ok"
    except Exception as e:
        status["checks"]["database"] = f"error: {e}"
        status["status"] = "degraded"

    # --- LLM / API ---
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

    # --- Redis ---
    try:
        from app.redis_client import get_redis
        get_redis().ping()
        status["checks"]["redis"] = "ok"
    except Exception as e:
        status["checks"]["redis"] = f"error: {e}"
        status["status"] = "degraded"

    # --- GPU generation backends ---
    backends = {
        "image": os.environ.get("HIDREAM_URL", current_app.config.get("HIDREAM_URL", "")),
        "video": os.environ.get("WAN22_URL", current_app.config.get("WAN22_URL", "")),
        "edit": os.environ.get("EDIT_URL", current_app.config.get("EDIT_URL", "")),
    }
    for kind, url in backends.items():
        if not url:
            continue
        result = _check_backend(url)
        status["checks"][f"generation_{kind}"] = result
        # Only degrade if there's a hard error — 'loading' is informational
        if result.startswith("error:"):
            status["status"] = "degraded"

    # --- Worker liveness ---
    try:
        from app.redis_client import get_redis
        r = get_redis()
        # Check for active worker heartbeats
        alive_keys = list(r.scan_iter("worker:alive:*"))
        worker_count = 0
        for key in alive_keys:
            ttl = r.ttl(key)
            if ttl and ttl > 0:
                worker_count += 1
        if worker_count > 0:
            status["checks"]["worker"] = f"ok ({worker_count} alive)"
        else:
            status["checks"]["worker"] = "error: no live workers"
            status["status"] = "degraded"
    except Exception as e:
        status["checks"]["worker"] = f"error: {e}"

    code = 200 if status["status"] == "ok" else 503
    return jsonify(status), code
