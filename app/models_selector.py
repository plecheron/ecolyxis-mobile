"""Multi-model selector: returns available model tiers for the UI."""
from flask import Blueprint, jsonify, current_app

models_selector_bp = Blueprint("models_selector", __name__)

AVAILABLE_MODELS = [
    {
        "id": "ecolyxis-standard",
        "name": "Standard",
        "description": "Balanced quality and speed (GLM-4.7 Flash)",
        "context_window": 200000,
        "tier": "free",
    },
    {
        "id": "ecolyxis-scatterbrain",
        "name": "Scatterbrain",
        "description": "Maximum capability for complex tasks (Qwen3.6-35B-A3B)",
        "context_window": 200000,
        "tier": "premium",
    },
]


@models_selector_bp.route("/api/models")
def list_available_models():
    """Return available models for the UI selector."""
    return jsonify({"models": AVAILABLE_MODELS})
