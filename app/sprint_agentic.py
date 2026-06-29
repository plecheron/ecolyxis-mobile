"""Sprint agentic routes — session management + task orchestration.

The web frontend calls these endpoints to drive the Sprint flow:
  1. POST /api/sprint/start          — create session, submit first question job
  2. POST /api/sprint/<id>/answer    — submit user answers, next question round
  3. POST /api/sprint/<id>/decompose — trigger task decomposition
  4. POST /api/sprint/<id>/tasks     — persist decomposed tasks
  5. POST /api/sprint/<id>/task/<tid>/dispatch  — execute a single task
  6. POST /api/sprint/<id>/task/<tid>/result    — save result, find unblocked
  7. POST /api/sprint/<id>/task/<tid>/retry     — retry a failed task
  8. POST /api/sprint/<id>/assemble   — submit artifact assembly job
  9. POST /api/sprint/<id>/artifact   — save final artifact
 10. GET  /api/sprint/<id>            — full session state (resume/disconnect)
"""
import json
import logging
from datetime import datetime, timezone

from flask import Blueprint, request, jsonify, session as flask_session
from flask_login import current_user, login_required

from app import db
from app.models import Thread, SprintSession, SprintTask

sprint_agentic_bp = Blueprint("sprint_agentic", __name__)
log = logging.getLogger("ecolyxis.sprint_agentic")


def _submit_remote_job(kind, params, client_ref=None):
    """Submit a job to ecolyxis-api and return {job_id, stream_url}."""
    from app.jobs.api_client import submit_job
    api_job_id = submit_job(kind, params, client_ref=client_ref)
    return {
        "job_id": api_job_id,
        "stream_url": f"/api/v1/jobs/{api_job_id}/stream",
    }


@sprint_agentic_bp.route("/api/sprint/start", methods=["POST"])
@login_required
def start_sprint_session():
    """Create a SprintSession and submit the first question round."""
    data = request.get_json(silent=True) or {}
    thread_id = data.get("thread_id")
    prompt = (data.get("prompt") or "").strip()

    if not thread_id or not prompt:
        return jsonify({"error": "thread_id and prompt required"}), 400

    thread = Thread.query.filter_by(id=thread_id, user_id=current_user.id).first_or_404()

    sprint_session = SprintSession(
        thread_id=thread_id,
        user_id=current_user.id,
        original_prompt=prompt,
        refined_prompt=prompt,
        state="questioning",
        qa_history="[]",
    )
    db.session.add(sprint_session)
    db.session.commit()

    job = _submit_remote_job("sprint_question", {
        "original_prompt": prompt,
        "qa_history": [],
    }, client_ref=sprint_session.id)

    return jsonify({
        "session_id": sprint_session.id,
        "job_id": job["job_id"],
        "stream_url": job["stream_url"],
        "state": "questioning",
    }), 202


@sprint_agentic_bp.route("/api/sprint/<session_id>/answer", methods=["POST"])
@login_required
def submit_answers(session_id):
    """Submit user's answers to the current question round."""
    sprint_session = SprintSession.query.filter_by(
        id=session_id, user_id=current_user.id
    ).first_or_404()
    data = request.get_json(silent=True) or {}
    answers = data.get("answers", [])

    if sprint_session.state != "questioning":
        return jsonify({
            "error": f"session is in state '{sprint_session.state}', not 'questioning'"
        }), 400

    qa = json.loads(sprint_session.qa_history)
    if qa:
        qa[-1]["answers"] = answers
    sprint_session.qa_history = json.dumps(qa)
    db.session.commit()

    job = _submit_remote_job("sprint_question", {
        "original_prompt": sprint_session.original_prompt,
        "qa_history": qa,
    }, client_ref=sprint_session.id)

    return jsonify({
        "job_id": job["job_id"],
        "stream_url": job["stream_url"],
        "state": "questioning",
    }), 202


