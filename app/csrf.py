"""Lightweight CSRF protection without external dependencies.

Generates a per-session token, validates it on POST requests.
Exempts API routes (Bearer token auth) and JSON requests with
X-CSRFToken header.
"""
import hashlib
import hmac
import secrets
from functools import wraps

from flask import request, session, jsonify, current_app


def generate_csrf_token():
    """Generate or return existing CSRF token for this session."""
    if "_csrf_token" not in session:
        session["_csrf_token"] = secrets.token_hex(32)
    return session["_csrf_token"]


def validate_csrf_token(token):
    """Constant-time compare of submitted token against session token."""
    expected = session.get("_csrf_token")
    if not expected or not token:
        return False
    return hmac.compare_digest(expected, token)


def csrf_protect(f):
    """Decorator: validate CSRF token on POST/PUT/DELETE/PATCH requests.

    Checks for token in:
    1. X-CSRFToken header (for AJAX/fetch requests)
    2. form field named 'csrf_token' (for form submissions)

    Skips validation for GET/HEAD/OPTIONS.
    """
    @wraps(f)
    def decorated(*args, **kwargs):
        if request.method in ("GET", "HEAD", "OPTIONS"):
            return f(*args, **kwargs)

        # API routes use Bearer token auth — exempt
        if request.path.startswith("/v1/"):
            return f(*args, **kwargs)

        # Check header first (AJAX), then form field
        token = request.headers.get("X-CSRFToken") or request.form.get("csrf_token")

        if not validate_csrf_token(token):
            current_app.logger.warning(
                f"CSRF validation failed for {request.method} {request.path} "
                f"from {request.remote_addr}"
            )
            # For JSON requests, return JSON error
            if request.is_json or request.headers.get("Accept", "").startswith("application/json"):
                return jsonify({"error": "CSRF token validation failed"}), 403
            # For form requests, return simple error
            return "CSRF token validation failed. Please go back and try again.", 403

        return f(*args, **kwargs)
    return decorated
