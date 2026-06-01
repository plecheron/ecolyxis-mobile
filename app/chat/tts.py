"""Text-to-Speech endpoint for chat messages.

POST /chat/<thread_id>/tts
    Body: {"message_id": 123} or {"text": "speak this"}
    Returns: WAV audio (audio/wav)

The endpoint extracts text from a message (or accepts raw text), calls the
Qwen3-TTS backend on gpu1 via gpu-manager proxy, and streams back the WAV.

GPU model switching is handled automatically by gpu-manager: if the TTS
backend isn't loaded, the first request triggers a cold start (~3 min on P40).
"""

import io
import json
import logging
import re

from flask import request, Response, current_app
from flask_login import login_required, current_user
import requests as req_lib

from app import db
from app.models import Message, Thread
from app.chat import chat_bp

log = logging.getLogger(__name__)

# TTS takes ~20-30s per 60 chars on P40. Cap at 500 chars (~120s generation).
# gpu-manager proxy read_timeout is 300s to handle cold starts.
TTS_TIMEOUT = 300
MAX_TTS_CHARS = 500


def _get_tts_url():
    """Return the configured TTS backend URL."""
    url = current_app.config.get("TTS_URL")
    if not url:
        return None
    return url.rstrip("/")


def _extract_text_from_message(message):
    """Extract plain text from a Message content field.

    Content may be:
      - A plain string
      - A JSON list of content blocks: [{"type": "text", "text": "..."}, ...]
    """
    content = message.content
    if not content:
        return ""

    # Try JSON list format first
    try:
        blocks = json.loads(content)
        if isinstance(blocks, list):
            parts = []
            for block in blocks:
                if isinstance(block, dict) and block.get("type") == "text":
                    parts.append(block.get("text", ""))
            return " ".join(parts).strip()
    except (json.JSONDecodeError, TypeError):
        pass

    # Plain string
    return content.strip()


def _strip_markdown(text):
    """Remove markdown formatting from text for cleaner TTS."""
    # Remove image tags
    text = re.sub(r'!?\[.*?\]\(.*?\)', '', text)
    # Remove code blocks
    text = re.sub(r'```[\s\S]*?```', '', text)
    # Remove inline code
    text = re.sub(r'`[^`]+`', '', text)
    # Remove bold/italic
    text = re.sub(r'\*{1,3}([^*]+)\*{1,3}', r'\1', text)
    # Remove links but keep text
    text = re.sub(r'\[([^\]]+)\]\([^)]+\)', r'\1', text)
    # Remove headings
    text = re.sub(r'^#{1,6}\s+', '', text, flags=re.MULTILINE)
    # Collapse whitespace
    text = re.sub(r'\s+', ' ', text).strip()
    return text


@chat_bp.route("/chat/<thread_id>/tts", methods=["POST"])
@login_required
def tts(thread_id):
    """Generate speech for a chat message or arbitrary text."""

    # Verify thread ownership
    thread = Thread.query.filter_by(id=thread_id, user_id=current_user.id).first()
    if not thread:
        return Response(json.dumps({"error": "Thread not found"}), status=404, mimetype="application/json")

    body = request.get_json(silent=True) or {}
    message_id = body.get("message_id")
    text = body.get("text", "").strip()

    # If message_id given, extract text from that message
    if message_id:
        msg = Message.query.filter_by(id=message_id, thread_id=thread_id).first()
        if not msg:
            return Response(json.dumps({"error": "Message not found"}), status=404, mimetype="application/json")
        if not text:
            text = _extract_text_from_message(msg)

    if not text:
        return Response(json.dumps({"error": "No text to speak"}), status=400, mimetype="application/json")

    # Strip markdown for cleaner speech
    text = _strip_markdown(text)

    if not text:
        return Response(json.dumps({"error": "No text to speak after stripping formatting"}), status=400, mimetype="application/json")

    # Truncate to keep generation under ~120s on P40
    if len(text) > MAX_TTS_CHARS:
        text = text[:MAX_TTS_CHARS].rsplit(' ', 1)[0] + '...'

    tts_url = _get_tts_url()
    if not tts_url:
        return Response(
            json.dumps({"error": "TTS service not configured"}),
            status=503,
            mimetype="application/json",
        )

    try:
        resp = req_lib.post(
            f"{tts_url}/tts",
            json={
                "text": text,
                "language": "Auto",
                "speaker": "Vivian",
            },
            timeout=TTS_TIMEOUT,
        )

        if resp.status_code != 200:
            log.error("TTS backend error: HTTP %d %s", resp.status_code, resp.text[:200])
            return Response(
                json.dumps({"error": f"TTS generation failed: HTTP {resp.status_code}"}),
                status=502,
                mimetype="application/json",
            )

        # Stream the WAV audio back to the client
        return Response(
            resp.content,
            status=200,
            mimetype="audio/wav",
            headers={
                "Content-Disposition": "inline; filename=tts_output.wav",
                "X-Audio-Duration": resp.headers.get("X-Audio-Duration", ""),
            },
        )

    except req_lib.exceptions.Timeout:
        return Response(
            json.dumps({"error": "TTS generation timed out — model may be loading, try again in a few minutes"}),
            status=504,
            mimetype="application/json",
        )
    except req_lib.exceptions.ConnectionError:
        return Response(
            json.dumps({"error": "TTS service unavailable — model may be loading, try again in a few minutes"}),
            status=503,
            mimetype="application/json",
        )
