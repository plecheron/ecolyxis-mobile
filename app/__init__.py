from flask import Flask, redirect, url_for, render_template
from flask_sqlalchemy import SQLAlchemy
from flask_login import LoginManager, login_required
from flask_migrate import Migrate
from config import Config

db = SQLAlchemy()
migrate = Migrate()
login_manager = LoginManager()
login_manager.login_view = "auth.login"
login_manager.login_message = "Please log in to access your chats."


def _validate_config(app):
    """Validate critical config at startup. Raises RuntimeError if secrets are missing."""
    if not app.config.get("SECRET_KEY"):
        raise RuntimeError("SECRET_KEY must be set via environment variable or .env file")
    if not app.config.get("SQLALCHEMY_DATABASE_URI"):
        raise RuntimeError("DATABASE_URL must be set via environment variable or .env file")
    # Warn (not crash) if Stripe config looks like placeholders
    sk = app.config.get("STRIPE_SECRET_KEY", "")
    if sk and sk.startswith("sk_test_placeholder"):
        app.logger.warning("STRIPE_SECRET_KEY is using a placeholder value — Stripe payments will not work")
    whsec = app.config.get("STRIPE_WEBHOOK_SECRET", "")
    if whsec and whsec.startswith("whsec_placeholder"):
        app.logger.warning("STRIPE_WEBHOOK_SECRET is using a placeholder value — webhook verification will fail")
    # A live Stripe key without a webhook secret means real payment events
    # would arrive unverifiable. The endpoint already rejects them, but that
    # silently breaks billing — fail loudly at startup instead.
    if sk.startswith("sk_live_") and (not whsec or whsec.startswith("whsec_placeholder")):
        raise RuntimeError(
            "STRIPE_WEBHOOK_SECRET must be set when a live STRIPE_SECRET_KEY is configured"
        )


