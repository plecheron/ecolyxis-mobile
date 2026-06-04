"""Durable, resumable generation jobs backed by Redis.

Two pieces live here:

* **Priority queue** — two lanes (premium / free) as Redis lists. A worker
  claims the next job with an atomic ``LMOVE`` into its own *processing* list
  (the reliable-queue pattern), so a job is never lost in the gap between pop
  and run: if the worker dies, the job sits in its processing list until the
  reaper re-enqueues it.

* **Event log** — a per-job Redis Stream ``job:<id>:events``. The worker
  appends token/progress/terminal events; clients replay them by sequence id,
  so a dropped connection resumes exactly where it left off (``Last-Event-ID``).

Only JSON/text passes through Redis; binary artifacts (images, audio, video)
are written to disk + Postgres by the worker, never streamed through here.
"""
import json

from app.redis_client import get_redis

QUEUE_PREMIUM = "jobs:queue:premium"
QUEUE_FREE = "jobs:queue:free"
PROCESSING_PREFIX = "jobs:processing:"
WORKER_ALIVE_PREFIX = "worker:"
EVENT_TTL_SECONDS = 3600  # keep a finished job's log replayable for 1h


def processing_key(worker_id):
    return f"{PROCESSING_PREFIX}{worker_id}"


def events_key(job_id):
    return f"job:{job_id}:events"


def worker_alive_key(worker_id):
    return f"{WORKER_ALIVE_PREFIX}{worker_id}:alive"


# --- queue ----------------------------------------------------------------

def enqueue(job_id, is_premium):
    """Push a job id onto the premium or free lane."""
    get_redis().lpush(QUEUE_PREMIUM if is_premium else QUEUE_FREE, job_id)


def claim(worker_id, block_s=5):
    """Atomically move the next job id into this worker's processing list.

    Premium lane is checked first (non-blocking); otherwise we wait up to
    ``block_s`` seconds on the free lane. Returns a job id or ``None``.
    """
    r = get_redis()
    pkey = processing_key(worker_id)
    job_id = r.lmove(QUEUE_PREMIUM, pkey, "RIGHT", "LEFT")
    if job_id:
        return job_id
    return r.blmove(QUEUE_FREE, pkey, block_s, "RIGHT", "LEFT")


def ack(worker_id, job_id):
    """Remove a finished job from the worker's processing list."""
    get_redis().lrem(processing_key(worker_id), 1, job_id)


def queue_depth():
    """Return {premium, free} pending counts (for /health and monitoring)."""
    r = get_redis()
    return {"premium": r.llen(QUEUE_PREMIUM), "free": r.llen(QUEUE_FREE)}


# --- event log ------------------------------------------------------------

def publish_event(job_id, event):
    """Append an event dict to the job's stream; returns its sequence id."""
    return get_redis().xadd(events_key(job_id), {"d": json.dumps(event, ensure_ascii=False)})


def read_events(job_id, last_id="0", block_ms=15000, count=500):
    """Block-read events after ``last_id``. Returns [(seq_id, event_dict), ...]."""
    resp = get_redis().xread({events_key(job_id): last_id}, block=block_ms, count=count)
    out = []
    if resp:
        for _stream, entries in resp:
            for seq_id, fields in entries:
                try:
                    out.append((seq_id, json.loads(fields["d"])))
                except (ValueError, KeyError):
                    pass
    return out


def expire_events(job_id, ttl=EVENT_TTL_SECONDS):
    get_redis().expire(events_key(job_id), ttl)


# --- worker liveness ------------------------------------------------------

def heartbeat_worker(worker_id, ttl=30):
    get_redis().set(worker_alive_key(worker_id), "1", ex=ttl)


def worker_is_alive(worker_id):
    return bool(get_redis().exists(worker_alive_key(worker_id)))
