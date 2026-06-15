"""Tests for v0.7.0-beta: Sustainability, dark mode, UX features.

Covers:
  - Sustainability calculation engine (CO₂e, energy estimation, savings)
  - Sustainability API endpoints (overview, site-wide)
  - Sustainability dashboard route
  - Message model energy_wh / co2e_g fields
  - Landing page site-wide counter endpoint
  - Dark mode CSS variables
  - Chat.js extraction (external file exists)
  - .bak file cleanup
"""
import pytest
import os
from app import create_app, db
from app.models import User, Thread, Message
from app.sustainability import (
    calculate_co2e,
    estimate_energy_for_tokens,
    calculate_savings,
    UK_GRID_CO2_PER_KWH,
    CLOUD_GRID_CO2_PER_KWH,
    CLOUD_PUE,
    PowerSampler,
)


# ── Sustainability calculation engine ───────────────────────────────────────

class TestCO2Calculations:
    """Unit tests for the CO₂e calculation engine."""

    def test_calculate_co2e_basic(self):
        """1 Wh at UK grid factor should produce a small but positive CO₂e."""
        co2e = calculate_co2e(1.0)  # 1 Wh
        assert co2e > 0
        # 1 Wh = 0.001 kWh; CO₂e = 0.001 * 0.18 * 1000 = 0.18g
        assert abs(co2e - 0.18) < 0.01

    def test_calculate_co2e_zero(self):
        """Zero energy should produce zero CO₂e."""
        assert calculate_co2e(0.0) == 0.0

    def test_calculate_co2e_none(self):
        """None energy should produce zero CO₂e."""
        assert calculate_co2e(None) == 0.0

    def test_calculate_co2e_cloud_higher(self):
        """Cloud baseline should produce more CO₂e than Ecolyxis."""
        energy_wh = 100.0
        ecolyxis = calculate_co2e(energy_wh, UK_GRID_CO2_PER_KWH, 1.0)
        cloud = calculate_co2e(energy_wh, CLOUD_GRID_CO2_PER_KWH, CLOUD_PUE)
        assert cloud > ecolyxis

    def test_calculate_savings_positive(self):
        """Savings should always be positive (Ecolyxis is greener)."""
        energy_wh = 50.0
        ecolyxis, cloud, savings = calculate_savings(energy_wh)
        assert savings > 0
        assert cloud > ecolyxis

    def test_calculate_savings_zero(self):
        """Zero energy should produce zero savings."""
        ecolyxis, cloud, savings = calculate_savings(0.0)
        assert savings == 0.0
        assert ecolyxis == 0.0
        assert cloud == 0.0

    def test_calculate_savings_proportional(self):
        """Savings should scale linearly with energy."""
        _, _, savings_100 = calculate_savings(100.0)
        _, _, savings_200 = calculate_savings(200.0)
        assert abs(savings_200 - 2 * savings_100) < 0.01

    def test_estimate_energy_for_tokens(self):
        """Token-based estimate should return positive Wh for tokens."""
        wh = estimate_energy_for_tokens(1000, 500)
        assert wh > 0
        # 1500 tokens * 0.000895 Wh = ~1.34 Wh
        assert 1.0 < wh < 2.0

    def test_estimate_energy_zero_tokens(self):
        """Zero tokens should produce zero energy."""
        assert estimate_energy_for_tokens(0, 0) == 0.0

    def test_estimate_energy_includes_reasoning(self):
        """Reasoning tokens should be included in the estimate."""
        without_reasoning = estimate_energy_for_tokens(100, 100)
        with_reasoning = estimate_energy_for_tokens(100, 100, reasoning_tokens=200)
        assert with_reasoning > without_reasoning


class TestPowerSampler:
    """Tests for the GPU power sampler."""

    def test_energy_wh_no_samples(self):
        """No samples should return None energy."""
        sampler = PowerSampler()
        assert sampler.energy_wh() is None

    def test_energy_wh_single_sample(self):
        """Single sample should return None (need at least 2 for integration)."""
        sampler = PowerSampler()
        sampler.samples.append((0.0, 100.0))
        assert sampler.energy_wh() is None


# ── Sustainability routes ───────────────────────────────────────────────────

