"""Ecolyxis generation worker.

A dedicated process (systemd: ``ecolyxis-worker.service``) that owns the
connection to the GPU backends. It claims jobs from the Redis priority queue,
runs them, appends events to each job's Redis Stream, and persists the final
artifact to Postgres. Because generation lives here and not in the web request,
a web restart or a client disconnect never interrupts a job.

Liveness is tracked at the *process* level: a single heartbeat thread refreshes
the alive-key for every worker thread, so a thread busy on a long generation is
never mistaken for dead. Only if the whole process dies do the heartbeats lapse,
at which point the reaper re-enqueues the jobs stranded in this process's
processing lists.
"""
import logging
import os
import signal
import socket
import threading
import time
import uuid
from datetime import datetime, timezone

from app import create_app, db
from app.models import GenerationJob
from app.redis_client import get_redis
from app.jobs import (
    PROCESSING_PREFIX,
    ack,
    claim,
    enqueue,
    expire_events,
    heartbeat_worker,
    publish_event,
    worker_is_alive,
)
from app.jobs.handlers.chat import run_chat
from app.jobs.handlers.media import (
    run_image, run_upscale, run_edit, run_video, run_animate,
)

log = logging.getLogger("ecolyxis.worker")

HANDLERS = {
    "chat": run_chat,
    "image": run_image,
    "upscale": run_upscale,
    "edit": run_edit,
    "video": run_video,
    "animate": run_animate,
}

_shutdown = threading.Event()
_thread_wids = []


def _new_worker_id():
    return f"{socket.gethostname()}-{os.getpid()}-{uuid.uuid4().hex[:6]}"


def run_job(app, wid, job_id):
    """Run one job to a terminal state inside a single app context."""
    with app.app_context():
        job = db.session.get(GenerationJob, job_id)
        if job is None:
            log.warning("job %s missing, dropping", job_id)
            return
        if job.status in GenerationJob.TERMINAL:
            log.info("job %s already %s, skipping", job_id, job.status)
            return

        job.status = "running"
        job.worker_id = wid
        job.heartbeat_at = datetime.now(timezone.utc)
        db.session.commit()

        def publish(event):
            return publish_event(job_id, event)

        handler = HANDLERS.get(job.kind)
        try:
            if handler is None:
                raise RuntimeError(f"no handler registered for kind={job.kind!r}")
            result = handler(app, job, publish)
            job = db.session.get(GenerationJob, job_id)
            job.status = "done"
            job.result = result
            job.heartbeat_at = datetime.now(timezone.utc)
            db.session.commit()
            publish({"type": "done", **(result or {})})
            log.info("job %s done", job_id)
        except Exception as e:  # noqa: BLE001 - record and surface to the client
            log.exception("job %s failed (attempt %d/%d)", job_id, job.retry_count + 1, GenerationJob.MAX_RETRIES)
            db.session.rollback()
            job = db.session.get(GenerationJob, job_id)
            if job is not None:
                # Retry on transient failures (connection errors, timeouts)
                is_transient = any(kw in str(e).lower() for kw in (
                    "timeout", "connection", "refused", "reset",
                    "unavailable", "temporary", "503", "502", "500"
                ))
                if is_transient and job.retry_count < GenerationJob.MAX_RETRIES:
                    job.retry_count += 1
                    job.status = "queued"
                    job.worker_id = None
                    job.heartbeat_at = datetime.now(timezone.utc)
                    db.session.commit()
                    enqueue(str(job.id), job.is_premium)
                    publish({"type": "retry", "message": f"Retrying ({job.retry_count}/{GenerationJob.MAX_RETRIES})\u2026", "error": str(e)})
                    log.info("job %s re-queued for retry %d/%d", job_id, job.retry_count, GenerationJob.MAX_RETRIES)
                else:
                    job.status = "error"
                    job.error = str(e)[:2000]
                    job.heartbeat_at = datetime.now(timezone.utc)
                    db.session.commit()
                    publish({"type": "error", "message": str(e)})
        finally:
            expire_events(job_id)


def _run_loop(app, wid):
    log.info("worker thread %s started", wid)
    while not _shutdown.is_set():
        try:
            job_id = claim(wid, block_s=5)
            if not job_id:
                continue
            try:
                run_job(app, wid, job_id)
            finally:
                ack(wid, job_id)
        except Exception:  # noqa: BLE001 - never let the loop die
            log.exception("worker loop error (%s)", wid)
            time.sleep(1)
    log.info("worker thread %s stopped", wid)


def _heartbeat_loop():
    """Refresh the alive-key for every worker thread while the process lives."""
    while not _shutdown.is_set():
        for wid in list(_thread_wids):
            try:
                heartbeat_worker(wid, ttl=30)
            except Exception:  # noqa: BLE001
                pass
        _shutdown.wait(10)


def _requeue_dead(app):
    """Move jobs out of dead workers' processing lists back onto the queue."""
    r = get_redis()
    for pkey in r.scan_iter(f"{PROCESSING_PREFIX}*"):
        wid = pkey[len(PROCESSING_PREFIX):]
        if worker_is_alive(wid):
            continue
        while True:
            job_id = r.rpop(pkey)
            if not job_id:
                break
            with app.app_context():
                job = db.session.get(GenerationJob, job_id)
                if job is None or job.status in GenerationJob.TERMINAL:
                    continue
                job.status = "queued"
                job.worker_id = None
                db.session.commit()
                enqueue(job_id, job.is_premium)
            log.warning("reaper re-enqueued job %s from dead worker %s", job_id, wid)


def _reaper_loop(app):
    while not _shutdown.is_set():
        try:
            _requeue_dead(app)
        except Exception:  # noqa: BLE001
            log.exception("reaper error")
        _shutdown.wait(15)


def main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )
    app = create_app()
    concurrency = int(os.environ.get("WORKER_CONCURRENCY", "4"))
    base = _new_worker_id()

    signal.signal(signal.SIGTERM, lambda *_: _shutdown.set())
    signal.signal(signal.SIGINT, lambda *_: _shutdown.set())

    for i in range(concurrency):
        _thread_wids.append(f"{base}-t{i}")

    log.info("starting worker %s with concurrency=%d", base, concurrency)

    threading.Thread(target=_heartbeat_loop, daemon=True).start()
    threading.Thread(target=_reaper_loop, args=(app,), daemon=True).start()

    workers = []
    for wid in _thread_wids:
        t = threading.Thread(target=_run_loop, args=(app, wid), name=wid)
        t.start()
        workers.append(t)

    for t in workers:
        t.join()
    log.info("worker %s shut down", base)


if __name__ == "__main__":
    main()
