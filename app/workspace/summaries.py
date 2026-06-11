"""Thread summary generation for workspace threads.

Provides functions to generate and retrieve compact summaries of thread
conversations, stored in thread.summary for use in workspace context.
"""

import requests
import logging
from flask import current_app
from app import db
from app.models import Message

logger = logging.getLogger("ecolyxis.workspace.summaries")


def _build_conversation_text(thread, max_chars=3000):
    """Build a truncated conversation transcript from a thread's messages.

    Returns a string of "User: ...\\n\\nAssistant: ...\\n\\n" turns,
    truncated to approximately max_chars characters.
    """
    messages = (
        Message.query.filter_by(thread_id=thread.id)
        .order_by(Message.created_at)
        .all()
    )

    if not messages:
        return ""

    parts = []
    total_len = 0

    for m in messages:
        role_label = "User" if m.role == "user" else "Assistant"
        # Extract plain text from content (handle JSON content arrays)
        content = m.content or ""
        if content.strip().startswith("["):
            try:
                import json
                parts_list = json.loads(content)
                if isinstance(parts_list, list):
                    text_bits = []
                    for p in parts_list:
                        if isinstance(p, dict) and p.get("type") == "text":
                            text_bits.append(p.get("text", ""))
                        elif isinstance(p, dict) and p.get("type") == "image":
                            text_bits.append("[image]")
                    content = " ".join(text_bits) if text_bits else content
            except (json.JSONDecodeError, KeyError, TypeError):
                pass

        line = "{}: {}\n\n".format(role_label, content)

        if total_len + len(line) > max_chars:
            # Truncate this message to fit
            remaining = max_chars - total_len
            if remaining > 50:
                line = "{}: {}...\n\n".format(role_label, content[:remaining - 10])
                parts.append(line)
            break

        parts.append(line)
        total_len += len(line)

    return "".join(parts)


def _call_llm_non_streaming(messages, timeout=60):
    """Make a non-streaming call to the GPU proxy LLM.

    Uses the same config as LLMClient but with stream=False for a simple
    request/response cycle.
    """
    base_url = current_app.config["LLM_BASE_URL"]
    model = current_app.config["LLM_MODEL"]

    url = "{}/chat/completions".format(base_url)
    payload = {
        "model": model,
        "messages": messages,
        "stream": False,
        "max_tokens": 1024,
        "temperature": 0.5,
    }

    try:
        resp = requests.post(url, json=payload, timeout=timeout)
        if resp.status_code >= 400:
            logger.error("LLM summary call failed: HTTP %d - %s",
                         resp.status_code, resp.text[:300])
            return None

        data = resp.json()
        choices = data.get("choices", [])
        if not choices:
            logger.error("LLM summary call returned no choices")
            return None

        content = choices[0].get("message", {}).get("content", "")
        return content.strip() if content else None

    except (requests.RequestException, requests.ConnectionError) as e:
        logger.error("LLM summary call error: %s", e)
        return None
    except (ValueError, KeyError) as e:
        logger.error("LLM summary response parse error: %s", e)
        return None


def generate_thread_summary(thread):
    """Generate a compact summary for a thread and store it in thread.summary.

    Takes a Thread object, builds a conversation transcript, calls the LLM
    with a summarization prompt, stores the result in thread.summary, and
    commits to DB.

    Returns the summary text on success, or None on failure.
    """
    conversation_text = _build_conversation_text(thread, max_chars=3000)

    if not conversation_text.strip():
        logger.info("Thread %s has no messages to summarize", thread.id)
        return thread.summary  # Return existing summary or None

    summary_messages = [
        {
            "role": "system",
            "content": (
                "You are a concise summarizer. Summarize the following conversation "
                "in 2-4 sentences. Focus on the key topics discussed and conclusions "
                "reached. Do not include filler or meta-commentary."
            ),
        },
        {
            "role": "user",
            "content": "Summarize this conversation:\n\n{}".format(conversation_text),
        },
    ]

    summary_text = _call_llm_non_streaming(summary_messages)

    if summary_text:
        thread.summary = summary_text
        db.session.commit()
        logger.info("Generated summary for thread %s (%d chars)",
                     thread.id, len(summary_text))
        return summary_text
    else:
        logger.warning("Failed to generate summary for thread %s", thread.id)
        return None


def get_or_generate_summary(thread):
    """Return existing thread summary, or generate one if missing.

    Returns the summary text, or None if generation fails and no prior
    summary exists.
    """
    if thread.summary:
        return thread.summary
    return generate_thread_summary(thread)
