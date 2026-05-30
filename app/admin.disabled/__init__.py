"""Admin dashboard package.

Owns the blueprint, the admin_required decorator, and the background
LLM metrics sampler. Helpers for system/app/LLM stats live in
metrics.py; HTTP routes in routes.py.
"""
import os
import threading
import time
import collections
from functools import wraps
from flask import Blueprint, redirect, url_for, request, abort
from flask_login import current_user

admin_bp = Blueprint("admin", __name__, url_prefix="/admin")

ADMIN_USERNAMES = os.environ.get("ADMIN_USERNAMES", "ashley").split(",")


def admin_required(f):
    """Decorator: must be logged in AND be admin (user ID 1 or in ADMIN_USERNAMES)."""
    @wraps(f)
    def decorated(*args, **kwargs):
        if not current_user.is_authenticated:
            return redirect(url_for("auth.login", next=request.url))
        if current_user.id != 1 and current_user.username not in ADMIN_USERNAMES:
            abort(403)
        return f(*args, **kwargs)
    return decorated


# ── Background LLM metrics sampler ───────────────────────────────────
# Stores (timestamp, gen_tps, prompt_tps, processing, deferred) every 5
# seconds. Kept in memory; lost on restart (acceptable for live dashboards).

class _MetricsSampler:
    INTERVAL = 5  # seconds
    MAX_POINTS = 17280  # 24h at 5s intervals

    def __init__(self):
        self._lock = threading.Lock()
        self._data = collections.deque(maxlen=self.MAX_POINTS)
        self._thread = None
        self._started = False

    def start(self):
        if self._started:
            return
        self._started = True
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def _run(self):
        while True:
            try:
                self._sample()
            except Exception:
                pass
            time.sleep(self.INTERVAL)

    def _sample(self):
        try:
            import requests
            base = os.environ.get("LLM_BASE_URL", "http://10.0.0.1:8081/v1")
            metrics_base = base.rstrip("/").rsplit("/v1", 1)[0].rstrip("/")
            r = requests.get(metrics_base + "/metrics", timeout=5)
            if r.status_code != 200:
                return
            metrics = {}
            for line in r.text.splitlines():
                if line.startswith("#") or not line.strip():
                    continue
                key, _, val = line.partition(" ")
                try:
                    metrics[key] = float(val)
                except ValueError:
                    pass
            entry = (
                time.time(),
                round(metrics.get("llamacpp:predicted_tokens_seconds", 0), 2),
                round(metrics.get("llamacpp:prompt_tokens_seconds", 0), 2),
                int(metrics.get("llamacpp:requests_processing", 0)),
                int(metrics.get("llamacpp:requests_deferred", 0)),
            )
            with self._lock:
                self._data.append(entry)
        except Exception:
            pass

    def get_range(self, seconds):
        cutoff = time.time() - seconds
        with self._lock:
            return [e for e in self._data if e[0] >= cutoff]


_metrics_sampler = _MetricsSampler()
_metrics_sampler.start()


from app.admin import metrics, routes, tests  # noqa: E402,F401 — register routes
