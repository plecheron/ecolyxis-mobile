"""Expert registry and API client for Sprint orchestration.

Loads expert definitions from experts.toml, provides expert descriptions
for Sprint's system prompt, and handles expert API calls.
"""
import logging
import os
from pathlib import Path

import requests
import tomllib  # Python 3.11+

logger = logging.getLogger('ecolyxis.experts')

# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

_REGISTRY = None


def _find_experts_toml():
    """Locate experts.toml relative to the app root."""
    candidates = [
        Path(os.environ.get("EXPERTS_TOML", "")),
        Path(__file__).parent.parent / "experts.toml",   # ecolyxis/experts.toml
        Path("/opt/Ecolyxis/experts.toml"),
    ]
    for p in candidates:
        if p.is_file():
            return p
    return None


def load_registry():
    """Load and cache the expert registry from experts.toml."""
    global _REGISTRY
    if _REGISTRY is not None:
        return _REGISTRY

    toml_path = _find_experts_toml()
    if not toml_path:
        logger.warning("No experts.toml found — Sprint will have no experts")
        _REGISTRY = {"experts": {}}
        return _REGISTRY

    try:
        with open(toml_path, "rb") as f:
            raw = tomllib.load(f)
        experts = {}
        for key, cfg in raw.get("expert", {}).items():
            if not cfg.get("enabled", True):
                continue
            experts[key] = {
                "name": cfg.get("name", key),
                "description": cfg.get("description", ""),
                "endpoint": cfg.get("endpoint", ""),
            }
            logger.info("Loaded expert '%s' → %s", key, cfg.get("endpoint"))
        _REGISTRY = {"experts": experts}
    except Exception as e:
        logger.error("Failed to load experts.toml: %s", e)
        _REGISTRY = {"experts": {}}

    return _REGISTRY


def reload_registry():
    """Force reload of the expert registry (e.g. after config change)."""
    global _REGISTRY
    _REGISTRY = None
    return load_registry()


def get_expert_descriptions():
    """Return a formatted string of expert descriptions for Sprint's system prompt."""
    registry = load_registry()
    experts = registry.get("experts", {})
    if not experts:
        return ""

    lines = ["You have access to the following experts:"]
    for key, cfg in experts.items():
        lines.append(f"  - {key}: {cfg['description']}")
    return "\n".join(lines)


def get_expert(name):
    """Get a single expert config by name."""
    registry = load_registry()
    return registry.get("experts", {}).get(name)


def list_expert_names():
    """Return list of registered expert names."""
    registry = load_registry()
    return list(registry.get("experts", {}).keys())


# ---------------------------------------------------------------------------
# Expert API Client
# ---------------------------------------------------------------------------

def call_expert(name, question, context=None, timeout=120):
    """Call an expert endpoint with a natural language question.

    Args:
        name: Expert name (must be in the registry)
        question: Natural language question from Sprint
        context: Optional conversation context
        timeout: Request timeout in seconds

    Returns:
        dict with keys: answer (str), confidence (float), sources (list)
    Raises:
        ValueError if expert not found, requests exceptions on API failure
    """
    expert = get_expert(name)
    if not expert:
        raise ValueError(f"Unknown expert: {name}")

    endpoint = expert["endpoint"]
    payload = {"question": question}
    if context:
        payload["context"] = context

    logger.info("Calling expert '%s' at %s: %s", name, endpoint, question[:100])

    resp = requests.post(endpoint, json=payload, timeout=timeout)
    resp.raise_for_status()
    data = resp.json()

    result = {
        "answer": data.get("answer", ""),
        "confidence": data.get("confidence", 0.0),
        "sources": data.get("sources", []),
        "expert_name": expert.get("name", name),
    }

    logger.info("Expert '%s' responded: confidence=%.3f, sources=%d",
                name, result["confidence"], len(result["sources"]))

    return result
