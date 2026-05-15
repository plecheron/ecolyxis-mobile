from flask import Blueprint, render_template, jsonify, request
from flask_login import login_required, current_user
from app import db
from app.models import User, Thread, Message
from datetime import datetime, timezone, timedelta
from sqlalchemy import func, text
import subprocess
import os
import threading
import time
import collections

admin_bp = Blueprint("admin", __name__, url_prefix="/admin")

# ── Background LLM metrics sampler ───────────────────────────────────
# Stores (timestamp, gen_tps, prompt_tps, processing, deferred) every 5 seconds.
# Kept in memory; lost on restart (acceptable for live dashboards).

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
            import os as _os
            base = _os.environ.get("LLM_BASE_URL", "http://10.0.0.1:8081/v1")
            # Derive metrics URL: strip /v1, add /metrics on same host:port
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

ADMIN_USERNAMES = os.environ.get("ADMIN_USERNAMES", "ashley").split(",")


def admin_required(f):
    """Decorator: must be logged in AND be admin (user ID 1 or in ADMIN_USERNAMES)."""
    from functools import wraps
    @wraps(f)
    def decorated(*args, **kwargs):
        if not current_user.is_authenticated:
            from flask import redirect, url_for, request
            return redirect(url_for("auth.login", next=request.url))
        if current_user.id != 1 and current_user.username not in ADMIN_USERNAMES:
            from flask import abort
            abort(403)
        return f(*args, **kwargs)
    return decorated


# ── helpers ──────────────────────────────────────────────────────────

def _system_stats():
    stats = {}
    try:
        with open("/proc/uptime") as f:
            up_secs = float(f.read().split()[0])
            stats["uptime_seconds"] = up_secs
            stats["uptime"] = _format_uptime(up_secs)

        stats["load_avg"] = list(os.getloadavg())

        mem = {}
        with open("/proc/meminfo") as f:
            for line in f:
                k, v = line.split(":")
                mem[k.strip()] = int(v.strip().split()[0])
        total_kb = mem.get("MemTotal", 0)
        avail_kb = mem.get("MemAvailable", 0)
        used_kb = total_kb - avail_kb
        stats["memory_total_mb"] = round(total_kb / 1024)
        stats["memory_used_mb"] = round(used_kb / 1024)
        stats["memory_pct"] = round(used_kb / total_kb * 100, 1) if total_kb else 0
        swap_total = mem.get("SwapTotal", 0)
        swap_free = mem.get("SwapFree", 0)
        stats["swap_total_mb"] = round(swap_total / 1024)
        stats["swap_used_mb"] = round((swap_total - swap_free) / 1024)
        stats["swap_pct"] = round((swap_total - swap_free) / swap_total * 100, 1) if swap_total else 0

        st = os.statvfs("/")
        total = st.f_blocks * st.f_frsize
        free = st.f_bfree * st.f_frsize
        used = total - free
        stats["disk_total_gb"] = round(total / 1e9, 1)
        stats["disk_used_gb"] = round(used / 1e9, 1)
        stats["disk_pct"] = round(used / total * 100, 1) if total else 0

        try:
            with open("/proc/cpuinfo") as f:
                for line in f:
                    if line.startswith("model name"):
                        stats["cpu_model"] = line.split(":")[1].strip()
                        break
        except Exception:
            stats["cpu_model"] = "Unknown"

        stats["cpu_cores"] = os.cpu_count() or 1

    except Exception as e:
        stats["error"] = str(e)
    return stats


