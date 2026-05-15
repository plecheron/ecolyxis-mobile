import random
import time
import os
import json
import base64
from flask import Blueprint, render_template, redirect, url_for, flash, request, make_response, session, jsonify
from flask_login import login_user, logout_user, login_required, current_user
from datetime import datetime, timezone, timedelta
from flask import current_app
from app import db
from app.models import User, WebAuthnCredential
import requests as http_requests

# WebAuthn configuration
RP_ID = os.environ.get("WEBAUTHN_RP_ID", "ecolyxis.co.uk")
RP_NAME = "Ecolyxis"
RP_ORIGIN = os.environ.get("WEBAUTHN_ORIGIN", "https://ecolyxis.co.uk")

auth_bp = Blueprint("auth", __name__)

# In-memory IP rate limit tracker {ip: [timestamps]}
_signup_attempts = {}

SIGNUP_RATE_LIMIT = 3       # max signups per IP
SIGNUP_RATE_WINDOW = 3600   # per hour
FORM_MIN_SECONDS = 3        # minimum time to fill form


def _check_ip_rate(ip):
    """Return True if IP is allowed to attempt signup."""
    now = time.time()
    attempts = _signup_attempts.get(ip, [])
    # Prune old attempts
    attempts = [t for t in attempts if now - t < SIGNUP_RATE_WINDOW]
    _signup_attempts[ip] = attempts
    return len(attempts) < SIGNUP_RATE_LIMIT


def _record_ip_attempt(ip):
    """Record a signup attempt for this IP."""
    _signup_attempts.setdefault(ip, []).append(time.time())


def _generate_captcha():
    """Generate a simple math question + answer, store in session."""
    a = random.randint(1, 12)
    b = random.randint(1, 12)
    ops = [("+", lambda x, y: x + y), ("−", lambda x, y: x - y)]
    op_sym, op_fn = random.choice(ops)
    # Keep results positive
    if op_sym == "−" and a < b:
        a, b = b, a
    answer = op_fn(a, b)
    question = f"{a} {op_sym} {b} = ?"
    session["captcha_answer"] = str(answer)
    session["captcha_time"] = time.time()
    return question


@auth_bp.route("/signup", methods=["GET", "POST"])
def signup():
    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")
        confirm = request.form.get("confirm", "")
        captcha_response = request.form.get("captcha", "").strip()

        # 1. Honeypot: hidden field bots fill in
        if request.form.get("website", ""):
            return redirect(url_for("auth.signup"))

        # 2. Time gate: reject forms submitted too fast
        form_time = session.get("captcha_time", 0)
        if form_time and (time.time() - form_time) < FORM_MIN_SECONDS:
            flash("Something went wrong. Please try again.", "error")
            return render_template("auth/signup.html", email=email, captcha=_generate_captcha())

        # 3. IP rate limit
        ip = request.headers.get("X-Forwarded-For", request.remote_addr).split(",")[0].strip()
        if not _check_ip_rate(ip):
            flash("Too many signup attempts. Please try again later.", "error")
            return render_template("auth/signup.html", email=email, captcha=_generate_captcha())

        # 4. CAPTCHA check
        expected = session.get("captcha_answer", "")
        if not captcha_response or captcha_response != expected:
            flash("Incorrect answer to the security question.", "error")
            return render_template("auth/signup.html", email=email, captcha=_generate_captcha())

        # Standard validation
        errors = []
        if not email or "@" not in email:
            errors.append("Please enter a valid email.")
        if len(password) < 6:
            errors.append("Password must be at least 6 characters.")
        if password != confirm:
            errors.append("Passwords do not match.")
        if User.query.filter_by(email=email).first():
            errors.append("Email already registered.")

        if errors:
            for e in errors:
                flash(e, "error")
            return render_template("auth/signup.html", email=email, captcha=_generate_captcha())

        # All good — create account
        _record_ip_attempt(ip)
        user = User(username=email, email=email)
        user.set_password(password)
        db.session.add(user)
        db.session.commit()

        # Clear captcha from session
        session.pop("captcha_answer", None)
        session.pop("captcha_time", None)

        login_user(user)
        flash("Welcome to Ecolyxis! 🌿", "success")
        resp = make_response(redirect(url_for("dashboard.index")))
        resp.status_code = 303
        return resp

    # GET — show form with fresh captcha
    return render_template("auth/signup.html", captcha=_generate_captcha())


