"""Admin dashboard integration for the main Ecolyxis app.

This module connects the main app to the admin dashboard via the shared
PostgreSQL database:

- Feature flags: reads from admin_feature_flag table
- User bans: checks is_banned on every authenticated request
- Audit webhook: receives events from the admin dashboard
"""
from functools import wraps
from flask import request, jsonify, redirect, url_for, flash, current_app
from app import db


# ── Feature Flags ────────────────────────────────────────────────────

# Cache for feature flags (in-process, 30s TTL)
_flag_cache = {}
_flag_cache_time = 0
_FLAG_CACHE_TTL = 30


def _read_flags():
    """Read all feature flags from the admin_feature_flag table."""
    import time
    global _flag_cache, _flag_cache_time

    now = time.time()
    if _flag_cache and now - _flag_cache_time < _FLAG_CACHE_TTL:
        return _flag_cache

    try:
        result = db.session.execute(db.text(
            "SELECT key, value FROM admin_feature_flag"
        )).fetchall()
        flags = {row[0]: row[1] for row in result}
        _flag_cache = flags
        _flag_cache_time = now
        return flags
    except Exception:
        return _flag_cache if _flag_cache else {}


def is_feature_enabled(key, default=False):
    """Check if a feature flag is enabled. Cached for 30 seconds."""
    flags = _read_flags()
    return flags.get(key, default)


def feature_required(flag_key, default=False):
    """Decorator that gates a route behind a feature flag.

    Usage:
        @feature_required('video_generation')
        def my_route():
            ...
    """
    def decorator(f):
        @wraps(f)
        def wrapped(*args, **kwargs):
            if not is_feature_enabled(flag_key, default):
                if request.is_json or request.headers.get("Accept", "").startswith("application/json"):
                    return jsonify({"error": "This feature is currently disabled"}), 403
                flash("This feature is currently disabled.", "info")
                return redirect(url_for("dashboard.index"))
            return f(*args, **kwargs)
        return wrapped
    return decorator


def invalidate_flag_cache():
    """Force flag cache to be re-read on next access."""
    global _flag_cache_time
    _flag_cache_time = 0


# ── Ban Enforcement ──────────────────────────────────────────────────

# Paths exempt from ban check
_BAN_EXEMPT_PATHS = {
    "/auth/login", "/auth/logout", "/auth/register",
    "/health", "/landing", "/",
    "/blog", "/contact", "/legal", "/pricing",
}


def check_ban_status():
    """Before-request hook: check if current user is banned."""
    from flask_login import current_user

    if not current_user.is_authenticated:
        return None

    # Exempt static pages and auth
    if request.path in _BAN_EXEMPT_PATHS:
        return None

    # Check if user model has is_banned
    is_banned = getattr(current_user, "is_banned", False)
    if is_banned:
        # Log them out
        from flask_login import logout_user
        logout_user()
        if request.is_json or request.headers.get("Accept", "").startswith("application/json"):
            return jsonify({"error": "Your account has been suspended. Please contact support."}), 403
        flash("Your account has been suspended. Please contact support.", "error")
        return redirect(url_for("auth.login"))

    return None


# ── Audit Webhook Endpoint ───────────────────────────────────────────

def register_audit_endpoint(app):
    """Register the audit webhook endpoint for the admin dashboard."""
    from app.csrf import validate_csrf_token

    @app.route("/admin/audit-ingest", methods=["POST"])
    def audit_ingest():
        """Receive audit events from the admin dashboard.

        Uses an API key for auth (ADMIN_AUDIT_KEY env var).
        """
        import os
        expected_key = os.environ.get("ADMIN_AUDIT_KEY", "")
        if not expected_key:
            return jsonify({"error": "Audit endpoint not configured"}), 503

        auth = request.headers.get("Authorization", "")
        if auth != f"Bearer {expected_key}":
            return jsonify({"error": "Unauthorized"}), 401

        data = request.json or {}
        # Just log it — the admin dashboard stores audit events in its own tables
        app.logger.info(
            "Admin audit event: %s %s %s",
            data.get("action", "?"),
            data.get("target", "?"),
            data.get("detail", ""),
        )
        return jsonify({"status": "received"})
