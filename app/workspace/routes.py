from flask import request, jsonify, Response, current_app
from flask_login import login_required, current_user
from app import db
from app.models import Workspace, Thread, Message
from . import workspace_bp
from .summaries import generate_thread_summary
from app.queue import enter_queue, leave_queue
from app.llm import LLMClient
from app.chat import check_rate_limit

import json
import logging
logger = logging.getLogger("ecolyxis.workspace.routes")


def _get_workspace_context_by_id(workspace_id, max_chars=2000):
    """Build workspace context string given just a workspace_id (no thread needed)."""
    workspace = Workspace.query.get(workspace_id)
    if not workspace:
        return None

    header = f"## Workspace: {workspace.name}"
    if workspace.description:
        header += f"\n{workspace.description}"

    siblings = (
        Thread.query
        .filter(Thread.workspace_id == workspace_id)
        .order_by(Thread.updated_at.desc())
        .all()
    )

    sections = []
    total_len = 0

    for sib in siblings:
        summary_text = ""
        if sib.summary:
            summary_text = sib.summary
        else:
            first_msg = (
                Message.query
                .filter_by(thread_id=sib.id, role="user")
                .order_by(Message.created_at)
                .first()
            )
            if first_msg and first_msg.content:
                preview = Thread._extract_text(first_msg.content)[:200]
                summary_text = preview
            if not summary_text and sib.title and sib.title != "New Chat":
                summary_text = sib.title

        if not summary_text:
            continue

        section = f"### {sib.title or 'Untitled'}\n{summary_text}"
        section_len = len(section) + 1

        if total_len + section_len > max_chars:
            break

        sections.append(section)
        total_len += section_len

    parts = [header]
    if sections:
        parts.append("")
        parts.append("## Related Conversations")
        parts.append("You are in a workspace with multiple related conversations. "
                      "Here are summaries of the other conversations in this workspace:")
        parts.append("")
        parts.append("\n\n".join(sections))
        parts.append("")
        parts.append("Use this context to provide consistent, informed responses across conversations.")

    return "\n".join(parts)


@workspace_bp.route('', methods=['GET'])
@login_required
def list_workspaces():
    """List all workspaces for the current user with thread counts."""
    workspaces = Workspace.query.filter_by(user_id=current_user.id)\
        .order_by(Workspace.updated_at.desc()).all()
    return jsonify([{
        'id': w.id,
        'name': w.name,
        'description': w.description,
        'created_at': w.created_at.isoformat() if w.created_at else None,
        'updated_at': w.updated_at.isoformat() if w.updated_at else None,
        'thread_count': w.threads.count(),
    } for w in workspaces])


@workspace_bp.route('', methods=['POST'])
@login_required
def create_workspace():
    """Create a new workspace."""
    data = request.get_json()
    if not data:
        return jsonify({'error': 'JSON body required'}), 400
    name = data.get('name', '').strip()
    if not name:
        return jsonify({'error': 'Name is required'}), 400

    existing = Workspace.query.filter_by(user_id=current_user.id, name=name).first()
    if existing:
        return jsonify({'error': 'Workspace with this name already exists'}), 409

    w = Workspace(user_id=current_user.id, name=name, description=data.get('description'))
    db.session.add(w)
    db.session.commit()
    return jsonify({'id': w.id, 'name': w.name, 'description': w.description}), 201


@workspace_bp.route('/<workspace_id>', methods=['GET'])
@login_required
def get_workspace(workspace_id):
    """Get a single workspace with its threads."""
    w = Workspace.query.filter_by(id=workspace_id, user_id=current_user.id).first_or_404()
    threads = w.threads.order_by(Thread.updated_at.desc()).all()
    return jsonify({
        'id': w.id,
        'name': w.name,
        'description': w.description,
        'created_at': w.created_at.isoformat() if w.created_at else None,
        'updated_at': w.updated_at.isoformat() if w.updated_at else None,
        'thread_count': w.threads.count(),
        'threads': [{
            'id': t.id,
            'title': t.title,
            'summary': t.summary,
            'updated_at': t.updated_at.isoformat() if t.updated_at else None,
            'message_count': t.messages.count(),
        } for t in threads],
    })


