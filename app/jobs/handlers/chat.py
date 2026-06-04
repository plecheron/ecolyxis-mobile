"""Chat generation handler.

Reuses the existing LLM client (``app.chat.get_client`` / ``LLMClient``) and
the precise-mode pipeline, but instead of streaming straight to the client it
publishes events into the job's Redis Stream and persists the assistant
message keyed by ``job_id`` (UNIQUE) so a retry after a worker crash never
creates a duplicate.

Must be called inside an app context (the worker provides one).
"""
from sqlalchemy.exc import IntegrityError

from app import db
from app.models import Thread, Message


def _persist_assistant(job, text, tokens):
    """Insert the assistant message exactly once for this job. Returns its id."""
    existing = Message.query.filter_by(job_id=job.id).first()
    if existing:
        return existing.id
    msg = Message(
        thread_id=job.thread_id,
        role="assistant",
        content=text,
        tokens_used=tokens,
        job_id=job.id,
    )
    db.session.add(msg)
    try:
        db.session.commit()
    except IntegrityError:
        # Concurrent/retry insert lost the race on the UNIQUE job_id — reuse it.
        db.session.rollback()
        existing = Message.query.filter_by(job_id=job.id).first()
        return existing.id if existing else None
    return msg.id


def run_chat(app, job, publish):
    """Run a chat job. ``publish(event)`` appends to the job's event log."""
    from app.chat import get_client, _run_precise

    thread = db.session.get(Thread, job.thread_id)
    if thread is None:
        raise RuntimeError("thread no longer exists")

    params = job.params or {}
    mode = params.get("mode", "standard")
    precise = params.get("precise", mode == "precise")
    show_thinking = params.get("show_thinking", True)

    client = get_client()
    msgs = client.build_messages(thread, mode=mode)

    publish({"type": "stream_start"})

    text = ""
    prompt_tokens = 0
    completion_tokens = 0

    if precise:
        text, prompt_tokens, completion_tokens = _run_precise(client, msgs, "standard")
        if text:
            publish({"type": "content", "text": text})
    else:
        for chunk in client.stream_chat(msgs, mode=mode):
            if isinstance(chunk, dict):
                if "thinking_start" in chunk:
                    if show_thinking:
                        publish({"type": "thinking_start"})
                elif "thinking_end" in chunk:
                    if show_thinking:
                        publish({"type": "thinking_end"})
                else:
                    prompt_tokens = chunk.get("prompt_tokens", prompt_tokens)
                    completion_tokens = chunk.get("completion_tokens", completion_tokens)
            else:
                text += chunk
                publish({"type": "content", "text": chunk})

    message_id = _persist_assistant(job, text, completion_tokens)

    return {
        "message_id": message_id,
        "tokens": completion_tokens,
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
    }
