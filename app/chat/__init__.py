from datetime import datetime, timezone, timedelta
import json
import re
import os
import base64
import uuid
import requests as req_lib
from flask import Blueprint, render_template, request, current_app
from flask_login import login_required, current_user
from app import db
from app.models import Thread, Message, GeneratedImage, GeneratedVideo
from app.llm import LLMClient

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


def save_user_message(thread, content, images):
    """Persist a user message (text and/or uploaded images), refresh the thread
    title, and return the created Message.

    Shared by the legacy in-request SSE path (``send_message``) and the async
    job-submit path (``app.jobs.routes``) so image handling stays identical.
    """
    msg_type = "text"
    if images:
        _ensure_upload_dir()
        msg_parts = []
        if content:
            msg_parts.append({"type": "text", "text": content})
        for img_data_url in images:
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
    return user_msg


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

def _save_remote_image(remote_url):
    """Fetch an image from the remote server and save it locally.

    Returns (local_filename, local_path) or raises.
    """
    import uuid
    import requests as req_lib
    _ensure_upload_dir()
    img_resp = req_lib.get(remote_url, timeout=60)
    if img_resp.status_code != 200:
        raise RuntimeError(f"Failed to fetch image: HTTP {img_resp.status_code}")
    local_name = f"{uuid.uuid4().hex[:12]}.png"
    local_path = os.path.join(UPLOAD_FOLDER, local_name)
    with open(local_path, "wb") as fout:
        fout.write(img_resp.content)
    return local_name, local_path

from app.chat import routes  # noqa: E402,F401 - register text-chat routes
# Video routes disabled — Wan2.2 backend is non-functional (#118)
# from app.chat import video   # noqa: E402,F401 - register video routes
from app.chat import export   # noqa: E402,F401 - register export routes
from app.chat import tts      # noqa: E402,F401 - register TTS routes