@auth_bp.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        login = request.form.get("login", "").strip()
        password = request.form.get("password", "")

        user = User.query.filter((User.email == login) | (User.username == login)).first()

        if user and user.check_password(password):
            user.last_login = datetime.now(timezone.utc)
            db.session.commit()
            login_user(user)
            flash("Welcome back! 🌿", "success")
            next_page = request.args.get("next")
            resp = make_response(redirect(next_page or url_for("dashboard.index")))
            resp.status_code = 303
            return resp
        else:
            flash("Invalid credentials.", "error")

    return render_template("auth/login.html")




# ── WebAuthn / Biometric Login ─────────────────────────────────────

@auth_bp.route("/webauthn/register-begin", methods=["POST"])
@login_required
def webauthn_register_begin():
    """Start WebAuthn registration. Returns options for navigator.credentials.create()."""
    try:
        from webauthn import (
            generate_registration_options,
            options_to_json,
        )
        from webauthn.helpers.structs import (
            AuthenticatorSelectionCriteria,
            AuthenticatorAttachment,
            UserVerificationRequirement,
            ResidentKeyRequirement,
        )
        from webauthn.helpers.cose import COSEAlgorithmIdentifier
    except ImportError:
        return jsonify({"error": "WebAuthn not available"}), 501

    import secrets
    challenge = secrets.token_bytes(32)

    # Build user handle (stable per user)
    user_handle = str(current_user.id).encode("utf-8")

    options = generate_registration_options(
        rp_id=RP_ID,
        rp_name=RP_NAME,
        user_id=user_handle,
        user_name=current_user.email or current_user.username,
        user_display_name=current_user.username,
        authenticator_selection=AuthenticatorSelectionCriteria(
            authenticator_attachment=AuthenticatorAttachment.PLATFORM,
            user_verification=UserVerificationRequirement.REQUIRED,
            resident_key=ResidentKeyRequirement.REQUIRED,
        ),
        supported_pub_key_algs=[
            COSEAlgorithmIdentifier.ECDSA_SHA_256,
            COSEAlgorithmIdentifier.RSASSA_PKCS1_v1_5_SHA_256,
        ],
    )

    opts_json = json.loads(options_to_json(options))

    # Store the challenge from the library response
    session["webauthn_reg_challenge"] = opts_json["challenge"]
    session["webauthn_user_handle"] = user_handle.decode()

    return jsonify(opts_json)


@auth_bp.route("/webauthn/register-finish", methods=["POST"])
@login_required
def webauthn_register_finish():
    """Complete WebAuthn registration. Verify attestation and store credential."""
    try:
        from webauthn import verify_registration_response
        from webauthn.helpers.structs import AuthenticatorTransport
    except ImportError:
        return jsonify({"error": "WebAuthn not available"}), 501

    data = request.get_json()
    credential = data.get("credential", {})
    cred_name = data.get("name", "My Device")

    challenge_b64 = session.get("webauthn_reg_challenge")
    if not challenge_b64:
        return jsonify({"error": "No registration in progress"}), 400

    challenge = base64.urlsafe_b64decode(challenge_b64 + "==")

    try:
        verification = verify_registration_response(
            credential=credential,
            expected_challenge=challenge,
            expected_origin=RP_ORIGIN,
            expected_rp_id=RP_ID,
        )
    except Exception as e:
        return jsonify({"error": f"Verification failed: {e}"}), 400

    # Check if credential already exists
    cred_id_bytes = verification.credential_id
    existing = WebAuthnCredential.query.filter_by(credential_id=cred_id_bytes).first()
    if existing:
        return jsonify({"error": "Credential already registered"}), 409

    # Store credential
    transports_list = []
    if credential.get("transports"):
        transports_list = credential["transports"]

    new_cred = WebAuthnCredential(
        user_id=current_user.id,
        credential_id=cred_id_bytes,
        public_key=verification.credential_public_key,
        sign_count=verification.sign_count,
        name=cred_name,
        transports=json.dumps(transports_list),
    )
    db.session.add(new_cred)
    db.session.commit()

    session.pop("webauthn_reg_challenge", None)
    session.pop("webauthn_user_handle", None)

    return jsonify({"success": True, "id": new_cred.id, "name": cred_name})


