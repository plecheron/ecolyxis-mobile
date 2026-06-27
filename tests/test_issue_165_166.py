"""Tests for login form improvements (#165 forgot-password link, #166 autocomplete)."""
import re


def test_login_has_autocomplete_username(app, client):
    """Login form username field should have autocomplete='username' (#166)."""
    resp = client.get('/login')
    assert resp.status_code == 200

    # Find the username input
    login_input = re.search(
        r'<input[^>]*id="login"[^>]*>', resp.text, re.DOTALL
    )
    assert login_input, "Login input not found"
    assert 'autocomplete="username"' in login_input.group(0), \
        f"Missing autocomplete=username: {login_input.group(0)}"


def test_login_has_autocomplete_current_password(app, client):
    """Login form password field should have autocomplete='current-password' (#166)."""
    resp = client.get('/login')
    assert resp.status_code == 200

    pwd_input = re.search(
        r'<input[^>]*id="password"[^>]*name="password"[^>]*>', resp.text, re.DOTALL
    )
    assert pwd_input, "Password input not found"
    assert 'autocomplete="current-password"' in pwd_input.group(0), \
        f"Missing autocomplete=current-password: {pwd_input.group(0)}"


def test_login_has_forgot_password_link(app, client):
    """Login page should have a 'Forgot password?' link (#165)."""
    resp = client.get('/login')
    assert resp.status_code == 200

    # Look for a link with "forgot" text (case-insensitive)
    forgot_match = re.search(
        r'<a[^>]*href="[^"]*"[^>]*>[^<]*[Ff]orgot\s+password[^<]*</a>',
        resp.text
    )
    assert forgot_match, "Forgot password link not found on login page"
