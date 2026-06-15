"""Tests for carbon offset tracking (v0.9.1 — carbon reclamation feature)."""
import os
import sys
import pytest
from datetime import datetime, timezone, timedelta
from unittest.mock import patch

# Test environment
os.environ["DATABASE_URL"] = "sqlite:///:memory:"
os.environ["SECRET_KEY"] = "test-secret"
os.environ["WTF_CSRF_ENABLED"] = "false"

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


@pytest.fixture
def app():
    from app import create_app as _create_app
    test_app = _create_app({
        "TESTING": True,
        "SQLALCHEMY_DATABASE_URI": "sqlite:///:memory:",
        "SECRET_KEY": "test-secret",
        "WTF_CSRF_ENABLED": False,
    })
    with test_app.app_context():
        from app import db
        db.create_all()
        yield test_app
        db.session.remove()
        db.drop_all()


@pytest.fixture
def client(app):
    return app.test_client()


# ── Model tests ────────────────────────────────────────────────────────────

class TestCarbonOffsetModel:
    def test_create_carbon_capture(self, app):
        from app.models import CarbonOffset
        from app import db
        offset = CarbonOffset(
            offset_type="carbon_capture",
            title="1 tonne DAC",
            amount_kg=1000.0,
            reference_number="DAC-2026-001",
            cost_gbp=150.0,
            purchase_date=datetime.now(timezone.utc),
        )
        db.session.add(offset)
        db.session.commit()
        assert offset.id is not None
        assert offset.offset_type == "carbon_capture"
        assert offset.amount_kg == 1000.0

    def test_create_tree_planting(self, app):
        from app.models import CarbonOffset
        from app import db
        offset = CarbonOffset(
            offset_type="tree_planting",
            title="100 oak trees",
            tree_count=100,
            reference_number="TREE-2026-001",
            cost_gbp=500.0,
            purchase_date=datetime.now(timezone.utc),
        )
        db.session.add(offset)
        db.session.commit()
        assert offset.tree_count == 100
        assert offset.amount_kg is None


# ── Calculation tests ──────────────────────────────────────────────────────

class TestTreeReclamation:
    def test_zero_reclamation_immediately(self, app):
        from app.models import CarbonOffset
        from app.carbon_offsets import calculate_tree_reclaimed_kg
        now = datetime.now(timezone.utc)
        offset = CarbonOffset(
            offset_type="tree_planting",
            title="Test",
            tree_count=100,
            purchase_date=now,
        )
        # Just planted — near zero
        kg = calculate_tree_reclaimed_kg(offset, now=now)
        assert kg == 0.0

    def test_one_year_of_growth(self, app):
        from app.models import CarbonOffset
        from app.carbon_offsets import calculate_tree_reclaimed_kg
        now = datetime.now(timezone.utc)
        planted = now - timedelta(days=365)
        offset = CarbonOffset(
            offset_type="tree_planting",
            title="Test",
            tree_count=100,
            purchase_date=planted,
        )
        # 100 trees × 0.021 kg/year × ~1 year = ~2.1 kg
        kg = calculate_tree_reclaimed_kg(offset, now=now)
        assert 1.9 < kg < 2.3

    def test_capped_at_lifetime(self, app):
        from app.models import CarbonOffset
        from app.carbon_offsets import calculate_tree_reclaimed_kg
        now = datetime.now(timezone.utc)
        # Planted 100 years ago — should cap at 40-year lifetime
        planted = now - timedelta(days=365 * 100)
        offset = CarbonOffset(
            offset_type="tree_planting",
            title="Ancient forest",
            tree_count=1,
            purchase_date=planted,
        )
        # 1 tree × 0.021 kg/year × 40 years = 0.84 kg
        kg = calculate_tree_reclaimed_kg(offset, now=now)
        assert abs(kg - 0.84) < 0.01

    def test_no_trees_returns_zero(self, app):
        from app.models import CarbonOffset
        from app.carbon_offsets import calculate_tree_reclaimed_kg
        offset = CarbonOffset(
            offset_type="tree_planting",
            title="Test",
            tree_count=None,
            purchase_date=datetime.now(timezone.utc),
        )
        assert calculate_tree_reclaimed_kg(offset) == 0.0


class TestReclaimedTotals:
    def test_empty_offsets(self, app):
        from app.carbon_offsets import get_reclaimed_totals
        totals = get_reclaimed_totals()
        assert totals["total_reclaimed_kg"] == 0.0
        assert totals["carbon_capture_kg"] == 0.0
        assert totals["tree_kg"] == 0.0

    def test_mixed_offsets(self, app):
        from app.models import CarbonOffset
        from app import db
        from app.carbon_offsets import get_reclaimed_totals

        # Carbon capture
        db.session.add(CarbonOffset(
            offset_type="carbon_capture",
            title="1 tonne DAC",
            amount_kg=1000.0,
            purchase_date=datetime.now(timezone.utc),
        ))
        # Trees (planted 2 years ago)
        db.session.add(CarbonOffset(
            offset_type="tree_planting",
            title="100 trees",
            tree_count=100,
            purchase_date=datetime.now(timezone.utc) - timedelta(days=730),
        ))
        db.session.commit()

        totals = get_reclaimed_totals()
        assert totals["carbon_capture_kg"] == 1000.0
        # 100 trees × 0.021 × ~2 years = ~4.2 kg
        assert 3.8 < totals["tree_kg"] < 4.6
        assert totals["total_reclaimed_kg"] == 1000.0 + totals["tree_kg"]


# ── API tests ──────────────────────────────────────────────────────────────

class TestCarbonOffsetsAPI:
    def test_list_offsets_public(self, client):
        """The public API should return 200 with zero offsets."""
        r = client.get("/api/carbon-offsets")
        assert r.status_code == 200
        data = r.get_json()
        assert "total_reclaimed_kg" in data
        assert "offsets" in data

    def test_site_wide_includes_reclaimed(self, client):
        """Site-wide API should include reclaimed fields."""
        r = client.get("/api/sustainability/site-wide")
        assert r.status_code == 200
        data = r.get_json()
        assert "total_reclaimed_kg" in data
        assert "carbon_capture_kg" in data
        assert "tree_kg" in data

    def test_certificate_404(self, client):
        """Non-existent certificate returns 404."""
        r = client.get("/api/carbon-offsets/9999/certificate")
        assert r.status_code == 404

    def test_certificate_served(self, app, client):
        """Certificate image is served correctly."""
        from app.models import CarbonOffset
        from app import db
        offset = CarbonOffset(
            offset_type="carbon_capture",
            title="Test",
            amount_kg=100.0,
            purchase_date=datetime.now(timezone.utc),
            certificate_image=b"\x89PNG fake image data",
            certificate_image_mime="image/png",
        )
        db.session.add(offset)
        db.session.commit()

        r = client.get(f"/api/carbon-offsets/{offset.id}/certificate")
        assert r.status_code == 200
        assert r.content_type == "image/png"
