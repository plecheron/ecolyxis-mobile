"""Auth package: signup/login/logout routes plus WebAuthn passkeys.

Module-level _signup_attempts is intentionally kept at package scope so
both routes.py and tests can clear it (it's an in-memory IP rate limit).
"""
import os
import random
import time
from flask import Blueprint, session

auth_bp = Blueprint("auth", __name__)

# WebAuthn configuration (consumed by webauthn.py)
RP_ID = os.environ.get("WEBAUTHN_RP_ID", "ecolyxis.co.uk")
RP_NAME = "Ecolyxis"
RP_ORIGIN = os.environ.get("WEBAUTHN_ORIGIN", "https://ecolyxis.co.uk")

# In-memory IP rate limit tracker {ip: [timestamps]}
_signup_attempts = {}
SIGNUP_RATE_LIMIT = 3       # max signups per IP
SIGNUP_RATE_WINDOW = 3600   # per hour
FORM_MIN_SECONDS = 3        # minimum time to fill form


def _check_ip_rate(ip):
    """Return True if IP is allowed to attempt signup."""
    now = time.time()
    attempts = _signup_attempts.get(ip, [])
    attempts = [t for t in attempts if now - t < SIGNUP_RATE_WINDOW]
    _signup_attempts[ip] = attempts
    return len(attempts) < SIGNUP_RATE_LIMIT


def _record_ip_attempt(ip):
    """Record a signup attempt for this IP."""
    _signup_attempts.setdefault(ip, []).append(time.time())

# Login brute-force protection (#114)
_login_attempts = {}
LOGIN_RATE_LIMIT = 5       # max failed logins per IP
LOGIN_RATE_WINDOW = 900    # 15 minutes
LOGIN_LOCKOUT_TIME = 900   # lockout for 15 minutes

def _check_login_rate(ip):
    """Return True if IP is allowed to attempt login."""
    now = time.time()
    attempts = _login_attempts.get(ip, [])
    attempts = [t for t in attempts if now - t < LOGIN_RATE_WINDOW]
    _login_attempts[ip] = attempts
    return len(attempts) < LOGIN_RATE_LIMIT

def _record_login_failure(ip):
    """Record a failed login attempt for this IP."""
    _login_attempts.setdefault(ip, []).append(time.time())

def _clear_login_attempts(ip):
    """Clear failed login attempts after successful login."""
    _login_attempts.pop(ip, None)



def _generate_captcha():
    """Generate a simple math question + answer, store in session."""
    a = random.randint(1, 12)
    b = random.randint(1, 12)
    ops = [("+", lambda x, y: x + y), ("−", lambda x, y: x - y)]
    op_sym, op_fn = random.choice(ops)
    if op_sym == "−" and a < b:
        a, b = b, a
    answer = op_fn(a, b)
    question = f"{a} {op_sym} {b} = ?"
    session["captcha_answer"] = str(answer)
    session["captcha_time"] = time.time()
    return question


from app.auth import routes, webauthn  # noqa: E402,F401 — register routes
