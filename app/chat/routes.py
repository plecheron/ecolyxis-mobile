"""Text chat routes: viewing, messaging, editing, regenerate, compaction,
system prompt, file uploads, search, served uploads, and rate-limit status.
"""
import base64
import json
import os
import re
import uuid
from datetime import datetime, timezone, timedelta
from flask import render_template, request, Response, current_app, send_from_directory, abort
from flask_login import login_required, current_user

from app import db
from app.models import Thread, Message
from app.chat import (
    chat_bp,
    get_client,
    check_rate_limit,
    _stream_llm,
    _sse,
    _ensure_upload_dir,
    UPLOAD_FOLDER,
    ALLOWED_EXTENSIONS,
    MAX_IMAGE_SIZE,
)


@chat_bp.route("/chat/<string:thread_id>")
@login_required
def view(thread_id):
    thread = Thread.query.filter_by(id=thread_id, user_id=current_user.id).first_or_404()
    threads = (
        Thread.query.filter_by(user_id=current_user.id)
        .order_by(Thread.updated_at.desc())
        .filter(Thread.messages.any())  # Hide empty threads
        .all()
    )
    messages = (
        Message.query.filter_by(thread_id=thread.id)
        .order_by(Message.created_at)
        .all()
    )

    _, used, limit = check_rate_limit()
    rate_info = {"used": used, "limit": limit, "is_premium": current_user.is_premium}

    return render_template(
        "chat.html",
        thread=thread,
        threads=threads,
        messages=messages,
        rate_info=rate_info,
        thread_system_prompt=thread.system_prompt,
    )


@chat_bp.route("/chat/<string:thread_id>/message", methods=["POST"])
@login_required
def send_message(thread_id):
    thread = Thread.query.filter_by(id=thread_id, user_id=current_user.id).first_or_404()

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
    content = data.get("content", "").strip() if data else ""
    images = data.get("images", []) if data else []
    mode = data.get("mode", "standard") if data else "standard"

    if not content and not images:
        return Response("data: " + json.dumps({"error": "Empty message"}) + "\n\n",
                        mimetype="text/event-stream")

    msg_type = "text"
    if images:
        _ensure_upload_dir()
        msg_parts = []
        if content:
            msg_parts.append({"type": "text", "text": content})
        for img_data_url in images:
            # Save image to disk, store file reference
            match = re.match(r'^data:(image/[\w]+);base64,(.+)$', img_data_url, re.DOTALL)
            if match:
                mime = match.group(1)
                b64_data = match.group(2)
                img_bytes = base64.b64decode(b64_data)
                ext = mime.split('/')[1].replace('jpeg', 'jpg')
                filename = f"{uuid.uuid4().hex[:12]}.{ext}"
                filepath = os.path.join(UPLOAD_FOLDER, filename)
                with open(filepath, 'wb') as f:
                    f.write(img_bytes)
                msg_parts.append({"type": "image", "file": filename, "name": filename})
            else:
                # Legacy fallback: already a URL or unknown format
                msg_parts.append({"type": "image", "url": img_data_url})
        storage_content = json.dumps(msg_parts)
        has_text = any(p.get("type") == "text" and p.get("text", "").strip() for p in msg_parts)
        msg_type = "mixed" if has_text else "image"
    else:
        storage_content = content

    user_msg = Message(thread_id=thread.id, role="user", content=storage_content, message_type=msg_type)
    db.session.add(user_msg)
    db.session.commit()

    thread.update_title()
    db.session.commit()

    client = get_client()
    msgs = client.build_messages(thread, mode=mode)

    return _sse(_stream_llm(
        client, msgs, mode, current_user.id, current_user.is_premium,
        current_app._get_current_object(), precise=(mode == "precise"),

    ))


@chat_bp.route("/chat/<string:thread_id>/save", methods=["POST"])
@login_required
def save_message(thread_id):
    """Called by client after streaming completes to persist the assistant response."""
    thread = Thread.query.filter_by(id=thread_id, user_id=current_user.id).first_or_404()
    data = request.get_json()
    content = data.get("content", "").strip() if data else ""
    tokens = data.get("tokens_used")

    if content:
        msg_type = data.get("message_type", "text")
        msg = Message(thread_id=thread.id, role="assistant", content=content, tokens_used=tokens, message_type=msg_type)
        db.session.add(msg)
        db.session.commit()

    return {"status": "ok"}


