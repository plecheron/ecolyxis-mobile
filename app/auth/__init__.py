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
    """Generate a varied anti-bot challenge, store answer in session.

    Rotates between four formats so bots can't pattern-match a single
    equation type:
      1. Two-step arithmetic: a * b + c = ?
      2. Reverse question:   ? + a = b
      3. Letter extraction:  "What is letter 3 of 'sunflower'?"
      4. Mixed operations:   a + b * c = ? (operator precedence)
    """
    import random as _rng
    fmt = _rng.randint(1, 4)

    if fmt == 1:
        a = _rng.randint(3, 9)
        b = _rng.randint(2, 9)
        c = _rng.randint(1, 20)
        answer = a * b + c
        question = f"{a} \u00d7 {b} + {c} = ?"
    elif fmt == 2:
        a = _rng.randint(5, 30)
        b = a + _rng.randint(3, 15)
        answer = a
        question = f"? + {b - a} = {b}"
    elif fmt == 3:
        words = ["sunflower", "computer", "keyboard", "elephant",
                 "rainbow", "umbrella", "butterfly", "mountain", "dolphin"]
        word = _rng.choice(words)
        pos = _rng.randint(1, len(word))
        answer = word[pos - 1]
        question = f"What is letter {pos} of \'{word}\'? (lowercase)"
    else:
        a = _rng.randint(1, 15)
        b = _rng.randint(2, 8)
        c = _rng.randint(2, 8)
        answer = a + b * c
        question = f"{a} + {b} \u00d7 {c} = ?"

    session["captcha_answer"] = str(answer).lower().strip()
    session["captcha_time"] = time.time()
    return question



from app.auth import routes, webauthn  # noqa: E402,F401 — register routes
