"""Chat generation handler — dispatches GPU work via ecolyxis-api."""
import logging
from sqlalchemy.exc import IntegrityError

from app import db
from app.models import Thread, Message

logger = logging.getLogger('ecolyxis.jobs.chat')


def _persist_assistant(job, text, tokens, reasoning_tokens=0, energy_wh=None, co2e_g=None):
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
        energy_wh=energy_wh,
        co2e_g=co2e_g,
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
    from app.sustainability import PowerSampler, calculate_co2e, estimate_energy_for_tokens

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

    # GPU power sampler — captures real nvidia-smi readings during inference
    sampler = PowerSampler()
    sampler.sample()  # baseline reading

    if precise:
        text, prompt_tokens, completion_tokens = _run_precise(client, msgs, "standard")
        sampler.sample()
        if text:
            publish({"type": "content", "text": text})
        # Compute energy from real GPU power data
        energy_wh = sampler.energy_wh()
        if energy_wh is None:
            # Fallback: estimate from tokens
            energy_wh = estimate_energy_for_tokens(prompt_tokens, completion_tokens)
        co2e_g = calculate_co2e(energy_wh)
        message_id = _persist_assistant(
            job, text, completion_tokens,
            energy_wh=energy_wh, co2e_g=co2e_g,
        )
        return {
            "message_id": message_id,
            "tokens": completion_tokens,
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "reasoning_tokens": 0,
            "energy_wh": energy_wh,
            "co2e_g": co2e_g,
            "via": "local-precise",
        }

    result = stream_remote_job(
        "chat",
        {"messages": msgs, "mode": mode, "stream": True},
        publish,
        client_ref=str(job.id),
    )

    sampler.sample()

    text = result.get("text", "")
    usage = result.get("usage") or {}
    prompt_tokens = int(result.get("prompt_tokens") or usage.get("prompt_tokens") or 0)
    completion_tokens = int(
        result.get("completion_tokens") or usage.get("completion_tokens") or 0
    )

    reasoning_tokens = int(result.get("reasoning_tokens") or 0)

    # Try real GPU power first, fall back to token-based estimate
    energy_wh = result.get("energy_wh") or sampler.energy_wh()
    if energy_wh is None:
        energy_wh = estimate_energy_for_tokens(prompt_tokens, completion_tokens, reasoning_tokens)
    co2e_g = calculate_co2e(energy_wh)

    message_id = _persist_assistant(
        job, text, completion_tokens,
        reasoning_tokens=reasoning_tokens,
        energy_wh=energy_wh, co2e_g=co2e_g,
    )
    return {
        "message_id": message_id,
        "tokens": completion_tokens,
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "reasoning_tokens": reasoning_tokens,
        "energy_wh": energy_wh,
        "co2e_g": co2e_g,
        "via": "ecolyxis-api",
        "gpu": result.get("gpu"),
    }