@chat_bp.route("/chat/<string:thread_id>/edit/<int:message_id>", methods=["POST"])
@login_required
def edit_message(thread_id, message_id):
    """Edit a user message, delete all subsequent messages, and re-generate response."""
    thread = Thread.query.filter_by(id=thread_id, user_id=current_user.id).first_or_404()

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

    msg = Message.query.filter_by(id=message_id, thread_id=thread.id, role="user").first_or_404()

    data = request.get_json()
    new_content = data.get("content", "").strip() if data else ""
    mode = data.get("mode", "standard") if data else "standard"

    if not new_content:
        return Response("data: " + json.dumps({"error": "Empty message"}) + "\n\n",
                        mimetype="text/event-stream")

    msg.content = new_content
    Message.query.filter(
        Message.thread_id == thread.id,
        Message.created_at > msg.created_at
    ).delete(synchronize_session="fetch")
    db.session.commit()

    thread.update_title()
    db.session.commit()

    client = get_client()
    msgs = client.build_messages(thread, mode=mode)

    return _sse(_stream_llm(
        client, msgs, mode, current_user.id, current_user.is_premium,
        current_app._get_current_object()
    ))


@chat_bp.route("/chat/<string:thread_id>/regenerate", methods=["POST"])
@login_required
def regenerate(thread_id):
    """Delete the last assistant message and regenerate it."""
    thread = Thread.query.filter_by(id=thread_id, user_id=current_user.id).first_or_404()
    data = request.get_json(silent=True) or {}
    mode = data.get("mode", "standard")

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

    last_assistant = (
        Message.query.filter_by(thread_id=thread.id, role="assistant")
        .order_by(Message.created_at.desc())
        .first()
    )
    if last_assistant:
        db.session.delete(last_assistant)
        db.session.commit()

    client = get_client()
    msgs = client.build_messages(thread, mode=mode)

    return _sse(_stream_llm(
        client, msgs, mode, current_user.id, current_user.is_premium,
        current_app._get_current_object()
    ))


