"""Helpers that gather stats for the admin dashboard.

System stats are pulled from /proc and systemctl. App stats come from
the DB. LLM stats come from the llama.cpp prometheus-style /metrics
endpoint. Chart helpers build day-grouped time series.
"""
import os
import subprocess
from datetime import datetime, timezone, timedelta
from sqlalchemy import func, text
from flask import current_app

from app import db
from app.models import User, Thread, Message


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

        # Per-core CPU usage
        try:
            import psutil
            stats["cpu_per_core"] = psutil.cpu_percent(percpu=True, interval=0)
        except Exception:
            pass

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


def _recent_api_requests(limit=20):
    """Fetch recent API usage records with user/key info."""
    try:
        from app.models import ApiUsage, ApiKey
        rows = (
            db.session.query(ApiUsage, ApiKey, User)
            .join(ApiKey, ApiUsage.api_key_id == ApiKey.id)
            .join(User, ApiKey.user_id == User.id)
            .order_by(ApiUsage.created_at.desc())
            .limit(limit)
            .all()
        )
        return [
            {
                "created_at": usage.created_at,
                "username": user.username,
                "key_name": api_key.name,
                "endpoint": usage.endpoint,
                "model": usage.model,
                "tokens_prompt": usage.tokens_prompt,
                "tokens_completion": usage.tokens_completion,
            }
            for usage, api_key, user in rows
        ]
    except Exception:
        return []


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
        "recent_api_requests": _recent_api_requests(),
    }


def _llm_health():
    try:
        import requests
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
    return [
        {"date": r.day if r.day else None, "tokens": int(r.tokens), "messages": int(r.messages)}
        for r in rows
    ]


def _user_chart_data(days=30):
    """Signups and active users grouped by day."""
    now = datetime.now(timezone.utc)
    start = now - timedelta(days=days)

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
    return [
        {
            "username": r.username,
            "email": r.email,
            "tier": r.tier,
            "created_at": r.created_at.isoformat() if r.created_at else None,
            "total_tokens": int(r.total_tokens),
            "total_messages": int(r.total_messages),
            "last_active": r.last_active.isoformat() if r.last_active else None,
        }
        for r in rows
    ]
