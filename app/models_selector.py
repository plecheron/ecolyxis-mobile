"""Multi-model selector: returns available model tiers for the UI."""
from flask import Blueprint, jsonify, current_app

models_selector_bp = Blueprint("models_selector", __name__)

AVAILABLE_MODELS = [
    {
        "id": "ecolyxis-standard",
        "name": "Standard",
        "description": "Balanced quality and speed for everyday tasks",
        "context_window": 8192,
        "tier": "free",
    },
    {
        "id": "ecolyxis-quick",
        "name": "Quick",
        "description": "Fastest responses, ideal for simple questions",
        "context_window": 4096,
        "tier": "free",
    },
    {
        "id": "ecolyxis-sprint",
        "name": "Sprint",
        "description": "Lightning-fast with expert knowledge lookup and smart escalation",
        "context_window": 4096,
        "tier": "free",
    },
    {
        "id": "ecolyxis-long",
        "name": "Long Context",
        "description": "Handles long documents and complex conversations",
        "context_window": 32768,
        "tier": "premium",
    },
    {
        "id": "ecolyxis-precise",
        "name": "Precise",
        "description": "Maximum accuracy for technical and analytical work",
        "context_window": 8192,
        "tier": "premium",
    },
    {
        "id": "ecolyxis-vision",
        "name": "Vision",
        "description": "Analyse images alongside text",
        "context_window": 8192,
        "tier": "premium",
    },
]


@models_selector_bp.route("/api/models")
def list_available_models():
    """Return available models for the UI selector."""
    return jsonify({"models": AVAILABLE_MODELS})
