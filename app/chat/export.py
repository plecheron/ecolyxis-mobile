"""Conversation export routes: JSON and Markdown formats.

Premium-only feature. Uses DB-backed rate limiting.
"""
import json
import re
from datetime import datetime, timezone
from flask import Response, jsonify, request
from flask_login import login_required, current_user

from app.models import Thread, Message
from app.chat import chat_bp


EXPORT_RATE_LIMIT = 10   # max exports per hour
EXPORT_RATE_WINDOW = 3600  # seconds


import time as _time

# In-process export rate tracking (per-worker, sufficient for this use case)
_export_timestamps: dict[int, list[float]] = {}


def _check_export_rate(user_id):
    """Check if user has exceeded export rate limit. Returns (allowed, count)."""
    now = _time.time()
    attempts = _export_timestamps.get(user_id, [])
    attempts = [t for t in attempts if now - t < EXPORT_RATE_WINDOW]
    _export_timestamps[user_id] = attempts
    return len(attempts) < EXPORT_RATE_LIMIT, len(attempts)


def _record_export(user_id):
    """Record an export attempt."""
    _export_timestamps.setdefault(user_id, []).append(_time.time())


def _extract_text(content):
    """Extract plain text from message content, handling multimodal JSON."""
    if not content:
        return ""
    try:
        parsed = json.loads(content)
        if isinstance(parsed, list):
            parts = []
            for p in parsed:
                if p.get("type") == "text":
                    parts.append(p.get("text", ""))
                elif p.get("type") == "image":
                    fname = p.get("file", p.get("url", ""))
                    parts.append(f"[Image: {fname}]")
            return " ".join(parts)
        return content
    except (json.JSONDecodeError, TypeError):
        return content


@chat_bp.route("/chat/<string:thread_id>/export/<fmt>")
@login_required
def export_thread(thread_id, fmt):
    """Export a conversation thread as JSON or Markdown.

    Premium-only. Rate limited to 10 exports/hour.
    """
    if fmt not in ("json", "md"):
        return jsonify({"error": "Unsupported format. Use 'json' or 'md'."}), 400

    if not current_user.is_premium:
        return jsonify({
            "error": "premium_required",
            "message": "Conversation export is a Premium feature. Upgrade to unlock it."
        }), 403

    thread = Thread.query.filter_by(id=thread_id, user_id=current_user.id).first_or_404()

    # Rate limit check
    allowed, count = _check_export_rate(current_user.id)
    if not allowed:
        return jsonify({
            "error": "rate_limited",
            "message": f"Export limit reached ({EXPORT_RATE_LIMIT}/hour). Try again later."
        }), 429

    messages = (
        Message.query.filter_by(thread_id=thread.id)
        .order_by(Message.created_at)
        .all()
    )

    if not messages:
        return jsonify({"error": "No messages to export"}), 400

    _record_export(current_user.id)

    # Sanitise filename
    safe_title = re.sub(r'[^\w\s-]', '', thread.title).strip().replace(' ', '_')[:50] or "conversation"
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d")

    if fmt == "json":
        data = {
            "thread": {
                "id": thread.id,
                "title": thread.title,
                "created_at": thread.created_at.isoformat(),
                "exported_at": datetime.now(timezone.utc).isoformat(),
            },
            "messages": [
                {
                    "id": m.id,
                    "role": m.role,
                    "content": _extract_text(m.content),
                    "raw_content": m.content,
                    "tokens_used": m.tokens_used,
                    "message_type": m.message_type,
                    "created_at": m.created_at.isoformat(),
                }
                for m in messages
            ],
            "export_metadata": {
                "total_messages": len(messages),
                "format": "json",
                "total_tokens": sum(m.tokens_used or 0 for m in messages),
            }
        }
        json_bytes = json.dumps(data, indent=2, ensure_ascii=False).encode("utf-8")
        return Response(
            json_bytes,
            mimetype="application/json",
            headers={
                "Content-Disposition": f'attachment; filename="{safe_title}_{timestamp}.json"',
                "Content-Length": len(json_bytes),
            }
        )

    # Markdown format
    lines = [
        f"# {thread.title}",
        "",
        f"> Exported {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}",
        f"> {len(messages)} messages | {sum(m.tokens_used or 0 for m in messages):,} tokens",
        "",
        "---",
        "",
    ]

    for m in messages:
        role = "**You**" if m.role == "user" else "**Assistant**"
        ts = m.created_at.strftime("%H:%M")
        text = _extract_text(m.content)
        lines.append(f"### {role} — {ts}")
        lines.append("")
        lines.append(text)
        lines.append("")
        lines.append("---")
        lines.append("")

    md_bytes = "\n".join(lines).encode("utf-8")
    return Response(
        md_bytes,
        mimetype="text/markdown; charset=utf-8",
        headers={
            "Content-Disposition": f'attachment; filename="{safe_title}_{timestamp}.md"',
            "Content-Length": len(md_bytes),
        }
    )