@auth_bp.route("/webauthn/authenticate-begin", methods=["POST"])
def webauthn_authenticate_begin():
    """Start WebAuthn authentication. Returns options for navigator.credentials.get()."""
    try:
        from webauthn import generate_authentication_options, options_to_json
        from webauthn.helpers.structs import UserVerificationRequirement
    except ImportError:
        return jsonify({"error": "WebAuthn not available"}), 501

    import secrets
    challenge = secrets.token_bytes(32)

    options = generate_authentication_options(
        rp_id=RP_ID,
        user_verification=UserVerificationRequirement.REQUIRED,
    )

    opts_json = json.loads(options_to_json(options))

    session["webauthn_auth_challenge"] = opts_json["challenge"]

    # Allow any stored credential (discoverable / passkey)
    opts_json["allowCredentials"] = []

    return jsonify(opts_json)


@auth_bp.route("/webauthn/authenticate-finish", methods=["POST"])
def webauthn_authenticate_finish():
    """Complete WebAuthn authentication. Verify assertion and log user in."""
    try:
        from webauthn import verify_authentication_response
    except ImportError:
        return jsonify({"error": "WebAuthn not available"}), 501

    data = request.get_json()
    credential = data.get("credential", {})

    challenge_b64 = session.get("webauthn_auth_challenge")
    if not challenge_b64:
        return jsonify({"error": "No authentication in progress"}), 400

    challenge = base64.urlsafe_b64decode(challenge_b64 + "==")

    # Find the credential by ID
    cred_id_raw = credential.get("id", "")
    try:
        cred_id_bytes = base64.urlsafe_b64decode(cred_id_raw + "==")
    except Exception:
        return jsonify({"error": "Invalid credential ID"}), 400

    stored_cred = WebAuthnCredential.query.filter_by(credential_id=cred_id_bytes).first()
    if not stored_cred:
        return jsonify({"error": "Unknown credential"}), 404

    user = User.query.get(stored_cred.user_id)
    if not user:
        return jsonify({"error": "User not found"}), 404

    # Get authenticator data from response
    authenticator_data = base64.urlsafe_b64decode(credential.get("response", {}).get("authenticatorData", "") + "==")
    client_data_json = base64.urlsafe_b64decode(credential.get("response", {}).get("clientDataJSON", "") + "==")
    signature = base64.urlsafe_b64decode(credential.get("response", {}).get("signature", "") + "==")

    try:
        verification = verify_authentication_response(
            credential=credential,
            expected_challenge=challenge,
            expected_origin=RP_ORIGIN,
            expected_rp_id=RP_ID,
            credential_public_key=stored_cred.public_key,
            credential_current_sign_count=stored_cred.sign_count,
        )
    except Exception as e:
        return jsonify({"error": f"Authentication failed: {e}"}), 401

    # Update sign count
    stored_cred.sign_count = verification.new_sign_count
    stored_cred.last_used_at = datetime.now(timezone.utc)
    user.last_login = datetime.now(timezone.utc)
    db.session.commit()

    login_user(user)
    session.pop("webauthn_auth_challenge", None)

    return jsonify({"success": True, "username": user.username})


@auth_bp.route("/webauthn/credentials")
@login_required
def webauthn_list_credentials():
    """List current user's WebAuthn credentials."""
    creds = WebAuthnCredential.query.filter_by(user_id=current_user.id).all()
    return jsonify([{
        "id": c.id,
        "name": c.name or "Unnamed device",
        "created_at": c.created_at.isoformat(),
        "last_used_at": c.last_used_at.isoformat() if c.last_used_at else None,
    } for c in creds])


@auth_bp.route("/webauthn/credentials/<int:cred_id>", methods=["DELETE"])
@login_required
def webauthn_delete_credential(cred_id):
    """Delete a WebAuthn credential."""
    cred = WebAuthnCredential.query.filter_by(id=cred_id, user_id=current_user.id).first_or_404()
    db.session.delete(cred)
    db.session.commit()
    return jsonify({"success": True})


@auth_bp.route("/logout")
@login_required
def logout():
    logout_user()
    flash("Logged out. See you next time! 🌱", "success")
    return redirect(url_for("landing"))