@sprint_agentic_bp.route("/api/sprint/<session_id>/decompose", methods=["POST"])
@login_required
def decompose_tasks(session_id):
    """Trigger task decomposition for a session that's ready."""
    sprint_session = SprintSession.query.filter_by(
        id=session_id, user_id=current_user.id
    ).first_or_404()
    qa = json.loads(sprint_session.qa_history)

    sprint_session.state = "decomposing"
    db.session.commit()

    job = _submit_remote_job("sprint_decompose", {
        "original_prompt": sprint_session.original_prompt,
        "qa_history": qa,
    }, client_ref=sprint_session.id)

    return jsonify({
        "job_id": job["job_id"],
        "stream_url": job["stream_url"],
        "state": "decomposing",
    }), 202


@sprint_agentic_bp.route("/api/sprint/<session_id>/tasks", methods=["POST"])
@login_required
def create_tasks(session_id):
    """Create SprintTask records from decomposition results."""
    sprint_session = SprintSession.query.filter_by(
        id=session_id, user_id=current_user.id
    ).first_or_404()
    data = request.get_json(silent=True) or {}
    tasks = data.get("tasks", [])

    for i, t in enumerate(tasks):
        task = SprintTask(
            session_id=session_id,
            order=i,
            title=t.get("title", f"Task {i+1}"),
            description=t.get("description", ""),
            depends_on=json.dumps(t.get("depends_on", [])),
            status="pending",
        )
        db.session.add(task)

    sprint_session.state = "executing"
    db.session.commit()

    # Return the created tasks with IDs
    created = SprintTask.query.filter_by(session_id=session_id).order_by(SprintTask.order).all()
    return jsonify({
        "session_id": session_id,
        "state": "executing",
        "task_count": len(created),
        "tasks": [
            {
                "id": t.id,
                "title": t.title,
                "description": t.description,
                "depends_on": json.loads(t.depends_on),
                "order": t.order,
                "status": t.status,
            }
            for t in created
        ],
    })


@sprint_agentic_bp.route("/api/sprint/<session_id>/task/<task_id>/dispatch", methods=["POST"])
@login_required
def dispatch_task(session_id, task_id):
    """Submit a single task for execution."""
    sprint_session = SprintSession.query.filter_by(
        id=session_id, user_id=current_user.id
    ).first_or_404()
    task = SprintTask.query.filter_by(
        session_id=session_id, id=task_id
    ).first_or_404()

    # Gather parent results
    deps = json.loads(task.depends_on)
    parent_results = []
    for dep_id in deps:
        parent = SprintTask.query.get(dep_id)
        if parent and parent.status == "completed":
            parent_results.append({"title": parent.title, "result": parent.result})

    task.status = "running"
    task.started_at = datetime.now(timezone.utc)
    db.session.commit()

    job = _submit_remote_job("sprint_task", {
        "original_prompt": sprint_session.original_prompt,
        "task_title": task.title,
        "task_description": task.description,
        "parent_results": parent_results,
    }, client_ref=sprint_session.id)

    task.job_id = job["job_id"]
    db.session.commit()

    return jsonify({
        "task_id": task_id,
        "job_id": job["job_id"],
        "stream_url": job["stream_url"],
    })


@sprint_agentic_bp.route("/api/sprint/<session_id>/task/<task_id>/result", methods=["POST"])
@login_required
def save_task_result(session_id, task_id):
    """Save task execution result and check for newly unblocked tasks."""
    task = SprintTask.query.filter_by(
        session_id=session_id, id=task_id
    ).first_or_404()
    data = request.get_json(silent=True) or {}
    result_text = data.get("result", "")
    status = data.get("status", "completed")

    task.result = result_text
    task.status = status
    task.completed_at = datetime.now(timezone.utc) if status == "completed" else None
    db.session.commit()

    # Find newly unblocked tasks
    all_tasks = SprintTask.query.filter_by(session_id=session_id).all()
    unblocked = []
    for t in all_tasks:
        if t.status == "pending":
            deps = json.loads(t.depends_on)
            dep_tasks = [SprintTask.query.get(dep_id) for dep_id in deps]
            if all(d and d.status == "completed" for d in dep_tasks):
                unblocked.append(t.id)

    all_done = all(t.status in ("completed", "failed") for t in all_tasks)

    return jsonify({
        "task_id": task_id,
        "status": status,
        "unblocked_tasks": unblocked,
        "all_complete": all_done,
    })


