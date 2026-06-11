"""Chat generation handler — dispatches GPU work via ecolyxis-api."""
from sqlalchemy.exc import IntegrityError

from app import db
from app.models import Thread, Message


def _persist_assistant(job, text, tokens, reasoning_tokens=0):
    """Insert the assistant message exactly once for this job. Returns its id."""
    existing = Message.query.filter_by(job_id=job.id).first()
    if existing:
        return existing.id
    msg = Message(
        thread_id=job.thread_id,
        role="assistant",
        content=text,
        tokens_used=tokens,
        reasoning_tokens=reasoning_tokens or None,
        job_id=job.id,
    )
    db.session.add(msg)
    try:
        db.session.commit()
    except IntegrityError:
        db.session.rollback()
        existing = Message.query.filter_by(job_id=job.id).first()
        return existing.id if existing else None
    return msg.id


def run_chat(app, job, publish):
    """Run a chat job via ecolyxis-api (precise mode still uses local pipeline)."""
    from app.chat import get_client, _run_precise
    from app.jobs.api_client import stream_remote_job
    from app.llm import get_workspace_context

    thread = db.session.get(Thread, job.thread_id)
    if thread is None:
        raise RuntimeError("thread no longer exists")

    params = job.params or {}
    mode = params.get("mode", "standard")
    precise = params.get("precise", mode == "precise")
    show_thinking = params.get("show_thinking", True)

    client = get_client()
    workspace_context = get_workspace_context(thread)
    msgs = client.build_messages(thread, mode=mode, workspace_context=workspace_context)

    publish({"type": "stream_start"})

    if precise:
        text, prompt_tokens, completion_tokens = _run_precise(client, msgs, "standard")
        if text:
            publish({"type": "content", "text": text})
        message_id = _persist_assistant(job, text, completion_tokens)
        return {
            "message_id": message_id,
            "tokens": completion_tokens,
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "reasoning_tokens": 0,
            "via": "local-precise",
        }

    result = stream_remote_job(
        "chat",
        {"messages": msgs, "mode": mode, "stream": True},
        publish,
        client_ref=str(job.id),
    )

    text = result.get("text", "")
    usage = result.get("usage") or {}
    prompt_tokens = int(result.get("prompt_tokens") or usage.get("prompt_tokens") or 0)
    completion_tokens = int(
        result.get("completion_tokens") or usage.get("completion_tokens") or 0
    )

    reasoning_tokens = int(result.get("reasoning_tokens") or 0)
    message_id = _persist_assistant(job, text, completion_tokens, reasoning_tokens=reasoning_tokens)
    return {
        "message_id": message_id,
        "tokens": completion_tokens,
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "reasoning_tokens": reasoning_tokens,
        "via": "ecolyxis-api",
        "gpu": result.get("gpu"),
    }