def _format_uptime(seconds):
    days = int(seconds // 86400)
    hours = int((seconds % 86400) // 3600)
    mins = int((seconds % 3600) // 60)
    parts = []
    if days:
        parts.append(f"{days}d")
    if hours:
        parts.append(f"{hours}h")
    parts.append(f"{mins}m")
    return " ".join(parts)


def _service_status(name):
    try:
        result = subprocess.run(
            ["systemctl", "is-active", name],
            capture_output=True, text=True, timeout=5
        )
        return result.stdout.strip()
    except Exception:
        return "unknown"


def _app_stats():
    now = datetime.now(timezone.utc)
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    week_ago = now - timedelta(days=7)

    total_users = User.query.count()
    premium_users = User.query.filter_by(tier="premium").count()
    total_threads = Thread.query.count()
    total_messages = Message.query.count()
    total_tokens = db.session.query(db.func.sum(Message.tokens_used)).scalar() or 0

    users_today = User.query.filter(User.created_at >= today_start).count()
    messages_today = Message.query.filter(Message.created_at >= today_start).count()
    tokens_today = (
        db.session.query(db.func.coalesce(db.func.sum(Message.tokens_used), 0))
        .filter(Message.created_at >= today_start).scalar() or 0
    )

    active_week = (
        db.session.query(db.func.count(db.func.distinct(Thread.user_id)))
        .join(Message, Message.thread_id == Thread.id)
        .filter(Message.created_at >= week_ago)
        .scalar() or 0
    )

    avg_msgs = round(total_messages / total_users, 1) if total_users else 0

    recent_users = User.query.order_by(User.created_at.desc()).limit(10).all()

    recent_messages_raw = (
        db.session.query(Message, Thread, User)
        .join(Thread, Message.thread_id == Thread.id)
        .join(User, Thread.user_id == User.id)
        .order_by(Message.created_at.desc())
        .limit(20)
        .all()
    )

    recent_messages = []
    for msg, thread, user in recent_messages_raw:
        recent_messages.append({
            "created_at": msg.created_at,
            "username": user.username,
            "role": msg.role,
            "content": msg.content,
            "tokens_used": msg.tokens_used,
        })

    return {
        "total_users": total_users,
        "premium_users": premium_users,
        "free_users": total_users - premium_users,
        "total_threads": total_threads,
        "total_messages": total_messages,
        "total_tokens": int(total_tokens),
        "users_today": users_today,
        "messages_today": messages_today,
        "tokens_today": int(tokens_today),
        "active_users_week": active_week,
        "avg_messages_per_user": avg_msgs,
        "recent_users": recent_users,
        "recent_messages": recent_messages,
    }


def _llm_health():
    try:
        import requests
        from flask import current_app
        base = current_app.config.get("LLM_BASE_URL", "http://10.0.0.1:8081/v1")
        models_url = base.rstrip("/") + "/models"
        r = requests.get(models_url, timeout=5)
        if r.status_code == 200:
            data = r.json()
            models = [m.get("id", "unknown") for m in data.get("data", [])]
            return {"status": "online", "models": models}
        return {"status": "error", "code": r.status_code}
    except Exception as e:
        return {"status": "offline", "error": str(e)[:100]}


def _error_stats():
    errors = {"last_hour": 0, "last_24h": 0, "recent": []}
    try:
        r = subprocess.run(
            ["journalctl", "-u", "ecolyxis", "--no-pager", "--since", "1 hour ago"],
            capture_output=True, text=True, timeout=5,
        )
        errors["last_hour"] = r.stdout.count("ERROR in app")

        r = subprocess.run(
            ["journalctl", "-u", "ecolyxis", "--no-pager", "--since", "24 hours ago"],
            capture_output=True, text=True, timeout=5,
        )
        errors["last_24h"] = r.stdout.count("ERROR in app")

        lines = r.stdout.splitlines()
        error_lines = [l for l in lines if "ERROR in app" in l or "Exception on" in l or "TypeError" in l or "Traceback" in l]
        for line in error_lines[-20:]:
            if "Exception on" in line:
                route = line.split("Exception on ")[-1].strip() if "Exception on " in line else ""
                ts = ""
                for part in line.split():
                    if part.startswith("2026-") or part.startswith("2025-"):
                        ts = part
                        break
                errors["recent"].append({"route": route, "time": ts})
            elif "TypeError" in line or "Error:" in line.split(":")[-1] if ":" in line else False:
                if errors["recent"] and not errors["recent"][-1].get("error"):
                    clean = line.split(":")[-1].strip() if ":" in line else line.strip()
                    errors["recent"][-1]["error"] = clean
    except Exception:
        pass
    return errors


def _llm_metrics():
    try:
        import requests
        from flask import current_app
        base = current_app.config.get("LLM_BASE_URL", "http://10.0.0.1:8081/v1")
        metrics_url = base.rstrip("/").rsplit("/v1", 1)[0].rstrip("/") + "/metrics"
        r = requests.get(metrics_url, timeout=5)
        if r.status_code != 200:
            return {"status": "error", "code": r.status_code}
        metrics = {}
        for line in r.text.splitlines():
            if line.startswith("#") or not line.strip():
                continue
            key, _, val = line.partition(" ")
            try:
                metrics[key] = float(val)
            except ValueError:
                pass
        return {
            "status": "online",
            "prompt_tokens_total": int(metrics.get("llamacpp:prompt_tokens_total", 0)),
            "prompt_seconds_total": round(metrics.get("llamacpp:prompt_seconds_total", 0), 2),
            "tokens_predicted_total": int(metrics.get("llamacpp:tokens_predicted_total", 0)),
            "tokens_predicted_seconds_total": round(metrics.get("llamacpp:tokens_predicted_seconds_total", 0), 2),
            "prompt_tokens_per_sec": round(metrics.get("llamacpp:prompt_tokens_seconds", 0), 1),
            "gen_tokens_per_sec": round(metrics.get("llamacpp:predicted_tokens_seconds", 0), 1),
            "requests_processing": int(metrics.get("llamacpp:requests_processing", 0)),
            "requests_deferred": int(metrics.get("llamacpp:requests_deferred", 0)),
            "n_busy_slots": int(metrics.get("llamacpp:n_busy_slots_per_decode", 0)),
            "n_decode_total": int(metrics.get("llamacpp:n_decode_total", 0)),
            "n_tokens_max": int(metrics.get("llamacpp:n_tokens_max", 0)),
        }
    except Exception as e:
        return {"status": "offline", "error": str(e)[:100]}


def _llm_error_stats():
    stats = {"last_hour": 0, "last_24h": 0, "last_error": None, "down": False}
    try:
        r = subprocess.run(
            ["journalctl", "-u", "ecolyxis", "--no-pager", "--since", "1 hour ago"],
            capture_output=True, text=True, timeout=5,
        )
        stats["last_hour"] = r.stdout.count("LLM backend error:")

        r = subprocess.run(
            ["journalctl", "-u", "ecolyxis", "--no-pager", "--since", "24 hours ago"],
            capture_output=True, text=True, timeout=5,
        )
        stats["last_24h"] = r.stdout.count("LLM backend error:")

        for line in reversed(r.stdout.splitlines()):
            if "LLM backend error:" in line:
                stats["last_error"] = line.split("LLM backend error:")[-1].strip()[:200]
                break
    except Exception:
        pass
    return stats


# ── Analytics helpers ────────────────────────────────────────────────

def _day_group(col):
    """SQLite-compatible day grouping."""
    return func.date_trunc("day", col)


def _token_chart_data(days=30):
    """Token usage grouped by day for the last N days."""
    now = datetime.now(timezone.utc)
    start = now - timedelta(days=days)
    rows = (
        db.session.query(
            _day_group(Message.created_at).label("day"),
            func.coalesce(func.sum(Message.tokens_used), 0).label("tokens"),
            func.count(Message.id).label("messages"),
        )
        .filter(Message.created_at >= start)
        .group_by(_day_group(Message.created_at))
        .order_by(text("day"))
        .all()
    )
    result = []
    for r in rows:
        result.append({
            "date": r.day if r.day else None,
            "tokens": int(r.tokens),
            "messages": int(r.messages),
        })
    return result


def _user_chart_data(days=30):
    """Signups and active users grouped by day."""
    now = datetime.now(timezone.utc)
    start = now - timedelta(days=days)

    # Signups per day
    signups = (
        db.session.query(
            _day_group(User.created_at).label("day"),
            func.count(User.id).label("count"),
        )
        .filter(User.created_at >= start)
        .group_by(_day_group(User.created_at))
        .order_by(text("day"))
        .all()
    )
    signup_map = {r.day: int(r.count) for r in signups}

    # Active users per day (users who sent at least 1 message that day)
    active = (
        db.session.query(
            _day_group(Message.created_at).label("day"),
            func.count(func.distinct(Thread.user_id)).label("count"),
        )
        .join(Thread, Message.thread_id == Thread.id)
        .filter(Message.created_at >= start, Message.role == "user")
        .group_by(_day_group(Message.created_at))
        .order_by(text("day"))
        .all()
    )
    active_map = {r.day: int(r.count) for r in active}

    # Build complete day range
    days_list = []
    for i in range(days):
        d = (start + timedelta(days=i)).strftime("%Y-%m-%d")
        days_list.append({
            "date": d,
            "signups": signup_map.get(d, 0),
            "active": active_map.get(d, 0),
        })
    return days_list


def _top_users(limit=20):
    """Top users by total token usage."""
    rows = (
        db.session.query(
            User.username,
            User.email,
            User.tier,
            User.created_at,
            func.coalesce(func.sum(Message.tokens_used), 0).label("total_tokens"),
            func.count(Message.id).label("total_messages"),
            func.max(Message.created_at).label("last_active"),
        )
        .join(Thread, Thread.user_id == User.id)
        .join(Message, Message.thread_id == Thread.id)
        .group_by(User.id)
        .order_by(func.sum(Message.tokens_used).desc())
        .limit(limit)
        .all()
    )
    result = []
    for r in rows:
        result.append({
            "username": r.username,
            "email": r.email,
            "tier": r.tier,
            "created_at": r.created_at.isoformat() if r.created_at else None,
            "total_tokens": int(r.total_tokens),
            "total_messages": int(r.total_messages),
            "last_active": r.last_active.isoformat() if r.last_active else None,
        })
    return result


# ── routes ───────────────────────────────────────────────────────────

@admin_bp.route("/")
@login_required
@admin_required
def index():
    sys_stats = _system_stats()
    app_stats = _app_stats()
    llm = _llm_health()

    services = {
        "ecolyxis (gunicorn)": _service_status("ecolyxis"),
        "caddy": _service_status("caddy"),
        "fail2ban": _service_status("fail2ban"),
    }

    return render_template(
        "admin/index.html",
        sys=sys_stats,
        app=app_stats,
        llm=llm,
        services=services,
        errors=_error_stats(),
        llm_errors=_llm_error_stats(),
        token_chart=_token_chart_data(30),
        user_chart=_user_chart_data(30),
        top_users=_top_users(20),
        now=datetime.now(timezone.utc),
    )


@admin_bp.route("/api/llm-metrics")
@login_required
@admin_required
def api_llm_metrics():
    """Live LLM metrics — polled every 2s by the dashboard."""
    return jsonify(_llm_metrics())


@admin_bp.route("/api/errors")
@login_required
@admin_required
def api_errors():
    """Error stats — polled every 30s."""
    return jsonify(_error_stats())


@admin_bp.route("/api/llm-errors")
@login_required
@admin_required
def api_llm_errors():
    """LLM backend error stats."""
    return jsonify(_llm_error_stats())


@admin_bp.route("/api/stats")
@login_required
@admin_required
def api_stats():
    """JSON endpoint for live refresh."""
    app_stats = _app_stats()
    users_list = []
    for u in app_stats.pop("recent_users"):
        users_list.append({
            "username": u.username,
            "email": u.email,
            "tier": u.tier,
            "created_at": u.created_at.isoformat() if u.created_at else None,
        })
    app_stats["recent_users"] = users_list

    for m in app_stats["recent_messages"]:
        if isinstance(m.get("created_at"), datetime):
            m["created_at"] = m["created_at"].isoformat()

    return jsonify({
        "system": _system_stats(),
        "app": app_stats,
        "llm": _llm_health(),
        "services": {
            "ecolyxis (gunicorn)": _service_status("ecolyxis"),
            "caddy": _service_status("caddy"),
            "fail2ban": _service_status("fail2ban"),
        },
        "timestamp": datetime.now(timezone.utc).isoformat(),
    })


@admin_bp.route("/api/llm-history")
@login_required
@admin_required
def api_llm_history():
    """Historical LLM gen speed for charts."""
    minutes = request.args.get("minutes", "60", type=str)
    seconds_map = {"1": 60, "60": 3600, "1440": 86400}
    seconds = seconds_map.get(minutes, 3600)
    raw = _metrics_sampler.get_range(seconds)
    points = []
    for ts, gen_tps, prompt_tps, proc, deferred in raw:
        points.append({
            "t": round(ts),
            "gen": gen_tps,
            "prompt": prompt_tps,
            "proc": proc,
            "deferred": deferred,
        })
    return jsonify({"points": points})
