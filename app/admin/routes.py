"""Admin HTTP endpoints — index dashboard + polled JSON endpoints."""
from datetime import datetime, timezone
from flask import render_template, jsonify, request
from flask_login import login_required

from app.admin import admin_bp, admin_required, _metrics_sampler
from app.admin.metrics import (
    _system_stats,
    _app_stats,
    _service_status,
    _llm_health,
    _llm_metrics,
    _llm_error_stats,
    _error_stats,
    _token_chart_data,
    _user_chart_data,
    _top_users,
)
from app.admin.tests import get_last_run as _get_last_test_run


@admin_bp.route("/")
@login_required
@admin_required
def index():
    sys_stats = _system_stats()
    app_stats = _app_stats()
    llm = _llm_health()

    services = {
        "ecolyxis (gunicorn)": _service_status("ecolyxis"),
        "caddy": _service_status("caddy"),
    }

    return render_template(
        "admin/index.html",
        sys=sys_stats,
        app=app_stats,
        llm=llm,
        services=services,
        errors=_error_stats(),
        llm_errors=_llm_error_stats(),
        token_chart=_token_chart_data(30),
        user_chart=_user_chart_data(30),
        top_users=_top_users(20),
        recent_api_requests=app_stats.get("recent_api_requests", []),
        test_run=_get_last_test_run(),
        now=datetime.now(timezone.utc),
    )


@admin_bp.route("/api/llm-metrics")
@login_required
@admin_required
def api_llm_metrics():
    """Live LLM metrics — polled every 2s by the dashboard."""
    return jsonify(_llm_metrics())


@admin_bp.route("/api/errors")
@login_required
@admin_required
def api_errors():
    """Error stats — polled every 30s."""
    return jsonify(_error_stats())


@admin_bp.route("/api/llm-errors")
@login_required
@admin_required
def api_llm_errors():
    """LLM backend error stats."""
    return jsonify(_llm_error_stats())


@admin_bp.route("/api/stats")
@login_required
@admin_required
def api_stats():
    """JSON endpoint for live refresh."""
    app_stats = _app_stats()
    users_list = []
    for u in app_stats.pop("recent_users"):
        users_list.append({
            "username": u.username,
            "email": u.email,
            "tier": u.tier,
            "created_at": u.created_at.isoformat() if u.created_at else None,
        })
    app_stats["recent_users"] = users_list

    for m in app_stats["recent_messages"]:
        if isinstance(m.get("created_at"), datetime):
            m["created_at"] = m["created_at"].isoformat()

    for r in app_stats.get("recent_api_requests", []):
        if isinstance(r.get("created_at"), datetime):
            r["created_at"] = r["created_at"].isoformat()

    return jsonify({
        "system": _system_stats(),
        "app": app_stats,
        "llm": _llm_health(),
        "services": {
            "ecolyxis (gunicorn)": _service_status("ecolyxis"),
            "caddy": _service_status("caddy"),
        },
        "timestamp": datetime.now(timezone.utc).isoformat(),
    })


@admin_bp.route("/api/llm-history")
@login_required
@admin_required
def api_llm_history():
    """Historical LLM gen speed for charts."""
    minutes = request.args.get("minutes", "60", type=str)
    seconds_map = {"1": 60, "60": 3600, "1440": 86400}
    seconds = seconds_map.get(minutes, 3600)
    raw = _metrics_sampler.get_range(seconds)
    points = []
    for ts, gen_tps, prompt_tps, proc, deferred in raw:
        points.append({
            "t": round(ts),
            "gen": gen_tps,
            "prompt": prompt_tps,
            "proc": proc,
            "deferred": deferred,
        })
    return jsonify({"points": points})


@admin_bp.route("/tests")
@login_required
@admin_required
def tests_page():
    """Dedicated test suite page."""
    return render_template(
        "admin/tests.html",
        test_run=_get_last_test_run(),
    )
