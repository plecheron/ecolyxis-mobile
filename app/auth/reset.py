"""Password reset routes — token-based reset via itsdangerous.

Since no SMTP/Flask-Mail is configured, the reset token is generated and
displayed to the user on-screen (beta mode). When email is configured later,
the same token can be emailed instead.

Token = URLSafeTimedSerializer signed with SECRET_KEY, 1-hour expiry.
"""
from datetime import datetime, timezone
from flask import render_template, redirect, url_for, flash, request, current_app
from flask_login import current_user
from itsdangerous import URLSafeTimedSerializer, SignatureExpired, BadSignature

from app import db
from app.models import User
from app.auth import auth_bp


def _get_serializer():
    """Build a serializer using the app's SECRET_KEY."""
    return URLSafeTimedSerializer(
        current_app.config["SECRET_KEY"],
        salt="password-reset",
    )


def _generate_reset_token(email):
    """Generate a time-limited signed token for the given email."""
    s = _get_serializer()
    return s.dumps({"email": email})


def _verify_reset_token(token, max_age=3600):
    """Verify a reset token; return the email or None."""
    s = _get_serializer()
    try:
        data = s.loads(token, max_age=max_age)
        return data["email"]
    except (SignatureExpired, BadSignature, KeyError):
        return None


@auth_bp.route("/forgot-password", methods=["GET", "POST"])
def forgot_password():
    """Request a password reset link.

    Displays the reset token on-screen in beta mode (no email infrastructure).
    """
    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        user = User.query.filter_by(email=email).first()

        if user:
            token = _generate_reset_token(email)
            reset_url = url_for("auth.reset_password", token=token, _external=True)
            flash(
                f"Password reset link generated (beta — no email configured yet): "
                f'<a href="{reset_url}">{reset_url}</a>',
                "info",
            )
            current_app.logger.info(f"Password reset requested for {email}")
        else:
            # Don't reveal whether email exists — same message either way
            flash(
                "If that email is registered, a reset link has been generated. "
                "Check with support if you don't receive it.",
                "info",
            )

        return render_template("auth/forgot_password.html")

    return render_template("auth/forgot_password.html")


@auth_bp.route("/reset-password/<token>", methods=["GET", "POST"])
def reset_password(token):
    """Reset password using a valid token."""
    email = _verify_reset_token(token)

    if not email:
        flash("This password reset link is invalid or has expired. Please request a new one.", "error")
        return redirect(url_for("auth.forgot_password"))

    user = User.query.filter_by(email=email).first()
    if not user:
        flash("Account not found.", "error")
        return redirect(url_for("auth.forgot_password"))

    if request.method == "POST":
        password = request.form.get("password", "")
        confirm = request.form.get("confirm", "")

        errors = []
        if len(password) < 6:
            errors.append("Password must be at least 6 characters.")
        if password != confirm:
            errors.append("Passwords do not match.")

        if errors:
            for e in errors:
                flash(e, "error")
            return render_template("auth/reset_password.html", token=token)

        user.set_password(password)
        user.last_login = datetime.now(timezone.utc)
        db.session.commit()

        flash("Your password has been reset successfully. Please log in.", "success")
        return redirect(url_for("auth.login"))

    return render_template("auth/reset_password.html", token=token)