def create_app(test_config=None):
    app = Flask(__name__)
    app.config.from_object(Config)
    if test_config:
        app.config.update(test_config)

    db.init_app(app)
    migrate.init_app(app, db)
    login_manager.init_app(app)

    # Validate secrets (skip in testing mode)
    if not app.config.get("TESTING"):
        _validate_config(app)

    from app.models import User

    @login_manager.user_loader
    def load_user(user_id):
        return db.session.get(User, int(user_id))

    from app.auth import auth_bp
    from app.dashboard import dash_bp
    from app.chat import chat_bp
    from app.billing import billing_bp
    from app.contact import contact_bp
    from app.api import api_bp
    from app.apikeys import apikeys_bp
    from app.wallet import wallet_bp
    from app.blog import blog_bp
    from app.legal import legal_bp
    from app.health import health_bp
    from app.pricing import pricing_bp
    from app.jobs.routes import jobs_bp
    from app.workspace import workspace_bp
    from app.admin import admin_bp
    from app.sharing import share_bp
    from app.analytics import analytics_bp
    from app.models_selector import models_selector_bp
    from app.sustainability import sustainability_bp
    from app.carbon_offsets import carbon_offsets_bp

    app.register_blueprint(auth_bp)
    app.register_blueprint(dash_bp)
    app.register_blueprint(chat_bp)
    app.register_blueprint(billing_bp)
    app.register_blueprint(contact_bp)
    app.register_blueprint(api_bp)
    app.register_blueprint(apikeys_bp)
    app.register_blueprint(wallet_bp)
    app.register_blueprint(blog_bp)
    app.register_blueprint(legal_bp)
    app.register_blueprint(health_bp)
    app.register_blueprint(pricing_bp)
    app.register_blueprint(jobs_bp)
    app.register_blueprint(workspace_bp)
    app.register_blueprint(admin_bp)
    app.register_blueprint(share_bp)
    app.register_blueprint(analytics_bp)
    app.register_blueprint(models_selector_bp)
    app.register_blueprint(sustainability_bp)
    app.register_blueprint(carbon_offsets_bp)


    # Security headers (#116)
    @app.after_request
    def _set_security_headers(response):
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Strict-Transport-Security", "max-age=31536000; includeSubDomains")
        response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
        response.headers.setdefault("Permissions-Policy", "geolocation=(), microphone=(), camera=()")
        return response

    # CSRF protection for all non-API POST routes
    from app.csrf import generate_csrf_token, validate_csrf_token
    app.jinja_env.globals["csrf_token"] = generate_csrf_token

    # Admin integration: ban enforcement + audit webhook + feature flags
    from app.admin_integration import check_ban_status, register_audit_endpoint
    register_audit_endpoint(app)

    @app.before_request
    def _check_ban():
        if app.config.get("TESTING"):
            return None
        return check_ban_status()

    @app.before_request
    def _check_csrf():
        from flask import request, session, jsonify
        # Skip in testing mode
        if app.config.get("TESTING"):
            return None
        # Skip safe methods
        if request.method in ("GET", "HEAD", "OPTIONS"):
            return None
        # API routes use Bearer token auth — exempt
        if request.path.startswith("/v1/"):
            return None
        # Health checks — exempt
        if request.path.startswith("/health"):
            return None
        # Stripe webhook — exempt (authenticated by Stripe signature, not a CSRF
        # token). Scoped to the exact path so other state-changing /billing/*
        # routes (e.g. cancel-subscription) keep CSRF protection.
        if request.path == "/billing/webhook":
            return None

        token = request.headers.get("X-CSRFToken") or request.form.get("csrf_token")
        if not validate_csrf_token(token):
            from flask import session as _sess
            # Generate a fresh token so the next request can succeed
            generate_csrf_token()
            # AJAX/fetch requests (DELETE, PUT, PATCH, JSON): return 403, don't redirect.
            # A 303 redirect on DELETE silently fails because the browser follows
            # it as GET and hits a 405, making the error opaque to the JS caller.
            if (request.method in ("DELETE", "PUT", "PATCH")
                    or request.is_json
                    or request.headers.get("Accept", "").startswith("application/json")):
                return jsonify({"error": "CSRF token validation failed"}), 403
            # For form submissions, redirect with flash so user gets fresh token
            from flask import flash as _flash
            _flash("Security token expired. Please try again.", "error")
            # Redirect to the same path (GET) to get a fresh form
            return redirect(request.url), 303
        return None

    @app.route("/")
    def landing():
        from flask import render_template, redirect, url_for
        from flask_login import current_user
        if current_user.is_authenticated:
            return redirect(url_for("dashboard.index"))
        return render_template("landing.html")

    with app.app_context():
        from app.post_model import Post  # ensure model is registered
        import markdown as md_lib
        app.jinja_env.filters["markdown"] = lambda text: md_lib.markdown(text, extensions=["fenced_code", "tables", "nl2br"])
        app.jinja_env.filters["markdown"].__name__ = "markdown"

        import json as _json
        import markupsafe
        def render_message(text):
            """Render message content. For image messages, show text + image tags."""
            if not text or not text.strip().startswith("["):
                return markupsafe.Markup.escape(text) if text else ""
            try:
                parts = _json.loads(text.strip())
                if not isinstance(parts, list):
                    return markupsafe.Markup.escape(text)
                has_image = any(p.get("type") == "image" for p in parts)
                if not has_image:
                    return markupsafe.Markup.escape(text)
                html_parts = []
                for p in parts:
                    if p.get("type") == "text":
                        html_parts.append(f"<p>{markupsafe.escape(p.get('text', ''))}</p>")
                    elif p.get("type") == "image":
                        fname = p.get("file", p.get("url", ""))
                        name = p.get("name", fname)
                        img_id = p.get("image_id", "")
                        seed = p.get("seed", "")
                        w = p.get("width", 128)
                        h = p.get("height", 128)
                        if fname and not fname.startswith("data:"):
                            next_size = w * 2 if w < 512 else None
                            wrapper_start = '<div class="generated-image-wrapper"'
                            if img_id:
                                wrapper_start += f' data-image-id="{markupsafe.escape(str(img_id))}"'
                                wrapper_start += f' data-seed="{markupsafe.escape(str(seed))}"'
                                wrapper_start += f' data-width="{w}" data-height="{h}"'
                            wrapper_start += '>'
                            img_tag = f'<img src="/uploads/{markupsafe.escape(fname)}" class="message-image generated-image" alt="{markupsafe.escape(name)}" loading="lazy" style="max-width:100%;border-radius:8px;margin-top:8px;">'
                            btn_html = ""
                            if img_id and next_size:
                                btn_html = f'<div class="upscale-btn-container"><button class="btn-upscale" onclick="upscaleImage(this.closest(\'.generated-image-wrapper\'), threadId)">⬆ Upscale to {next_size}x{next_size}</button></div>'
                            elif img_id and not next_size:
                                btn_html = '<div class="upscale-btn-container"><span class="upscale-max">✓ Max resolution</span></div>'
                            wrapper_end = '</div>'
                            html_parts.append(wrapper_start + img_tag + btn_html + wrapper_end)
                        elif p.get("url", "").startswith("data:"):
                            html_parts.append('<div class="message-images"><img src="' + str(markupsafe.escape(p["url"][:50])) + '..." class="message-image" alt="image"></div>')
                return markupsafe.Markup("".join(html_parts))
            except (_json.JSONDecodeError, KeyError, TypeError):
                return markupsafe.Markup.escape(text)
        app.jinja_env.filters["render_message"] = render_message

        @app.errorhandler(404)
        def not_found(e):
            """Custom 404 page with navigation."""
            return render_template("404.html"), 404

        @app.errorhandler(500)
        def internal_error(e):
            """Custom 500 page."""
            return render_template("500.html"), 500

        @app.route("/settings")
        def settings_redirect():
            """Redirect /settings to dashboard."""
            from flask import redirect, url_for
            return redirect(url_for("dashboard.index"))

        @app.route("/security")
        def security_redirect():
            """Redirect /security to dashboard."""
            from flask import redirect, url_for
            return redirect(url_for("dashboard.index"))

        @app.route("/chat")
        @login_required
        def chat_redirect():
            """Redirect bare /chat to dashboard."""
            return redirect(url_for("dashboard.index"))

    return app
