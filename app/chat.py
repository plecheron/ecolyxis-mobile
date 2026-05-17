from datetime import datetime, timezone, timedelta
import json
import re
import os
import base64
import uuid
import requests as req_lib
from flask import Blueprint, render_template, request, Response, current_app
from flask_login import login_required, current_user
from app import db
from app.models import Thread, Message, GeneratedImage
from app.llm import LLMClient
from app.queue import enter_queue, leave_queue

UPLOAD_FOLDER = "/opt/Ecolyxis/uploads"
MAX_IMAGE_SIZE = 20 * 1024 * 1024  # 20MB
ALLOWED_EXTENSIONS = {"png", "jpg", "jpeg", "gif", "webp"}


def _ensure_upload_dir():
    os.makedirs(UPLOAD_FOLDER, exist_ok=True)

chat_bp = Blueprint("chat", __name__)


def get_client():
    return LLMClient(
        base_url=current_app.config["LLM_BASE_URL"],
        model=current_app.config["LLM_MODEL"],
        system_prompt=current_app.config["LLM_SYSTEM_PROMPT"],
        max_history=current_app.config["LLM_MAX_HISTORY"],
    )


def check_rate_limit():
    """Return (allowed, messages_used, limit)."""
    if current_user.is_premium:
        return True, 0, None

    limit = current_app.config["RATE_LIMIT_MESSAGES"]
    window = current_app.config["RATE_LIMIT_WINDOW_SECONDS"]
    used = current_user.messages_in_window(window)
    return used < limit, used, limit


# ---------------------------------------------------------------------------
# Shared streaming helpers
# ---------------------------------------------------------------------------

def _run_precise(client, msgs, mode):
    """Multi-stage precise mode: Plan → Generate → Refine.

    All stages run internally (non-streaming from caller's perspective).
    Only the final refined output is streamed back as SSE chunks.
    """
    import time

    def _call(messages, retries=3):
        """Call the LLM and return (text, prompt_tokens, completion_tokens). Retries on error."""
        for attempt in range(retries):
            text = ""
            pt = 0
            ct = 0
            for chunk in client.stream_chat(messages, mode=mode):
                if isinstance(chunk, dict):
                    pt += chunk.get("prompt_tokens", 0)
                    ct += chunk.get("completion_tokens", 0)
                else:
                    text += chunk
            if not text.startswith("⚠️ Error"):
                return text, pt, ct
            time.sleep(5 * (attempt + 1))  # backoff
        return text, pt, ct  # return error text as last resort

    plan_prompt = (
        "You are a planning assistant. Given the conversation and the user's latest message, "
        "create a concise step-by-step plan for how to answer it. "
        "Consider what information to include, structure, tone, and any potential pitfalls. "
        "Output ONLY the plan, nothing else."
    )
    generate_prompt = (
        "You are following an explicit plan to answer the user. "
        "Here is the plan:\n\n{plan}\n\n"
        "Now execute this plan fully. Produce the complete, detailed response. "
        "Do not mention the plan — just produce the output."
    )
    refine_prompt = (
        "You are a quality reviewer. Here is a draft response to a user's message:\n\n---\n{draft}\n---\n\n"
        "Review and refine this response. Fix any errors, improve clarity and accuracy, "
        "remove redundancy, and ensure it directly answers the user's question. "
        "If the response is already excellent, return it unchanged. "
        "Output ONLY the final refined response."
    )

    total_prompt_tokens = 0
    total_completion_tokens = 0

    # Stage 1: Plan
    plan_messages = [{"role": "system", "content": plan_prompt}] + msgs[1:]
    if not any(m["role"] == "user" for m in plan_messages):
        plan_messages.append({"role": "user", "content": msgs[-1]["content"] if msgs else "Please help."})
    plan, pt, ct = _call(plan_messages)
    total_prompt_tokens += pt
    total_completion_tokens += ct

    # Stage 2: Generate from plan
    gen_messages = [{"role": "system", "content": generate_prompt.format(plan=plan)}] + msgs[1:]
    if not any(m["role"] == "user" for m in gen_messages):
        gen_messages.append({"role": "user", "content": msgs[-1]["content"] if msgs else "Please respond."})
    draft, pt, ct = _call(gen_messages)
    total_prompt_tokens += pt
    total_completion_tokens += ct

    # Stage 3: Refine
    refine_messages = [{"role": "system", "content": refine_prompt.format(draft=draft)}]
    refine_messages.append({"role": "user", "content": "Produce the final refined version now."})
    final, pt, ct = _call(refine_messages)
    total_prompt_tokens += pt
    total_completion_tokens += ct

    return final, total_prompt_tokens, total_completion_tokens