class TestSustainabilityRoutes:
    """Route tests for sustainability endpoints."""

    def test_dashboard_requires_auth(self, client):
        """Unauthenticated access should redirect to login."""
        resp = client.get("/sustainability", follow_redirects=False)
        assert resp.status_code in (301, 302)

    def test_dashboard_authenticated(self, client, make_user, login_as):
        """Authenticated user should see the dashboard."""
        user = make_user()
        login_as(user)
        resp = client.get("/sustainability")
        assert resp.status_code == 200
        assert b"Sustainability" in resp.data
        assert b"CO" in resp.data

    def test_api_overview_requires_auth(self, client):
        """API overview should require authentication."""
        resp = client.get("/api/sustainability/overview")
        assert resp.status_code in (301, 302)

    def test_api_overview_returns_json(self, client, make_user, login_as):
        """API overview should return JSON with correct fields."""
        user = make_user()
        login_as(user)
        resp = client.get("/api/sustainability/overview")
        assert resp.status_code == 200
        data = resp.get_json()
        assert "energy_wh" in data
        assert "ecolyxis_co2e_g" in data
        assert "cloud_co2e_g" in data
        assert "savings_g" in data
        assert "messages_count" in data
        assert "methodology" in data

    def test_api_site_wide_public(self, client):
        """Site-wide endpoint should be public (no auth required)."""
        resp = client.get("/api/sustainability/site-wide")
        assert resp.status_code == 200
        data = resp.get_json()
        assert "total_energy_wh" in data
        assert "total_savings_g" in data
        assert "user_count" in data

    def test_dashboard_shows_user_energy(self, client, make_user, login_as, app, db):
        """Dashboard should show energy from user's messages."""
        user = make_user()
        login_as(user)

        with app.app_context():
            thread = Thread(user_id=user.id, title="Test Thread")
            db.session.add(thread)
            db.session.commit()

            msg = Message(
                thread_id=thread.id,
                role="assistant",
                content="Test response",
                tokens_used=100,
                reasoning_tokens=50,
                energy_wh=0.5,
                co2e_g=0.09,
            )
            db.session.add(msg)
            db.session.commit()

        resp = client.get("/sustainability")
        assert resp.status_code == 200
        assert b"0.5" in resp.data or b"Wh" in resp.data

    def test_overview_with_energy_data(self, client, make_user, login_as, app, db):
        """Overview API should include energy from messages with energy_wh."""
        user = make_user()
        login_as(user)

        with app.app_context():
            thread = Thread(user_id=user.id, title="Energy Test")
            db.session.add(thread)
            db.session.commit()

            msg = Message(
                thread_id=thread.id,
                role="assistant",
                content="Green response",
                tokens_used=200,
                energy_wh=2.5,
                co2e_g=0.45,
            )
            db.session.add(msg)
            db.session.commit()

        resp = client.get("/api/sustainability/overview")
        data = resp.get_json()
        assert data["energy_wh"] >= 2.5
        assert data["messages_count"] >= 1
        assert data["savings_g"] > 0


# ── Message model sustainability fields ─────────────────────────────────────

class TestMessageSustainabilityFields:
    """Test that Message model has energy_wh and co2e_g fields."""

    def test_message_has_energy_wh(self, app, db):
        """Message model should accept energy_wh field."""
        with app.app_context():
            user = User(username="energytest", email="energy@test.com")
            user.set_password("Test123!")
            db.session.add(user)
            db.session.commit()

            thread = Thread(user_id=user.id, title="Energy Test")
            db.session.add(thread)
            db.session.commit()

            msg = Message(
                thread_id=thread.id,
                role="assistant",
                content="Green response",
                tokens_used=100,
                energy_wh=1.5,
                co2e_g=0.27,
            )
            db.session.add(msg)
            db.session.commit()

            loaded = db.session.get(Message, msg.id)
            assert loaded.energy_wh == 1.5
            assert loaded.co2e_g == 0.27

    def test_message_energy_wh_nullable(self, app, db):
        """energy_wh should default to None (legacy messages)."""
        with app.app_context():
            user = User(username="nullabletest", email="nullable@test.com")
            user.set_password("Test123!")
            db.session.add(user)
            db.session.commit()

            thread = Thread(user_id=user.id, title="Legacy Test")
            db.session.add(thread)
            db.session.commit()

            msg = Message(
                thread_id=thread.id,
                role="assistant",
                content="Legacy response",
                tokens_used=50,
            )
            db.session.add(msg)
            db.session.commit()

            loaded = db.session.get(Message, msg.id)
            assert loaded.energy_wh is None
            assert loaded.co2e_g is None


# ── Landing page ────────────────────────────────────────────────────────────