@chat_bp.route("/chat/<string:thread_id>/compact", methods=["POST"])
@login_required
def compact_thread(thread_id):
    """Compact the entire conversation by summarizing it through the LLM.
    Server-side: generates summary and persists it automatically."""
    thread = Thread.query.filter_by(id=thread_id, user_id=current_user.id).first_or_404()

    all_messages = (
        Message.query.filter_by(thread_id=thread.id)
        .order_by(Message.created_at)
        .all()
    )

    if len(all_messages) < 2:
        return Response(
            "data: " + json.dumps({"error": "Nothing to compact — need at least 2 messages."}) + "\n\n",
            mimetype="text/event-stream",
        )

    allowed, used, limit = check_rate_limit()
    if not allowed:
        return Response(
            "data: " + json.dumps({
                "error": "rate_limited",
                "message": f"Free tier limit reached ({limit} messages per hour). Upgrade to Premium for unlimited.",
            }) + "\n\n",
            mimetype="text/event-stream",
        )

    conversation_text = ""
    for m in all_messages:
        role_label = "User" if m.role == "user" else "Assistant"
        conversation_text += f"{role_label}: {m.content}\n\n"

    msg_count = len(all_messages)

    summary_prompt = (
        "You are summarising a conversation. Produce a detailed but concise summary "
        "that captures all key topics discussed, decisions made, conclusions reached, "
        "and any important details or context that would be needed to continue the conversation. "
        "Do NOT include filler. Be thorough but efficient."
    )

    client = get_client()
    summary_messages = [
        {"role": "system", "content": summary_prompt},
        {"role": "user", "content": f"Summarise this conversation:\n\n{conversation_text}"},
    ]

    _app = current_app._get_current_object()
    _thread_id = thread.id
    _user_id = current_user.id

    def _stream_and_save():
        summary_text = ""
        prompt_tokens = 0
        completion_tokens = 0
        queue_id = enter_queue(_user_id, current_user.is_premium, _app=_app)
        if queue_id is None:
            yield f"data: {json.dumps({'error': 'queue_timeout', 'message': 'Too many requests. Please try again.'})}\n\n"
            return
        try:
            for chunk in client.stream_chat(summary_messages, mode="long"):
                if isinstance(chunk, dict):
                    if "thinking_start" in chunk:
                        yield f"data: {json.dumps({'thinking_start': True})}\n\n"
                    elif "thinking_end" in chunk:
                        yield f"data: {json.dumps({'thinking_end': True})}\n\n"
                    else:
                        prompt_tokens = chunk.get("prompt_tokens", 0)
                        completion_tokens = chunk.get("completion_tokens", 0)
                else:
                    summary_text += chunk
                    yield f"data: {json.dumps({'content': chunk, 'compacted': True}, ensure_ascii=False)}\n\n"

            # Server-side save — no client confirmation needed
            with _app.app_context():
                Message.query.filter_by(thread_id=_thread_id).delete()
                user_summary = Message(
                    thread_id=_thread_id,
                    role="user",
                    content=f"\U0001f4dd **Conversation compacted** ({msg_count} messages \u2192 summary)\n\nHere is the summary of our previous conversation:",
                )
                db.session.add(user_summary)
                assistant_summary = Message(
                    thread_id=_thread_id,
                    role="assistant",
                    content=summary_text,
                    tokens_used=completion_tokens,
                )
                db.session.add(assistant_summary)
                db.session.commit()

            yield f"data: {json.dumps({'done': True, 'full_response': summary_text, 'compacted': True, 'compact_saved': True, 'msg_count': msg_count, 'prompt_tokens': prompt_tokens, 'completion_tokens': completion_tokens}, ensure_ascii=False)}\n\n"
        except Exception as e:
            yield f"data: {json.dumps({'error': str(e)})}\n\n"
        finally:
            leave_queue(queue_id, _app=_app)

    return Response(_stream_and_save(), mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@chat_bp.route("/chat/<string:thread_id>/compact/progressive", methods=["POST"])
@login_required
def compact_progressive(thread_id):
    """Progressive compact: summarise the oldest 50% of messages, keep recent ones.
    Server-side: generates summary and persists it automatically."""
    thread = Thread.query.filter_by(id=thread_id, user_id=current_user.id).first_or_404()

    all_messages = (
        Message.query.filter_by(thread_id=thread.id)
        .order_by(Message.created_at)
        .all()
    )

    if len(all_messages) < 4:
        return Response(
            "data: " + json.dumps({"error": "Need at least 4 messages for progressive compact. Try full compact instead."}) + "\n\n",
            mimetype="text/event-stream",
        )

    allowed, used, limit = check_rate_limit()
    if not allowed:
        return Response(
            "data: " + json.dumps({"error": "rate_limited", "message": f"Free tier limit reached ({limit} messages per hour)."}) + "\n\n",
            mimetype="text/event-stream",
        )

    half_point = len(all_messages) // 2
    old_messages = all_messages[:half_point]

    conversation_text = ""
    for m in old_messages:
        role_label = "User" if m.role == "user" else "Assistant"
        conversation_text += f"{role_label}: {m.content}\n\n"

    msg_count = len(old_messages)

    summary_prompt = (
        "You are summarising the first half of a conversation. Produce a detailed but concise summary "
        "that captures all key topics discussed, decisions made, conclusions reached, "
        "and any important details or context that would be needed to continue the conversation. "
        "The second half of the conversation will remain intact — only summarise what you are given. "
        "Do NOT include filler. Be thorough but efficient."
    )

    client = get_client()
    summary_messages = [
        {"role": "system", "content": summary_prompt},
        {"role": "user", "content": f"Summarise this conversation:\n\n{conversation_text}"},
    ]

    _app = current_app._get_current_object()
    _thread_id = thread.id
    _user_id = current_user.id
    _oldest_time = all_messages[0].created_at if all_messages else datetime.now(timezone.utc)

    def _stream_and_save():
        summary_text = ""
        prompt_tokens = 0
        completion_tokens = 0
        queue_id = enter_queue(_user_id, current_user.is_premium, _app=_app)
        if queue_id is None:
            yield f"data: {json.dumps({'error': 'queue_timeout', 'message': 'Too many requests. Please try again.'})}\n\n"
            return
        try:
            for chunk in client.stream_chat(summary_messages, mode="standard"):
                if isinstance(chunk, dict):
                    if "thinking_start" in chunk:
                        yield f"data: {json.dumps({'thinking_start': True})}\n\n"
                    elif "thinking_end" in chunk:
                        yield f"data: {json.dumps({'thinking_end': True})}\n\n"
                    else:
                        prompt_tokens = chunk.get("prompt_tokens", 0)
                        completion_tokens = chunk.get("completion_tokens", 0)
                else:
                    summary_text += chunk
                    yield f"data: {json.dumps({'content': chunk, 'compacted': True}, ensure_ascii=False)}\n\n"

            # Server-side save — no client confirmation needed
            with _app.app_context():
                for m in all_messages[:half_point]:
                    db.session.delete(m)
                db.session.flush()
                user_summary = Message(
                    thread_id=_thread_id,
                    role="user",
                    content=f"\U0001f4dd **Earlier conversation compacted** ({msg_count} messages \u2192 summary)\n\nHere is the summary:",
                    created_at=_oldest_time,
                )
                db.session.add(user_summary)
                assistant_summary = Message(
                    thread_id=_thread_id,
                    role="assistant",
                    content=summary_text,
                    tokens_used=completion_tokens,
                    created_at=_oldest_time + timedelta(seconds=1),
                )
                db.session.add(assistant_summary)
                db.session.commit()

            yield f"data: {json.dumps({'done': True, 'full_response': summary_text, 'compacted': True, 'compact_saved': True, 'compact_type': 'progressive', 'msg_count': msg_count, 'prompt_tokens': prompt_tokens, 'completion_tokens': completion_tokens}, ensure_ascii=False)}\n\n"
        except Exception as e:
            yield f"data: {json.dumps({'error': str(e)})}\n\n"
        finally:
            leave_queue(queue_id, _app=_app)

    return Response(_stream_and_save(), mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@chat_bp.route("/chat/<string:thread_id>/clear", methods=["POST"])
@login_required
def clear_thread(thread_id):
    """Delete all messages in a thread."""
    thread = Thread.query.filter_by(id=thread_id, user_id=current_user.id).first_or_404()
    Message.query.filter_by(thread_id=thread.id).delete()
    db.session.commit()
    return {"status": "ok"}


@chat_bp.route("/chat/<string:thread_id>/compact/save", methods=["POST"])
@login_required
def compact_save(thread_id):
    """Persist the compacted conversation after streaming completes."""
    thread = Thread.query.filter_by(id=thread_id, user_id=current_user.id).first_or_404()
    data = request.get_json()
    content = data.get("content", "").strip()
    msg_count = data.get("msg_count", 0)
    tokens = data.get("tokens_used")
    compact_type = data.get("compact_type", "full")

    if not content:
        return {"error": "Empty summary"}, 400

    if compact_type == "progressive":
        all_msgs = (
            Message.query.filter_by(thread_id=thread.id)
            .order_by(Message.created_at)
            .all()
        )
        half = len(all_msgs) // 2
        oldest_time = all_msgs[0].created_at if all_msgs else datetime.now(timezone.utc)
        for m in all_msgs[:half]:
            db.session.delete(m)
        db.session.flush()
        user_summary = Message(
    
            role="user",
            content=f"\U0001f4dd **Earlier conversation compacted** ({msg_count} messages \u2192 summary)\n\nHere is the summary:",
            created_at=oldest_time,
        )
        db.session.add(user_summary)
        assistant_summary = Message(
    
            role="assistant",
            content=content,
            tokens_used=tokens,
            created_at=oldest_time + timedelta(seconds=1),
        )
        db.session.add(assistant_summary)
    else:
        Message.query.filter_by(thread_id=thread.id).delete()
        user_summary = Message(
    
            role="user",
            content=f"\U0001f4dd **Conversation compacted** ({msg_count} messages \u2192 summary)\n\nHere is the summary of our previous conversation:",
        )
        db.session.add(user_summary)
        assistant_summary = Message(
    
            role="assistant",
            content=content,
            tokens_used=tokens,
        )
        db.session.add(assistant_summary)

    db.session.commit()
    return {"status": "ok"}


@chat_bp.route("/chat/<string:thread_id>/generate-title", methods=["POST"])
@login_required
def generate_title(thread_id):
    """Auto-generate a thread title from the first user message using the LLM.
    Called async from frontend after the first message exchange in a new thread."""
    thread = Thread.query.filter_by(id=thread_id, user_id=current_user.id).first_or_404()

    # Only generate if still has default title
    if thread.title != "New Chat":
        return {"title": thread.title}

    first_msg = (
        Message.query.filter_by(thread_id=thread.id, role="user")
        .order_by(Message.created_at)
        .first()
    )
    if not first_msg:
        return {"title": thread.title}

    # Extract plain text from message content (strip JSON image parts)
    raw = first_msg.content
    try:
        parts = json.loads(raw)
        if isinstance(parts, list):
            text = " ".join(p.get("text", "") for p in parts if p.get("type") == "text")
        else:
            text = raw
    except (json.JSONDecodeError, TypeError):
        text = raw

    if not text.strip():
        return {"title": thread.title}

    try:
        client = get_client()
        title_messages = [
            {"role": "system", "content": "Generate a very short title (3-6 words) for a conversation that starts with this message. Output ONLY the title, nothing else. No quotes."},
            {"role": "user", "content": text[:500]},
        ]
        title_text = ""
        for chunk in client.stream_chat(title_messages, mode="quick"):
            if isinstance(chunk, str):
                title_text += chunk

        title_text = title_text.strip().strip('"').strip("'")
        if title_text and len(title_text) <= 100:
            thread.title = title_text
        else:
            # Fallback: truncate first message
            thread.title = text[:50] + ("..." if len(text) > 50 else "")
        db.session.commit()
    except Exception:
        # Fallback on error
        thread.title = text[:50] + ("..." if len(text) > 50 else "")
        db.session.commit()

    return {"title": thread.title}


@chat_bp.route("/chat/<string:thread_id>/rename", methods=["PATCH"])
@login_required
def rename_thread(thread_id):
    """Rename a thread. Users can override auto-generated titles."""
    thread = Thread.query.filter_by(id=thread_id, user_id=current_user.id).first_or_404()
    data = request.get_json()
    new_title = (data.get("title") or "").strip()[:200]

    if not new_title:
        return {"error": "Title cannot be empty"}, 400

    thread.title = new_title
    db.session.commit()
    return {"status": "ok", "title": thread.title}


@chat_bp.route("/chat/<string:thread_id>/system-prompt", methods=["POST"])
@login_required
def update_system_prompt(thread_id):
    """Update custom system prompt for a thread. Premium only."""
    thread = Thread.query.filter_by(id=thread_id, user_id=current_user.id).first_or_404()

    if not current_user.is_premium:
        return {"error": "premium_required", "message": "Custom system prompts are a Premium feature."}, 403

    data = request.get_json()
    prompt = (data.get("system_prompt") or "").strip()[:2000]
    thread.system_prompt = prompt if prompt else None
    db.session.commit()

    return {"status": "ok", "system_prompt": thread.system_prompt}


@chat_bp.route("/chat/<string:thread_id>/upload", methods=["POST"])
@login_required
def upload_image(thread_id):
    """Upload an image for a chat thread. Returns a URL reference."""
    thread = Thread.query.filter_by(id=thread_id, user_id=current_user.id).first_or_404()

    if 'file' not in request.files:
        return {"error": "No file provided"}, 400

    file = request.files['file']
    if not file.filename:
        return {"error": "No file selected"}, 400

    ext = file.filename.rsplit('.', 1)[-1].lower() if '.' in file.filename else ''
    if ext not in ALLOWED_EXTENSIONS:
        return {"error": f"Unsupported format. Allowed: {', '.join(ALLOWED_EXTENSIONS)}"}, 400

    file_data = file.read()
    if len(file_data) > MAX_IMAGE_SIZE:
        return {"error": f"Image too large. Max {MAX_IMAGE_SIZE // 1024 // 1024}MB"}, 400

    _ensure_upload_dir()
    filename = f"{uuid.uuid4().hex[:12]}.{ext}"
    filepath = os.path.join(UPLOAD_FOLDER, filename)
    with open(filepath, 'wb') as f:
        f.write(file_data)

    return {"url": f"/uploads/{filename}", "filename": filename}


@chat_bp.route("/api/search")
@login_required
def search():
    """Search messages across all threads. Premium only. Returns JSON."""
    if not current_user.is_premium:
        return {"error": "premium_required", "message": "Search is a Premium feature. Upgrade to unlock full-text search."}, 403

    q = request.args.get("q", "").strip()
    if not q or len(q) < 2:
        return {"results": []}

    results = (
        Message.query
        .join(Thread, Message.thread_id == Thread.id)
        .filter(Thread.user_id == current_user.id, Message.content.ilike(f"%{q}%"))
        .order_by(Message.created_at.desc())
        .limit(20)
        .all()
    )

    return {
        "results": [
            {
                "thread_id": m.thread_id,
                "message_id": m.id,
                "role": m.role,
                "content": m.content[:150] + ("..." if len(m.content) > 150 else ""),
                "created_at": m.created_at.isoformat(),
            }
            for m in results
        ]
    }






@chat_bp.route("/uploads/<filename>")
@login_required
def serve_upload(filename):
    """Serve uploaded images (login required)."""
    from flask import send_from_directory, abort
    if not re.match(r"^[\w.-]+$", filename):
        abort(404)
    return send_from_directory(UPLOAD_FOLDER, filename)


@chat_bp.route("/api/rate-limit")
@login_required
def rate_limit_status():
    """Return current rate limit status for the user."""
    allowed, used, limit = check_rate_limit()
    return {
        "allowed": allowed,
        "used": used,
        "limit": limit,
        "is_premium": current_user.is_premium,
    }
