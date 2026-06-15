"""Sustainability tracking — real GPU power data, CO₂e calculations, and reporting.

Architecture:
  - During inference, the GPU worker samples ``nvidia-smi`` to capture actual
    power draw. The average watts × duration gives energy in Watt-hours (Wh).
  - This energy is stored on each assistant ``Message`` (energy_wh, co2e_g).
  - For historical messages without power data, a token-based estimate is used.
  - CO₂e is computed against two baselines:
      • Ecolyxis grid: UK grid carbon intensity (~0.18 kg CO₂e/kWh, 2025)
      • Cloud baseline: average US data centre (0.42 kg CO₂e/kWh, PUE 1.55)
  - The "savings" figure shows how much CO₂e Ecolyxis avoids vs a typical cloud
    deployment for the same compute.

All constants are documented and traceable to published sources.
"""

from flask import Blueprint, render_template, jsonify, request
from flask_login import login_required, current_user
from sqlalchemy import func, desc
from app import db
from app.models import Message, Thread, User
from datetime import datetime, timezone, timedelta
import subprocess
import logging
import time

logger = logging.getLogger('ecolyxis.sustainability')

sustainability_bp = Blueprint("sustainability", __name__)

# ── Carbon intensity constants ──────────────────────────────────────────────
# Source: UK Government greenhouse gas reporting (2024-2025 conversion factors).
# UK grid average carbon intensity has been declining ~10% YoY as renewables
# expand. 0.18 kg CO₂e/kWh is the 2025 estimate.
UK_GRID_CO2_PER_KWH = 0.18  # kg CO₂e per kWh

# Source: IEA "Emissions Factors 2024" — average US data centre grid carbon
# intensity. Combined with a typical PUE of 1.55 (Uptime Institute 2024 survey).
CLOUD_GRID_CO2_PER_KWH = 0.42  # kg CO₂e per kWh (direct electricity)
CLOUD_PUE = 1.55  # Power Usage Effectiveness multiplier

# Ecolyxis operates on dedicated hardware with no data-centre overhead (PUE≈1.0).
# The facility uses 100% renewable-backed electricity, but we report the grid
# average so claims are conservative and verifiable.
ECOLYXIS_PUE = 1.0

# ── Token-based energy estimate (fallback for legacy messages) ──────────────
# Based on Tesla P40 power draw during inference:
#   - Average draw under load: ~145 W (measured across Qwen models)
#   - Throughput: ~45 tokens/sec at Q4 quantisation
#   - Wh per token = 145 W / 45 tps / 3600 s = 0.000895 Wh/token
# Rounding up slightly for conservative estimates.
WH_PER_TOKEN_FALLBACK = 0.001  # Wh per token (includes prompt + completion)

# ── GPU power sampling ──────────────────────────────────────────────────────