class TestLandingPage:
    """Tests for the beefed-up landing page."""

    def test_landing_has_sustainability_counter(self, client):
        """Landing page should have the site-wide counter element."""
        resp = client.get("/")
        assert resp.status_code == 200
        assert b"site-counter" in resp.data

    def test_landing_has_features(self, client):
        """Landing page should have the features section."""
        resp = client.get("/")
        assert b"feature-card" in resp.data
        assert b"Efficient Compute" in resp.data

    def test_landing_has_pricing_teaser(self, client):
        """Landing page should have pricing cards."""
        resp = client.get("/")
        assert b"pricing-card" in resp.data
        assert b"Premium" in resp.data

    def test_landing_has_sustainability_section(self, client):
        """Landing page should showcase sustainability."""
        resp = client.get("/")
        assert b"sustainability-showcase" in resp.data

    def test_landing_has_stats_bar(self, client):
        """Landing page should have the hardware/stats bar."""
        resp = client.get("/")
        assert b"stats-bar" in resp.data
        assert b"PUE" in resp.data


# ── Dark mode ───────────────────────────────────────────────────────────────

class TestDarkMode:
    """Tests for light/dark theme support."""

    def test_light_theme_css_exists(self, app):
        """style.css should contain light theme variables."""
        css_path = os.path.join(app.static_folder, "css", "style.css")
        with open(css_path) as f:
            css = f.read()
        assert 'data-theme="light"' in css
        assert "prefers-color-scheme" in css

    def test_theme_toggle_in_base(self, client):
        """Base template should have the theme toggle button."""
        # Landing page uses base.html
        resp = client.get("/")
        assert b"toggleTheme" in resp.data
        assert b"theme-toggle" in resp.data

    def test_theme_persistence_script(self, client):
        """Base template should have theme persistence script."""
        resp = client.get("/")
        assert b"localStorage.getItem('theme')" in resp.data


# ── Chat JS extraction ──────────────────────────────────────────────────────

class TestChatJsExtraction:
    """Tests for the chat.html JS extraction."""

    def test_chat_js_exists(self, app):
        """static/js/chat.js should exist."""
        js_path = os.path.join(app.static_folder, "js", "chat.js")
        assert os.path.exists(js_path), "static/js/chat.js not found"

    def test_chat_js_has_functions(self, app):
        """chat.js should contain the extracted functions."""
        js_path = os.path.join(app.static_folder, "js", "chat.js")
        with open(js_path) as f:
            js = f.read()
        assert "function sendMessage" in js
        assert "function streamJob" in js
        assert "function addMessage" in js

    def test_chat_html_reduced(self, app):
        """chat.html should be significantly smaller after extraction."""
        template_path = os.path.join(app.root_path, app.template_folder, "chat.html")
        with open(template_path) as f:
            lines = len(f.readlines())
        # Was 3649 lines, should now be under 400
        assert lines < 400, f"chat.html is still {lines} lines"

    def test_chat_html_references_external_js(self, app):
        """chat.html should reference the external chat.js."""
        template_path = os.path.join(app.root_path, app.template_folder, "chat.html")
        with open(template_path) as f:
            html = f.read()
        assert "js/chat.js" in html

    def test_chat_js_has_retry_feature(self, app):
        """chat.js should contain the one-click retry feature."""
        js_path = os.path.join(app.static_folder, "js", "chat.js")
        with open(js_path) as f:
            js = f.read()
        assert "retryLastMessage" in js
        assert "_lastSentContent" in js

    def test_chat_js_has_jump_latest(self, app):
        """chat.js should contain the jump-to-latest button feature."""
        js_path = os.path.join(app.static_folder, "js", "chat.js")
        with open(js_path) as f:
            js = f.read()
        assert "jump-latest-btn" in js


# ── .bak file cleanup ──────────────────────────────────────────────────────

class TestBakFileCleanup:
    """Tests that .bak files have been removed."""

    def test_no_bak_in_static(self, app):
        """No .bak files should exist in static directory."""
        for root, dirs, files in os.walk(app.static_folder):
            for f in files:
                assert not f.endswith(".bak"), f"Found .bak file: {os.path.join(root, f)}"
                assert not f.endswith(".bak2"), f"Found .bak2 file: {os.path.join(root, f)}"

    def test_no_bak_in_templates(self, app):
        """No .bak files should exist in templates directory."""
        for root, dirs, files in os.walk(app.template_folder):
            for f in files:
                assert not f.endswith(".bak"), f"Found .bak file: {os.path.join(root, f)}"
                assert not f.endswith(".bak2"), f"Found .bak2 file: {os.path.join(root, f)}"