def _stream_llm(client, messages, mode, user_id, is_premium, app,
                precise=False, show_thinking=True, compacted=False):
    """Generator: enter queue → stream LLM response as SSE data lines → cleanup.

    Yields ``"data: {…}\\n\\n"`` strings suitable for a ``text/event-stream`` Response.
    """
    queue_id = enter_queue(user_id, is_premium, _app=app)
    if queue_id is None:
        yield f"data: {json.dumps({'error': 'queue_timeout', 'message': 'Too many requests. Please try again.'})}\n\n"
        return
    try:
        if precise:
            text, prompt_tokens, completion_tokens = _run_precise(client, messages, "standard")
            yield f"data: {json.dumps({'content': text}, ensure_ascii=False)}\n\n"
        else:
            text = ""
            prompt_tokens = 0
            completion_tokens = 0
            for chunk in client.stream_chat(messages, mode=mode):
                if isinstance(chunk, dict):
                    if show_thinking and "thinking_start" in chunk:
                        yield f"data: {json.dumps({'thinking_start': True})}\n\n"
                    elif show_thinking and "thinking_end" in chunk:
                        yield f"data: {json.dumps({'thinking_end': True})}\n\n"
                    elif "thinking_start" not in chunk and "thinking_end" not in chunk:
                        prompt_tokens = chunk.get("prompt_tokens", 0)
                        completion_tokens = chunk.get("completion_tokens", 0)
                else:
                    text += chunk
                    yield f"data: {json.dumps({'content': chunk}, ensure_ascii=False)}\n\n"

        done = {"done": True, "full_response": text,
                "prompt_tokens": prompt_tokens, "completion_tokens": completion_tokens}
        if compacted:
            done["compacted"] = True
        yield f"data: {json.dumps(done, ensure_ascii=False)}\n\n"
    except Exception as e:
        yield f"data: {json.dumps({'error': str(e)})}\n\n"
    finally:
        leave_queue(queue_id, _app=app)


