"""Signup, login, logout — password-based auth flows."""
import time
from datetime import datetime, timezone
from flask import render_template, redirect, url_for, flash, request, make_response, session
from flask_login import login_user, logout_user, login_required

from app import db
from app.models import User
from app.auth import (
    auth_bp,
    _check_ip_rate,
    _record_ip_attempt,
    _generate_captcha,
    FORM_MIN_SECONDS,
)


@auth_bp.route("/signup", methods=["GET", "POST"])
def signup():
    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")
        confirm = request.form.get("confirm", "")
        captcha_response = request.form.get("captcha", "").strip()

        # Honeypot: hidden field bots fill in
        if request.form.get("website", ""):
            return redirect(url_for("auth.signup"))

        # Time gate: reject forms submitted too fast
        form_time = session.get("captcha_time", 0)
        if form_time and (time.time() - form_time) < FORM_MIN_SECONDS:
            flash("Something went wrong. Please try again.", "error")
            return render_template("auth/signup.html", email=email, captcha=_generate_captcha())

        # IP rate limit
        ip = request.headers.get("X-Forwarded-For", request.remote_addr).split(",")[0].strip()
        if not _check_ip_rate(ip):
            flash("Too many signup attempts. Please try again later.", "error")
            return render_template("auth/signup.html", email=email, captcha=_generate_captcha())

        # CAPTCHA check
        expected = session.get("captcha_answer", "")
        if not captcha_response or captcha_response != expected:
            flash("Incorrect answer to the security question.", "error")
            return render_template("auth/signup.html", email=email, captcha=_generate_captcha())

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

        _record_ip_attempt(ip)
        user = User(username=email, email=email)
        user.set_password(password)
        db.session.add(user)
        db.session.commit()

        session.pop("captcha_answer", None)
        session.pop("captcha_time", None)

        login_user(user)
        flash("Welcome to Ecolyxis! 🌿", "success")
        resp = make_response(redirect(url_for("dashboard.index")))
        resp.status_code = 303
        return resp

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


@auth_bp.route("/logout")
@login_required
def logout():
    logout_user()
    flash("Logged out. See you next time! 🌱", "success")
    return redirect(url_for("landing"))