@sprint_agentic_bp.route("/api/sprint/<session_id>/task/<task_id>/retry", methods=["POST"])
@login_required
def retry_task(session_id, task_id):
    """Retry a failed task."""
    task = SprintTask.query.filter_by(
        session_id=session_id, id=task_id
    ).first_or_404()
    task.status = "retrying"
    task.error = None
    task.result = None
    db.session.commit()
    return dispatch_task(session_id, task_id)


@sprint_agentic_bp.route("/api/sprint/<session_id>/assemble", methods=["POST"])
@login_required
def assemble_artifact(session_id):
    """Submit the artifact assembly job."""
    sprint_session = SprintSession.query.filter_by(
        id=session_id, user_id=current_user.id
    ).first_or_404()
    tasks = SprintTask.query.filter_by(
        session_id=session_id
    ).order_by(SprintTask.order).all()
    qa = json.loads(sprint_session.qa_history)

    task_results = [
        {"title": t.title, "result": t.result}
        for t in tasks if t.status == "completed" and t.result
    ]

    sprint_session.state = "assembling"
    db.session.commit()

    job = _submit_remote_job("sprint_assemble", {
        "original_prompt": sprint_session.original_prompt,
        "qa_history": qa,
        "task_results": task_results,
    }, client_ref=sprint_session.id)

    return jsonify({
        "job_id": job["job_id"],
        "stream_url": job["stream_url"],
        "state": "assembling",
    })


@sprint_agentic_bp.route("/api/sprint/<session_id>/artifact", methods=["POST"])
@login_required
def save_artifact(session_id):
    """Save the final artifact and mark session complete."""
    sprint_session = SprintSession.query.filter_by(
        id=session_id, user_id=current_user.id
    ).first_or_404()
    data = request.get_json(silent=True) or {}
    sprint_session.artifact_markdown = data.get("artifact", "")
    sprint_session.state = "complete"
    db.session.commit()

    # Also save as an assistant message in the thread
    from app.models import Message
    msg = Message(
        thread_id=sprint_session.thread_id,
        role="assistant",
        content=sprint_session.artifact_markdown,
        message_type="text",
    )
    db.session.add(msg)
    db.session.commit()

    return jsonify({"state": "complete"})


@sprint_agentic_bp.route("/api/sprint/<session_id>", methods=["GET"])
@login_required
def get_session_status(session_id):
    """Get full session state including tasks — for resume/disconnect recovery."""
    sprint_session = SprintSession.query.filter_by(
        id=session_id, user_id=current_user.id
    ).first_or_404()
    tasks = SprintTask.query.filter_by(
        session_id=session_id
    ).order_by(SprintTask.order).all()

    return jsonify({
        "session_id": session_id,
        "state": sprint_session.state,
        "original_prompt": sprint_session.original_prompt,
        "qa_history": json.loads(sprint_session.qa_history),
        "artifact": sprint_session.artifact_markdown,
        "error": sprint_session.error,
        "tasks": [
            {
                "id": t.id,
                "title": t.title,
                "description": t.description,
                "depends_on": json.loads(t.depends_on),
                "status": t.status,
                "result": t.result,
                "job_id": t.job_id,
                "order": t.order,
            }
            for t in tasks
        ],
    })


# Add SSE proxy at the end of sprint_agentic.py

@sprint_agentic_bp.route("/api/sprint/stream/<api_job_id>")
@login_required
def sprint_stream_proxy(api_job_id):
    """Proxy SSE stream from the API server for sprint jobs."""
    from flask import Response
    import requests as _requests
    from app.jobs.api_client import _base_url, _headers

    def generate():
        url = f"{_base_url()}/jobs/{api_job_id}/stream"
        resp = _requests.get(url, headers=_headers(), stream=True, timeout=3600)
        if resp.status_code != 200:
            yield f"data: {json.dumps({'type': 'error', 'message': f'API stream failed: HTTP {resp.status_code}'})}\n\n"
            return
        resp.encoding = "utf-8"
        for raw in resp.iter_lines(decode_unicode=True):
            if not raw:
                continue
            if raw.startswith(":"):
                continue
            if raw.startswith("id: "):
                continue
            if raw.startswith("data: "):
                yield f"{raw}\n\n"

    return Response(generate(), mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})
