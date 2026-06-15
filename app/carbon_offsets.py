"""Carbon offset tracking — live CO₂ reclamation from purchases and tree planting.

Two offset types:
  - carbon_capture: A purchased carbon credit (e.g., 1 tonne direct air capture).
    The amount_kg is claimed immediately upon purchase.
  - tree_planting: Trees planted with a live CO₂ reclamation calculation.
    Conservative: 21 kg CO₂/year per tree, capped at 40-year lifetime (840 kg/tree).

The "reclaimed" figure is shown separately from the "saved" figure (which measures
CO₂ avoided by running on green infrastructure vs cloud).
"""
from datetime import datetime, timezone
from flask import Blueprint, jsonify, render_template
from flask_login import login_required, current_user
from app import db
from app.models import CarbonOffset
from sqlalchemy import desc
import logging

logger = logging.getLogger("ecolyxis.carbon_offsets")

carbon_offsets_bp = Blueprint("carbon_offsets", __name__)

# ── Tree absorption constants (conservative) ────────────────────────────────
# Source: EPA / UK Forestry Commission — average mature tree absorbs ~21 kg
# CO₂ per year over its productive lifetime.
TREE_CO2_KG_PER_YEAR = 0.021  # 21 kg CO₂ per tree per year

# Trees plateau in absorption after ~40 years; cap to avoid overcounting.
TREE_LIFETIME_YEARS = 40
TREE_MAX_KG = TREE_CO2_KG_PER_YEAR * TREE_LIFETIME_YEARS  # 0.84 kg max per tree


# ── Calculation functions ───────────────────────────────────────────────────

def calculate_tree_reclaimed_kg(offset, now=None):
    """Live CO₂ reclaimed from a tree planting offset.

    Returns kg CO₂ based on time elapsed since planting date.
    Capped at TREE_LIFETIME_YEARS per tree.
    """
    if not offset.tree_count or not offset.purchase_date:
        return 0.0
    now = now or datetime.now(timezone.utc)
    purchase = offset.purchase_date
    if purchase.tzinfo is None:
        purchase = purchase.replace(tzinfo=timezone.utc)
    seconds_elapsed = (now - purchase).total_seconds()
    if seconds_elapsed <= 0:
        return 0.0
    years_elapsed = seconds_elapsed / (365.25 * 24 * 3600)
    years_elapsed = min(years_elapsed, TREE_LIFETIME_YEARS)
    kg = offset.tree_count * TREE_CO2_KG_PER_YEAR * years_elapsed
    return round(kg, 4)


def get_reclaimed_totals(now=None):
    """Get total CO₂ reclaimed from all offsets.

    Returns dict with carbon_capture_kg, tree_kg, total_reclaimed_kg/g,
    plus a list of all offset records for display.
    """
    offsets = CarbonOffset.query.order_by(desc(CarbonOffset.purchase_date)).all()
    carbon_capture_kg = 0.0
    tree_kg = 0.0
    offset_list = []

    for o in offsets:
        tree_reclaimed = 0.0
        capture_kg = 0.0
        if o.offset_type == "carbon_capture":
            capture_kg = o.amount_kg or 0
            carbon_capture_kg += capture_kg
        elif o.offset_type == "tree_planting":
            tree_reclaimed = calculate_tree_reclaimed_kg(o, now)
            tree_kg += tree_reclaimed

        offset_list.append({
            "id": o.id,
            "type": o.offset_type,
            "title": o.title,
            "description": o.description or "",
            "amount_kg": capture_kg,
            "tree_count": o.tree_count or 0,
            "tree_reclaimed_kg": round(tree_reclaimed, 4),
            "reference_number": o.reference_number or "",
            "cost_gbp": o.cost_gbp,
            "purchase_date": o.purchase_date.isoformat() if o.purchase_date else None,
            "has_certificate": o.certificate_image is not None,
            "certificate_url": f"/api/carbon-offsets/{o.id}/certificate" if o.certificate_image else None,
        })

    total_kg = carbon_capture_kg + tree_kg
    return {
        "carbon_capture_kg": round(carbon_capture_kg, 4),
        "tree_kg": round(tree_kg, 4),
        "total_reclaimed_kg": round(total_kg, 4),
        "total_reclaimed_g": round(total_kg * 1000, 2),
        "offsets": offset_list,
    }


# ── Routes ──────────────────────────────────────────────────────────────────

@carbon_offsets_bp.route("/api/carbon-offsets")
def api_list_offsets():
    """Public JSON API listing all carbon offset records (transparency)."""
    totals = get_reclaimed_totals()
    return jsonify({
        "carbon_capture_kg": totals["carbon_capture_kg"],
        "tree_kg": totals["tree_kg"],
        "total_reclaimed_kg": totals["total_reclaimed_kg"],
        "total_reclaimed_g": totals["total_reclaimed_g"],
        "offsets": totals["offsets"],
    })


@carbon_offsets_bp.route("/api/carbon-offsets/<int:offset_id>/certificate")
def api_certificate(offset_id):
    """Serve a certificate image."""
    from flask import Response
    offset = db.session.get(CarbonOffset, offset_id)
    if not offset or not offset.certificate_image:
        return Response("Not found", status=404)
    return Response(
        offset.certificate_image,
        mimetype=offset.certificate_image_mime or "image/png",
    )