def _sse(generator):
    """Wrap a generator in a streaming SSE Response."""
    return Response(generator, mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@chat_bp.route("/chat/<string:thread_id>")
@login_required
def view(thread_id):
    thread = Thread.query.filter_by(id=thread_id, user_id=current_user.id).first_or_404()
    threads = (
        Thread.query.filter_by(user_id=current_user.id)
        .order_by(Thread.updated_at.desc())
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
    """Compact the entire conversation by summarizing it through the LLM."""
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

    return _sse(_stream_llm(
        client, summary_messages, "long", current_user.id, current_user.is_premium,
        current_app._get_current_object(), show_thinking=False, compacted=True,
    ))


@chat_bp.route("/chat/<string:thread_id>/compact/progressive", methods=["POST"])
@login_required
def compact_progressive(thread_id):
    """Progressive compact: summarise the oldest 50% of messages, keep recent ones."""
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

    return _sse(_stream_llm(
        client, summary_messages, "standard", current_user.id, current_user.is_premium,
        current_app._get_current_object(), show_thinking=False, compacted=True,
    ))


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


@chat_bp.route("/chat/<string:thread_id>/generate-image", methods=["POST"])
@login_required
def generate_image_endpoint(thread_id):
    """Generate an image using HiDream and return it."""
    thread = Thread.query.filter_by(id=thread_id, user_id=current_user.id).first_or_404()

    allowed, used, limit = check_rate_limit()
    if not allowed:
        return {"error": "rate_limited", "message": f"Free tier limit reached ({limit} messages per hour)."}, 429

    data = request.get_json()
    prompt = data.get("prompt", "").strip()
    if not prompt:
        return {"error": "Empty prompt"}, 400

    width = data.get("width", 128)
    height = data.get("height", 128)

    hidream_url = current_app.config["HIDREAM_URL"]
    try:
        resp = req_lib.post(
            f"{hidream_url}/generate",
            json={"prompt": prompt, "width": width, "height": height},
            timeout=300,
        )
        if resp.status_code != 200:
            return {"error": f"Image generation failed: {resp.text[:200]}"}, 502
    except req_lib.RequestException as e:
        return {"error": f"Image service unavailable: {e}"}, 503

    # Save the image
    _ensure_upload_dir()
    filename = f"{uuid.uuid4().hex[:12]}.png"
    filepath = os.path.join(UPLOAD_FOLDER, filename)
    with open(filepath, "wb") as f:
        f.write(resp.content)

    # Save image metadata
    actual_seed = int(resp.headers.get("X-Seed", 42)) if hasattr(resp, "headers") else 42
    img_record = GeneratedImage(
        user_id=current_user.id,
        thread_id=thread_id,
        prompt=prompt,
        seed=actual_seed,
        width=width,
        height=height,
        filename=filename,
    )
    db.session.add(img_record)
    db.session.commit()

    return {"url": f"/uploads/{filename}", "filename": filename, "size": [width, height], "seed": actual_seed, "image_id": img_record.id}


@chat_bp.route("/chat/<string:thread_id>/generate-image-stream", methods=["POST"])
@login_required
def generate_image_stream(thread_id):
    """SSE proxy: stream image generation progress from Z-Image server."""
    from flask import Response
    import json as _json

    thread = Thread.query.filter_by(id=thread_id, user_id=current_user.id).first_or_404()

    allowed, used, limit = check_rate_limit()
    if not allowed:
        err = _json.dumps({"error": "rate_limited", "message": f"Free tier limit reached ({limit} messages per hour)."})
        def err_stream():
            yield "data: " + err + "\n\n"
        return Response(err_stream(), mimetype="text/event-stream")

    data = request.get_json()
    prompt = data.get("prompt", "").strip()
    if not prompt:
        err = _json.dumps({"error": "Empty prompt"})
        def err_stream2():
            yield "data: " + err + "\n\n"
        return Response(err_stream2(), mimetype="text/event-stream")

    width = data.get("width", 128)
    height = data.get("height", 128)

    hidream_url = current_app.config["HIDREAM_URL"]
    gen_url = f"{hidream_url}/generate-stream"

    _ensure_upload_dir()
    _gen_user_id = current_user.id
    _app = current_app._get_current_object()

    def stream_proxy():
        with _app.app_context():
            try:
                resp = req_lib.post(
                    gen_url,
                    json={"prompt": prompt, "width": width, "height": height},
                    timeout=300,
                    stream=True,
                )
                for line in resp.iter_lines(decode_unicode=True):
                    if not line or not line.startswith("data: "):
                        continue
                    payload = line[6:]
                    try:
                        event = _json.loads(payload)
                    except _json.JSONDecodeError:
                        continue

                    if event.get("stage") == "done" and event.get("filename"):
                        img_url = f"{hidream_url}/outputs/{event['filename']}"
                        try:
                            img_resp = req_lib.get(img_url, timeout=30)
                            if img_resp.status_code == 200:
                                local_name = f"{uuid.uuid4().hex[:12]}.png"
                                local_path = os.path.join(UPLOAD_FOLDER, local_name)
                                with open(local_path, "wb") as imgf:
                                    imgf.write(img_resp.content)
                                event["url"] = f"/uploads/{local_name}"
                                event["filename"] = local_name
                                actual_seed = event.get("seed", 42)
                                img_record = GeneratedImage(
                                    user_id=_gen_user_id,
                                    thread_id=thread_id,
                                    prompt=prompt,
                                    seed=actual_seed,
                                    width=width,
                                    height=height,
                                    filename=local_name,
                                )
                                db.session.add(img_record)
                                db.session.flush()
                                event["image_id"] = img_record.id
                                db.session.commit()
                                event["width"] = width
                                event["height"] = height
                        except Exception as e:
                            event["error"] = f"Failed to save image: {e}"

                    yield "data: " + _json.dumps(event) + "\n\n"

                    if event.get("stage") == "done" or event.get("error"):
                        break
            except req_lib.RequestException as e:
                err_payload = _json.dumps({"error": "Image service unavailable: " + str(e)})
                yield "data: " + err_payload + "\n\n"

    return Response(stream_proxy(), mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@chat_bp.route("/chat/<string:thread_id>/upscale-image", methods=["POST"])
@login_required
def upscale_image(thread_id):
    """Upscale a previously generated image to the next size using the same seed."""
    from flask import Response
    import json as _json

    thread = Thread.query.filter_by(id=thread_id, user_id=current_user.id).first_or_404()

    data = request.get_json()
    image_id = data.get("image_id")
    if not image_id:
        return {"error": "image_id required"}, 400

    img = GeneratedImage.query.filter_by(id=image_id, user_id=current_user.id).first_or_404()
    next_size = img.next_size()
    if next_size is None:
        return {"error": "Already at maximum size (512x512)"}, 400

    hidream_url = current_app.config["HIDREAM_URL"]
    gen_url = f"{hidream_url}/generate-stream"

    _ensure_upload_dir()
    _user_id = current_user.id
    _prompt = img.prompt
    _seed = img.seed
    _parent_id = img.id
    _app = current_app._get_current_object()

    def stream_upscale():
        with _app.app_context():
            try:
                resp = req_lib.post(
                    gen_url,
                    json={"prompt": _prompt, "width": next_size, "height": next_size, "seed": _seed},
                    timeout=300,
                    stream=True,
                )
                for line in resp.iter_lines(decode_unicode=True):
                    if not line or not line.startswith("data: "):
                        continue
                    payload = line[6:]
                    try:
                        event = _json.loads(payload)
                    except _json.JSONDecodeError:
                        continue

                    if event.get("stage") == "done" and event.get("filename"):
                        img_url = f"{hidream_url}/outputs/{event['filename']}"
                        try:
                            img_resp = req_lib.get(img_url, timeout=30)
                            if img_resp.status_code == 200:
                                local_name = f"{uuid.uuid4().hex[:12]}.png"
                                local_path = os.path.join(UPLOAD_FOLDER, local_name)
                                with open(local_path, "wb") as imgf:
                                    imgf.write(img_resp.content)
                                event["url"] = f"/uploads/{local_name}"
                                event["filename"] = local_name
                                new_img = GeneratedImage(
                                    user_id=_user_id,
                                    thread_id=thread_id,
                                    prompt=_prompt,
                                    seed=_seed,
                                    width=next_size,
                                    height=next_size,
                                    filename=local_name,
                                    parent_id=_parent_id,
                                )
                                db.session.add(new_img)
                                db.session.flush()
                                event["image_id"] = new_img.id
                                db.session.commit()
                        except Exception as e:
                            event["error"] = f"Failed to save image: {e}"

                    yield "data: " + _json.dumps(event) + "\n\n"

                    if event.get("stage") == "done" or event.get("error"):
                        break
            except req_lib.RequestException as e:
                err_payload = _json.dumps({"error": "Image service unavailable: " + str(e)})
                yield "data: " + err_payload + "\n\n"

    return Response(stream_upscale(), mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


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
