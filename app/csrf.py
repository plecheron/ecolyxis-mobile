"""Lightweight CSRF protection without external dependencies.

Generates a per-session token and validates it. Enforcement is global via the
``before_request`` hook in :mod:`app` (see ``_check_csrf``); these helpers back
that hook and the ``csrf_token()`` Jinja global.
"""
import hmac
import secrets

from flask import session


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