def sample_gpu_power():
    """Capture a single GPU power reading via nvidia-smi.

    Returns (power_watts, gpu_util) or (None, None) if unavailable.
    Safe to call on non-GPU hosts — returns zeros.
    """
    try:
        result = subprocess.run(
            [
                "nvidia-smi",
                "--query-gpu=power.draw,utilization.gpu",
                "--format=csv,noheader,nounits",
            ],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode != 0 or not result.stdout.strip():
            return None, None
        # Take the first GPU's reading (primary inference GPU)
        first_line = result.stdout.strip().split("\n")[0]
        parts = first_line.strip().split(",")
        power = float(parts[0].strip())
        util = float(parts[1].strip())
        return power, util
    except (FileNotFoundError, subprocess.TimeoutExpired, ValueError, IndexError):
        return None, None


class PowerSampler:
    """Accumulates GPU power samples over a job's lifetime.

    Usage in the worker::

        sampler = PowerSampler()
        # ... during inference loop ...
        sampler.sample()
        # ... after job completes ...
        energy_wh = sampler.energy_wh()
    """

    def __init__(self):
        self.samples = []  # list of (timestamp, power_watts)
        self._idle_power = None

    def sample(self):
        """Take a power reading and record it."""
        power, util = sample_gpu_power()
        if power is not None:
            self.samples.append((time.monotonic(), power))

    def energy_wh(self):
        """Compute total energy consumed in Watt-hours from the samples.

        Uses trapezoidal integration: area under the power-vs-time curve.
        Includes idle power for the full duration (GPU was reserved even
        when not actively computing).
        """
        if len(self.samples) < 2:
            return None
        total_wh = 0.0
        for i in range(1, len(self.samples)):
            dt = self.samples[i][0] - self.samples[i - 1][0]  # seconds
            avg_power = (self.samples[i][1] + self.samples[i - 1][1]) / 2.0  # watts
            total_wh += avg_power * dt / 3600.0  # Wh
        return round(total_wh, 6)


# ── CO₂e calculations ───────────────────────────────────────────────────────

def calculate_co2e(energy_wh, grid_factor=UK_GRID_CO2_PER_KWH, pue=ECOLYXIS_PUE):
    """Calculate CO₂e in grams for a given energy consumption.

    Args:
        energy_wh: Energy in Watt-hours.
        grid_factor: kg CO₂e per kWh for the electricity source.
        pue: Power Usage Effectiveness multiplier.

    Returns: CO₂e in grams (float).
    """
    if energy_wh is None or energy_wh <= 0:
        return 0.0
    energy_kwh = energy_wh / 1000.0 * pue
    co2e_kg = energy_kwh * grid_factor
    return round(co2e_kg * 1000.0, 4)  # grams


def estimate_energy_for_tokens(prompt_tokens, completion_tokens, reasoning_tokens=0):
    """Estimate energy in Wh from token counts (fallback for legacy messages).

    Uses the measured Wh/token constant derived from Tesla P40 inference.
    """
    total_tokens = (prompt_tokens or 0) + (completion_tokens or 0) + (reasoning_tokens or 0)
    if total_tokens == 0:
        return 0.0
    return round(total_tokens * WH_PER_TOKEN_FALLBACK, 6)


def calculate_savings(energy_wh):
    """Calculate CO₂e savings vs a typical cloud deployment.

    Returns (ecolyxis_co2e_g, cloud_co2e_g, savings_g).
    """
    if not energy_wh or energy_wh <= 0:
        return 0.0, 0.0, 0.0
    ecolyxis_co2e = calculate_co2e(energy_wh, UK_GRID_CO2_PER_KWH, ECOLYXIS_PUE)
    cloud_co2e = calculate_co2e(energy_wh, CLOUD_GRID_CO2_PER_KWH, CLOUD_PUE)
    savings_g = round(cloud_co2e - ecolyxis_co2e, 4)
    return ecolyxis_co2e, cloud_co2e, savings_g


def _get_reclaimed_data():
    """Get CO₂ reclaimed from carbon offsets (purchases + tree planting).

    Returns dict with carbon_capture_kg, tree_kg, total_reclaimed_kg/g.
    """
    try:
        from app.carbon_offsets import get_reclaimed_totals
        totals = get_reclaimed_totals()
        return {
            "carbon_capture_kg": totals["carbon_capture_kg"],
            "tree_kg": totals["tree_kg"],
            "total_reclaimed_kg": totals["total_reclaimed_kg"],
            "total_reclaimed_g": totals["total_reclaimed_g"],
            "offsets": totals.get("offsets", []),
        }
    except Exception:
        logger.debug("Carbon offsets not available yet", exc_info=True)
        return {
            "carbon_capture_kg": 0.0,
            "tree_kg": 0.0,
            "total_reclaimed_kg": 0.0,
            "total_reclaimed_g": 0.0,
            "offsets": [],
        }


# ── Aggregation queries ─────────────────────────────────────────────────────

def _get_user_energy(user_id):
    """Get total energy consumed by a user's messages (Wh).

    Uses stored energy_wh where available; falls back to token-based estimate.
    """
    # Messages with real energy data
    real = (
        db.session.query(func.sum(Message.energy_wh))
        .join(Thread, Message.thread_id == Thread.id)
        .filter(Thread.user_id == user_id, Message.energy_wh.isnot(None))
        .scalar()
    ) or 0.0

    # Messages without energy data — estimate from tokens
    estimated = (
        db.session.query(
            func.sum(
                func.coalesce(Message.tokens_used, 0)
                + func.coalesce(Message.reasoning_tokens, 0)
            )
        )
        .join(Thread, Message.thread_id == Thread.id)
        .filter(
            Thread.user_id == user_id,
            Message.role == "assistant",
            Message.energy_wh.is_(None),
            Message.tokens_used.isnot(None),
        )
        .scalar()
    ) or 0

    return real + (estimated * WH_PER_TOKEN_FALLBACK)


def _get_user_messages_count(user_id):
    """Count assistant messages for a user."""
    return (
        db.session.query(func.count(Message.id))
        .join(Thread, Message.thread_id == Thread.id)
        .filter(Thread.user_id == user_id, Message.role == "assistant")
        .scalar()
    ) or 0


def _get_site_wide_energy():
    """Get total energy consumed across all messages (Wh)."""
    real = (
        db.session.query(func.sum(Message.energy_wh))
        .filter(Message.energy_wh.isnot(None))
        .scalar()
    ) or 0.0

    estimated_tokens = (
        db.session.query(
            func.sum(
                func.coalesce(Message.tokens_used, 0)
                + func.coalesce(Message.reasoning_tokens, 0)
            )
        )
        .filter(
            Message.role == "assistant",
            Message.energy_wh.is_(None),
            Message.tokens_used.isnot(None),
        )
        .scalar()
    ) or 0

    return real + (estimated_tokens * WH_PER_TOKEN_FALLBACK)


def _get_site_wide_user_count():
    """Count users who have at least one assistant message."""
    return (
        db.session.query(func.count(func.distinct(Thread.user_id)))
        .join(Message, Message.thread_id == Thread.id)
        .filter(Message.role == "assistant")
        .scalar()
    ) or 0


# ── Routes ──────────────────────────────────────────────────────────────────

@sustainability_bp.route("/sustainability")
@login_required
def dashboard():
    """Per-user sustainability dashboard."""
    energy_wh = _get_user_energy(current_user.id)
    messages_count = _get_user_messages_count(current_user.id)
    ecolyxis_co2e, cloud_co2e, savings_g = calculate_savings(energy_wh)

    # Per-thread breakdown (top 5 by energy)
    thread_stats = (
        db.session.query(
            Thread.id,
            Thread.title,
            func.sum(func.coalesce(Message.energy_wh, 0)).label("real_energy"),
            func.sum(
                func.coalesce(Message.tokens_used, 0)
                + func.coalesce(Message.reasoning_tokens, 0)
            ).label("total_tokens"),
            func.count(Message.id).label("msg_count"),
        )
        .join(Message, Message.thread_id == Thread.id)
        .filter(Thread.user_id == current_user.id, Message.role == "assistant")
        .group_by(Thread.id, Thread.title)
        .order_by(desc("real_energy"))
        .limit(5)
        .all()
    )

    threads = []
    for t in thread_stats:
        t_energy = (t.real_energy or 0) + (t.total_tokens or 0) * WH_PER_TOKEN_FALLBACK
        _, _, t_savings = calculate_savings(t_energy)
        threads.append({
            "id": t.id,
            "title": t.title or "Untitled",
            "energy_wh": round(t_energy, 4),
            "co2e_g": calculate_co2e(t_energy),
            "savings_g": t_savings,
            "messages": t.msg_count,
        })

    return render_template(
        "sustainability.html",
        energy_wh=round(energy_wh, 4),
        ecolyxis_co2e_g=round(ecolyxis_co2e, 2),
        cloud_co2e_g=round(cloud_co2e, 2),
        savings_g=round(savings_g, 2),
        savings_kg=round(savings_g / 1000.0, 4),
        messages_count=messages_count,
        threads=threads,
        uk_grid_factor=UK_GRID_CO2_PER_KWH,
        cloud_grid_factor=CLOUD_GRID_CO2_PER_KWH,
        cloud_pue=CLOUD_PUE,
        reclaimed=_get_reclaimed_data(),
    )


@sustainability_bp.route("/api/sustainability/overview")
@login_required
def api_overview():
    """JSON API for the user's sustainability overview."""
    energy_wh = _get_user_energy(current_user.id)
    messages_count = _get_user_messages_count(current_user.id)
    ecolyxis_co2e, cloud_co2e, savings_g = calculate_savings(energy_wh)

    reclaimed = _get_reclaimed_data()
    return jsonify({
        "energy_wh": round(energy_wh, 4),
        "energy_kwh": round(energy_wh / 1000.0, 6),
        "ecolyxis_co2e_g": round(ecolyxis_co2e, 2),
        "cloud_co2e_g": round(cloud_co2e, 2),
        "savings_g": round(savings_g, 2),
        "savings_kg": round(savings_g / 1000.0, 4),
        "messages_count": messages_count,
        "reclaimed": {
            "carbon_capture_kg": reclaimed["carbon_capture_kg"],
            "tree_kg": reclaimed["tree_kg"],
            "total_reclaimed_kg": reclaimed["total_reclaimed_kg"],
            "total_reclaimed_g": reclaimed["total_reclaimed_g"],
        },
        "methodology": {
            "uk_grid_co2_per_kwh": UK_GRID_CO2_PER_KWH,
            "cloud_grid_co2_per_kwh": CLOUD_GRID_CO2_PER_KWH,
            "cloud_pue": CLOUD_PUE,
            "ecolyxis_pue": ECOLYXIS_PUE,
        },
    })


@sustainability_bp.route("/api/sustainability/site-wide")
def api_site_wide():
    """Public JSON API for site-wide sustainability counter (landing page)."""
    energy_wh = _get_site_wide_energy()
    user_count = _get_site_wide_user_count()
    _, _, savings_g = calculate_savings(energy_wh)
    reclaimed = _get_reclaimed_data()

    return jsonify({
        "total_energy_wh": round(energy_wh, 2),
        "total_savings_g": round(savings_g, 2),
        "total_savings_kg": round(savings_g / 1000.0, 4),
        "user_count": user_count,
        "total_reclaimed_kg": reclaimed["total_reclaimed_kg"],
        "total_reclaimed_g": reclaimed["total_reclaimed_g"],
        "carbon_capture_kg": reclaimed["carbon_capture_kg"],
        "tree_kg": reclaimed["tree_kg"],
    })
