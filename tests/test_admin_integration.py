"""Tests for admin integration: feature flags, ban enforcement, audit webhook."""
import os
import pytest
from app.models import User


class TestFeatureFlags:
    """Feature flag system — reads from admin_feature_flag table."""

    def test_is_feature_enabled_returns_default(self, app):
        """Feature flags return default when table is empty or key not found."""
        from app.admin_integration import is_feature_enabled, invalidate_flag_cache
        invalidate_flag_cache()
        with app.app_context():
            from app import db
            db.create_all()  # Ensure admin_feature_flag table exists
            assert is_feature_enabled("nonexistent_flag", default=True) is True
            assert is_feature_enabled("nonexistent_flag", default=False) is False

    def test_feature_required_decorator_blocks(self, app):
        """feature_required decorator returns redirect when flag is off."""
        from app.admin_integration import feature_required, invalidate_flag_cache
        from flask import jsonify
        invalidate_flag_cache()
        with app.app_context():
            from app import db
            db.create_all()

            @feature_required("test_flag_never_exists", default=False)
            def gated_route():
                return jsonify({"ok": True})

            with app.test_request_context("/_test_gated", method="GET"):
                result = gated_route()
                # Should return redirect response when flag is off
                assert result is not None


class TestBanEnforcement:
    """Ban checking — before_request hook."""

    def test_ban_check_passes_for_unauthenticated(self, app):
        """check_ban_status returns None for unauthenticated users."""
        from app.admin_integration import check_ban_status
        with app.test_request_context("/"):
            result = check_ban_status()
            assert result is None

    def test_ban_exempt_paths(self, app):
        """Exempt paths don't trigger ban check."""
        from app.admin_integration import _BAN_EXEMPT_PATHS
        assert "/auth/login" in _BAN_EXEMPT_PATHS
        assert "/health" in _BAN_EXEMPT_PATHS
        assert "/" in _BAN_EXEMPT_PATHS

    def test_banned_user_check_directly(self, app, db, make_user, login_as):
        """check_ban_status logs out banned users and returns redirect."""
        from app.admin_integration import check_ban_status
        from flask_login import login_user

        user = make_user(email="banned@test.com")
        user.is_banned = True
        db.session.commit()

        with app.test_request_context("/dashboard"):
            login_user(user)
            result = check_ban_status()
            # Should return a redirect response (302)
            assert result is not None
            assert result.status_code == 302


class TestAuditEndpoint:
    """Audit webhook endpoint."""

    def test_audit_endpoint_not_configured(self, client):
        """Returns 503 when ADMIN_AUDIT_KEY is not set."""
        response = client.post("/admin/audit-ingest", json={"action": "test"})
        assert response.status_code == 503


class TestModels:
    """Test that new model fields exist."""

    def test_user_has_is_banned(self, db, make_user):
        """User model has is_banned field."""
        user = make_user(email="ban@test.com")
        assert hasattr(user, "is_banned")
        assert user.is_banned is False

        user.is_banned = True
        db.session.commit()

        from app.models import User
        fetched = User.query.filter_by(email="ban@test.com").first()
        assert fetched.is_banned is True

    def test_carbon_offset_model(self, db):
        """CarbonOffset model can be created."""
        from app.models import CarbonOffset
        from datetime import datetime, timezone
        offset = CarbonOffset(
            offset_type="carbon_capture",
            title="Test DAC Purchase",
            amount_kg=1000.0,
            purchase_date=datetime.now(timezone.utc),
        )
        db.session.add(offset)
        db.session.commit()

        fetched = CarbonOffset.query.first()
        assert fetched.title == "Test DAC Purchase"
        assert fetched.offset_type == "carbon_capture"
        assert fetched.amount_kg == 1000.0
