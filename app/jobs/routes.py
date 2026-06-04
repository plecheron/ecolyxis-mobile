"""HTTP surface for async generation jobs.

* ``POST /jobs/chat/<thread_id>`` — validate, persist the user message, create a
  GenerationJob, enqueue it, and return ``{job_id, stream_url}`` immediately.
* ``GET  /jobs/<job_id>`` — JSON status snapshot.
* ``GET  /jobs/<job_id>/stream`` — resumable SSE. The browser's EventSource
  reconnects with ``Last-Event-ID`` and we replay from the next sequence, so a
  dropped connection loses nothing. Generation itself runs in the worker, fully
  decoupled from this request.
"""
import json

from flask import Blueprint, Response, current_app, jsonify, request
from flask_login import current_user, login_required

from app import db
from app.models import GenerationJob, Thread
from app.chat import check_rate_limit, save_user_message
from app.jobs import enqueue, read_events

jobs_bp = Blueprint("jobs", __name__)


def _sse(generator):
    return Response(
        generator,
        mimetype="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@jobs_bp.route("/jobs/chat/<string:thread_id>", methods=["POST"])
@login_required
def submit_chat(thread_id):
    thread = Thread.query.filter_by(id=thread_id, user_id=current_user.id).first_or_404()

    allowed, used, limit = check_rate_limit()
    if not allowed:
        return jsonify({
            "error": "rate_limited",
            "message": f"Free tier limit reached ({limit} messages per hour). "
                       "Upgrade to Premium for unlimited.",
            "used": used,
            "limit": limit,
        }), 429

    data = request.get_json(silent=True) or {}
    content = (data.get("content") or "").strip()
    images = data.get("images") or []
    mode = data.get("mode", "standard")

    if not content and not images:
        return jsonify({"error": "Empty message"}), 400

    save_user_message(thread, content, images)

    job = GenerationJob(
        user_id=current_user.id,
        thread_id=thread.id,
        kind="chat",
        status="queued",
        is_premium=current_user.is_premium,
        params={"mode": mode, "precise": mode == "precise", "show_thinking": True},
    )
    db.session.add(job)
    db.session.commit()

    enqueue(job.id, job.is_premium)

    return jsonify({
        "job_id": job.id,
        "status": "queued",
        "stream_url": f"/jobs/{job.id}/stream",
    }), 202


@jobs_bp.route("/jobs/<string:job_id>")
@login_required
def job_status(job_id):
    job = GenerationJob.query.filter_by(id=job_id, user_id=current_user.id).first_or_404()
    return jsonify({
        "job_id": job.id,
        "kind": job.kind,
        "status": job.status,
        "result": job.result,
        "error": job.error,
        "created_at": job.created_at.isoformat() if job.created_at else None,
    })


@jobs_bp.route("/jobs/<string:job_id>/stream")
@login_required
def job_stream(job_id):
    job = GenerationJob.query.filter_by(id=job_id, user_id=current_user.id).first_or_404()

    # Resume point: EventSource sends Last-Event-ID automatically on reconnect;
    # ?last_id= is the manual fallback. "0" replays the whole log from the start.
    last_id = request.headers.get("Last-Event-ID") or request.args.get("last_id") or "0"

    # Snapshot terminal state now, so that if the event log has already expired
    # (job finished > TTL ago) we can still synthesize a final event.
    initial_status = job.status
    initial_result = job.result
    initial_error = job.error

    def _synthesize_terminal():
        if initial_status == "done":
            final = {"type": "done", **(initial_result or {})}
        else:
            final = {"type": "error", "message": initial_error or "Generation failed"}
        return f"data: {json.dumps(final, ensure_ascii=False)}\n\n"

    def gen():
        cursor = last_id

        # Fast path: a finished job replays instantly (no blocking). If its log
        # has already expired, synthesize the outcome from the DB snapshot.
        if initial_status in GenerationJob.TERMINAL:
            for seq_id, event in read_events(job_id, last_id=cursor, block_ms=None):
                yield f"id: {seq_id}\n"
                yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"
                if event.get("type") in ("done", "error"):
                    return
            yield _synthesize_terminal()
            return

        # In-progress: block-read and forward, resuming clients from `cursor`.
        idle = 0
        while True:
            events = read_events(job_id, last_id=cursor, block_ms=15000, count=500)
            if not events:
                idle += 1
                if idle > 40:  # ~10 min with no activity — give up; client may retry
                    return
                yield ": keepalive\n\n"
                continue

            idle = 0
            for seq_id, event in events:
                cursor = seq_id
                yield f"id: {seq_id}\n"
                yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"
                if event.get("type") in ("done", "error"):
                    return

    return _sse(gen())