@workspace_bp.route('/<workspace_id>', methods=['PATCH'])
@login_required
def update_workspace(workspace_id):
    """Rename or update description of a workspace."""
    w = Workspace.query.filter_by(id=workspace_id, user_id=current_user.id).first_or_404()
    data = request.get_json()
    if not data:
        return jsonify({'error': 'JSON body required'}), 400
    if 'name' in data:
        name = data['name'].strip()
        if not name:
            return jsonify({'error': 'Name cannot be empty'}), 400
        existing = Workspace.query.filter_by(user_id=current_user.id, name=name).first()
        if existing and existing.id != w.id:
            return jsonify({'error': 'Workspace with this name already exists'}), 409
        w.name = name
    if 'description' in data:
        w.description = data['description']
    db.session.commit()
    return jsonify({'id': w.id, 'name': w.name, 'description': w.description})


@workspace_bp.route('/<workspace_id>', methods=['DELETE'])
@login_required
def delete_workspace(workspace_id):
    """Delete a workspace. Threads become unassigned."""
    w = Workspace.query.filter_by(id=workspace_id, user_id=current_user.id).first_or_404()
    Thread.query.filter_by(workspace_id=w.id).update({'workspace_id': None})
    db.session.delete(w)
    db.session.commit()
    return jsonify({'success': True})


@workspace_bp.route('/<workspace_id>/threads', methods=['GET'])
@login_required
def list_workspace_threads(workspace_id):
    """List threads in a workspace."""
    w = Workspace.query.filter_by(id=workspace_id, user_id=current_user.id).first_or_404()
    threads = w.threads.order_by(Thread.updated_at.desc()).all()
    return jsonify([{
        'id': t.id,
        'title': t.title,
        'summary': t.summary,
        'updated_at': t.updated_at.isoformat() if t.updated_at else None,
        'message_count': t.messages.count(),
    } for t in threads])


@workspace_bp.route('/<workspace_id>/threads/<thread_id>', methods=['PUT'])
@login_required
def assign_thread_to_workspace(workspace_id, thread_id):
    """Assign or move a thread to a workspace.

    After assigning, generates a summary for the assigned thread and also
    generates summaries for any other threads in the workspace that don't
    yet have one.
    """
    w = Workspace.query.filter_by(id=workspace_id, user_id=current_user.id).first_or_404()
    t = Thread.query.filter_by(id=thread_id, user_id=current_user.id).first_or_404()
    t.workspace_id = w.id
    db.session.commit()

    # Generate summary for the assigned thread
    summary = None
    try:
        summary = generate_thread_summary(t)
    except Exception as e:
        logger.warning("Failed to generate summary for thread %s: %s", t.id, e)

    # Generate summaries for other threads in the workspace that lack one
    try:
        other_threads = (
            Thread.query
            .filter(
                Thread.workspace_id == w.id,
                Thread.id != t.id,
                Thread.summary.is_(None),
            )
            .all()
        )
        for ot in other_threads:
            try:
                generate_thread_summary(ot)
            except Exception as e:
                logger.warning("Failed to generate summary for thread %s: %s", ot.id, e)
    except Exception as e:
        logger.warning("Error generating summaries for other threads: %s", e)

    return jsonify({
        'success': True,
        'summary': summary,
    })


@workspace_bp.route('/threads/<thread_id>', methods=['DELETE'])
@login_required
def unassign_thread(thread_id):
    """Remove a thread from its workspace."""
    t = Thread.query.filter_by(id=thread_id, user_id=current_user.id).first_or_404()
    t.workspace_id = None
    db.session.commit()
    return jsonify({'success': True})


@workspace_bp.route('/threads/<thread_id>/summarize', methods=['POST'])
@login_required
def summarize_thread(thread_id):
    """Explicitly generate (or regenerate) a summary for a thread."""
    t = Thread.query.filter_by(id=thread_id, user_id=current_user.id).first_or_404()
    summary = generate_thread_summary(t)
    if summary:
        return jsonify({'success': True, 'summary': summary})
    return jsonify({'error': 'Could not generate summary'}), 500


