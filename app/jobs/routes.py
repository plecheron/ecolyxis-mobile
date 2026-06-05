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
from app.models import GenerationJob, GeneratedImage, Thread
from app.chat import check_rate_limit, save_user_message
from app.jobs import enqueue, read_events

jobs_bp = Blueprint("jobs", __name__)


def _sse(generator):
    return Response(
        generator,
        mimetype="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


def _rate_limited():
    _, used, limit = check_rate_limit()
    return jsonify({
        "error": "rate_limited",
        "message": f"Free tier limit reached ({limit} messages per hour). "
                   "Upgrade to Premium for unlimited.",
        "used": used, "limit": limit,
    }), 429


def _enqueue_job(thread, kind, params):
    """Create + enqueue a GenerationJob, returning the 202 submit response."""
    job = GenerationJob(
        user_id=current_user.id, thread_id=thread.id, kind=kind,
        status="queued", is_premium=current_user.is_premium, params=params,
    )
    db.session.add(job)
    db.session.commit()
    enqueue(job.id, job.is_premium)
    return jsonify({"job_id": job.id, "status": "queued",
                    "stream_url": f"/jobs/{job.id}/stream"}), 202


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


@jobs_bp.route("/jobs/image/<string:thread_id>", methods=["POST"])
@login_required
def submit_image(thread_id):
    thread = Thread.query.filter_by(id=thread_id, user_id=current_user.id).first_or_404()
    allowed, _, _ = check_rate_limit()
    if not allowed:
        return _rate_limited()
    data = request.get_json(silent=True) or {}
    prompt = (data.get("prompt") or "").strip()
    if not prompt:
        return jsonify({"error": "Empty prompt"}), 400
    return _enqueue_job(thread, "image", {
        "prompt": prompt,
        "width": int(data.get("width", 1024)),
        "height": int(data.get("height", 1024)),
        "seed": int(data.get("seed", -1)),
    })


@jobs_bp.route("/jobs/upscale/<string:thread_id>", methods=["POST"])
@login_required
def submit_upscale(thread_id):
    thread = Thread.query.filter_by(id=thread_id, user_id=current_user.id).first_or_404()
    data = request.get_json(silent=True) or {}
    image_id = data.get("image_id")
    if not image_id:
        return jsonify({"error": "image_id required"}), 400
    img = GeneratedImage.query.filter_by(id=image_id, user_id=current_user.id).first_or_404()
    next_size = img.next_size()
    if next_size is None:
        return jsonify({"error": "Already at maximum size"}), 400
    return _enqueue_job(thread, "upscale", {
        "prompt": img.prompt, "seed": img.seed,
        "next_size": next_size, "parent_image_id": img.id,
    })


@jobs_bp.route("/jobs/edit/<string:thread_id>", methods=["POST"])
@login_required
def submit_edit(thread_id):
    thread = Thread.query.filter_by(id=thread_id, user_id=current_user.id).first_or_404()
    allowed, _, _ = check_rate_limit()
    if not allowed:
        return _rate_limited()
    data = request.get_json(silent=True) or {}
    prompt = (data.get("prompt") or "").strip()
    image = data.get("image", "")
    if not prompt:
        return jsonify({"error": "Edit instruction required"}), 400
    if not image:
        return jsonify({"error": "Source image required"}), 400
    return _enqueue_job(thread, "edit", {
        "prompt": prompt, "image": image,
        "source_image_id": data.get("source_image_id"),
        "size": int(data.get("size", 512)), "steps": int(data.get("steps", 4)),
        "cfg": float(data.get("cfg", 6.0)), "seed": int(data.get("seed", -1)),
    })


@jobs_bp.route("/jobs/video/<string:thread_id>", methods=["POST"])
@login_required
def submit_video(thread_id):
    thread = Thread.query.filter_by(id=thread_id, user_id=current_user.id).first_or_404()
    allowed, _, _ = check_rate_limit()
    if not allowed:
        return _rate_limited()
    data = request.get_json(silent=True) or {}
    prompt = (data.get("prompt") or "").strip()
    if not prompt:
        return jsonify({"error": "Empty prompt"}), 400
    return _enqueue_job(thread, "video", {
        "prompt": prompt,
        "width": int(data.get("width", 480)),
        "height": int(data.get("height", 480)),
        "frames": int(data.get("frames", 33)),
    })


@jobs_bp.route("/jobs/animate/<string:thread_id>", methods=["POST"])
@login_required
def submit_animate(thread_id):
    thread = Thread.query.filter_by(id=thread_id, user_id=current_user.id).first_or_404()
    allowed, _, _ = check_rate_limit()
    if not allowed:
        return _rate_limited()
    data = request.get_json(silent=True) or {}
    prompt = (data.get("prompt") or "").strip()
    image_url = (data.get("image_url") or "").strip()
    if not prompt or not image_url:
        return jsonify({"error": "Missing prompt or image_url"}), 400
    return _enqueue_job(thread, "animate", {"prompt": prompt, "image_url": image_url})


@jobs_bp.route("/jobs/active")
@login_required
def active_jobs():
    """All of the current user's in-flight jobs — drives the sidebar
    "generating" indicators and resume-on-open of the live thinking stream."""
    jobs = (
        GenerationJob.query
        .filter(GenerationJob.user_id == current_user.id)
        .filter(GenerationJob.status.notin_(GenerationJob.TERMINAL))
        .all()
    )
    return jsonify({
        "jobs": [
            {"job_id": j.id, "thread_id": j.thread_id, "kind": j.kind, "status": j.status}
            for j in jobs
        ]
    })


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
