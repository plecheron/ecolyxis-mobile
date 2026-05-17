"""WebAuthn (FIDO2 / passkey) registration and authentication.

All endpoints 501 if the `webauthn` package isn't installed.
"""
import base64
import json
from datetime import datetime, timezone
from flask import request, jsonify, session
from flask_login import login_user, login_required, current_user

from app import db
from app.models import User, WebAuthnCredential
from app.auth import auth_bp, RP_ID, RP_NAME, RP_ORIGIN


@auth_bp.route("/webauthn/register-begin", methods=["POST"])
@login_required
def webauthn_register_begin():
    """Start WebAuthn registration. Returns options for navigator.credentials.create()."""
    try:
        from webauthn import generate_registration_options, options_to_json
        from webauthn.helpers.structs import (
            AuthenticatorSelectionCriteria,
            AuthenticatorAttachment,
            UserVerificationRequirement,
            ResidentKeyRequirement,
        )
        from webauthn.helpers.cose import COSEAlgorithmIdentifier
    except ImportError:
        return jsonify({"error": "WebAuthn not available"}), 501

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
    session["webauthn_reg_challenge"] = opts_json["challenge"]
    session["webauthn_user_handle"] = user_handle.decode()
    return jsonify(opts_json)


@auth_bp.route("/webauthn/register-finish", methods=["POST"])
@login_required
def webauthn_register_finish():
    """Complete WebAuthn registration. Verify attestation and store credential."""
    try:
        from webauthn import verify_registration_response
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

    cred_id_bytes = verification.credential_id
    if WebAuthnCredential.query.filter_by(credential_id=cred_id_bytes).first():
        return jsonify({"error": "Credential already registered"}), 409

    transports_list = credential.get("transports") or []
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

    options = generate_authentication_options(
        rp_id=RP_ID,
        user_verification=UserVerificationRequirement.REQUIRED,
    )

    opts_json = json.loads(options_to_json(options))
    session["webauthn_auth_challenge"] = opts_json["challenge"]
    opts_json["allowCredentials"] = []  # allow any discoverable credential
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