@workspace_bp.route('/threads/<thread_id>/toggle-context', methods=['POST'])
@login_required
def toggle_workspace_context(thread_id):
    """Toggle use_workspace_context on a thread."""
    t = Thread.query.filter_by(id=thread_id, user_id=current_user.id).first_or_404()
    t.use_workspace_context = not t.use_workspace_context
    db.session.commit()
    return jsonify({'success': True, 'use_workspace_context': t.use_workspace_context})


# ===== Ephemeral Chat ===== no DB persistence =====

@workspace_bp.route('/<workspace_id>/ephemeral-chat', methods=['POST'])
@login_required
def ephemeral_chat(workspace_id):
    """Ephemeral chat: LLM response with workspace context, no DB persistence.

    Accepts JSON: {"prompt": "...", "history": [...], "mode": "standard"}
    Returns SSE stream identical to normal chat streaming.
    """
    ws = Workspace.query.filter_by(id=workspace_id, user_id=current_user.id).first_or_404()

    allowed, used, limit = check_rate_limit()
    if not allowed:
        return Response(
            "data: " + json.dumps({
                "error": "rate_limited",
                "message": f"Free tier limit reached ({limit} messages per hour). Upgrade to Premium for unlimited.",
                "used": used,
                "limit": limit,
            }) + "\n\n",
            mimetype="text/event-stream",
        )

    data = request.get_json()
    prompt = (data.get("prompt") or "").strip() if data else ""
    history = data.get("history") or []
    mode = data.get("mode") or "standard"

    if not prompt:
        return Response(
            "data: " + json.dumps({"error": "Empty message"}) + "\n\n",
            mimetype="text/event-stream",
        )

    # Get workspace context (sibling thread summaries)
    ws_context = _get_workspace_context_by_id(workspace_id)

    # Build system prompt
    system_prompt = current_app.config.get("LLM_SYSTEM_PROMPT", "You are a helpful assistant.")
    if ws_context:
        system_prompt = system_prompt + "\n\n" + ws_context

    # Build messages array
    messages = [{"role": "system", "content": system_prompt}]

    # Add client-side history (capped)
    for h in history[-40:]:
        if isinstance(h, dict) and h.get("role") in ("user", "assistant") and h.get("content"):
            messages.append({"role": h["role"], "content": h["content"]})

    # Add current prompt
    messages.append({"role": "user", "content": prompt})

    # Create LLM client
    client = LLMClient(
        base_url=current_app.config["LLM_BASE_URL"],
        model=current_app.config["LLM_MODEL"],
        system_prompt=system_prompt,
        max_history=current_app.config.get("LLM_MAX_HISTORY", 20),
    )

    _app = current_app._get_current_object()
    _user_id = current_user.id
    _is_premium = current_user.is_premium

    def _stream():
        queue_id = enter_queue(_user_id, _is_premium, _app=_app)
        if queue_id is None:
            yield f"data: {json.dumps({'error': 'queue_timeout', 'message': 'Too many requests. Please try again.'})}\n\n"
            return
        try:
            full_response = ""
            prompt_tokens = 0
            completion_tokens = 0
            for chunk in client.stream_chat(messages, mode=mode):
                if isinstance(chunk, dict):
                    if "thinking_start" in chunk:
                        yield f"data: {json.dumps({'thinking_start': True})}\n\n"
                    elif "thinking_progress" in chunk:
                        yield f"data: {json.dumps({'thinking_progress': chunk['thinking_progress']})}\n\n"
                    elif "thinking_end" in chunk:
                        yield f"data: {json.dumps({'thinking_end': True, 'tokens': chunk.get('tokens', 0)})}\n\n"
                    else:
                        prompt_tokens = chunk.get("prompt_tokens", 0)
                        completion_tokens = chunk.get("completion_tokens", 0)
                else:
                    full_response += chunk
                    yield f"data: {json.dumps({'content': chunk}, ensure_ascii=False)}\n\n"

            yield f"data: {json.dumps({'done': True, 'full_response': full_response, 'ephemeral': True, 'prompt_tokens': prompt_tokens, 'completion_tokens': completion_tokens}, ensure_ascii=False)}\n\n"
        except Exception as e:
            logger.error("Ephemeral chat error: %s", e)
            yield f"data: {json.dumps({'error': str(e)})}\n\n"
        finally:
            leave_queue(queue_id, _app=_app)

    return Response(_stream(), mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})
