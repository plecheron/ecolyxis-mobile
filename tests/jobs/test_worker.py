"""Worker tests: run_job, reaper, heartbeat, _new_worker_id."""
import uuid
from unittest.mock import patch, MagicMock
from datetime import datetime, timezone
from app.models import GenerationJob, Thread
from app.jobs.worker import run_job, _new_worker_id, _requeue_dead


def _thread(db, user):
    t = Thread(id=str(uuid.uuid4()), user_id=user.id, title="Job Thread")
    db.session.add(t)
    db.session.commit()
    return t


def _job(db, user, thread, kind="chat", status="queued"):
    job = GenerationJob(
        id=str(uuid.uuid4()),
        user_id=user.id,
        thread_id=thread.id,
        kind=kind,
        status=status,
        params={},
    )
    db.session.add(job)
    db.session.commit()
    return job


# ─── _new_worker_id ───

def test_new_worker_id():
    wid = _new_worker_id()
    assert "-" in wid
    parts = wid.split("-")
    assert len(parts) >= 3


# ─── run_job ───

def test_run_job_missing(app, db, make_user):
    """run_job with non-existent job should silently return."""
    with app.app_context():
        run_job(app, "test-wid", "nonexistent-job-id")


def test_run_job_already_done(app, db, make_user):
    user = make_user()
    thread = _thread(db, user)
    job = _job(db, user, thread, status="done")
    with app.app_context():
        run_job(app, "test-wid", job.id)
    db.session.refresh(job)
    assert job.status == "done"


def test_run_job_success(app, db, make_user):
    user = make_user()
    thread = _thread(db, user)
    job = _job(db, user, thread, kind="chat")
    with app.app_context():
        with patch("app.jobs.worker.HANDLERS", {"chat": MagicMock(return_value={"content": "Hello"})}):
            run_job(app, "test-wid", job.id)
    db.session.refresh(job)
    assert job.status == "done"
    assert job.result["content"] == "Hello"


def test_run_job_no_handler(app, db, make_user):
    user = make_user()
    thread = _thread(db, user)
    job = _job(db, user, thread, kind="unknown_kind")
    with app.app_context():
        run_job(app, "test-wid", job.id)
    db.session.refresh(job)
    assert job.status == "error"
    assert "no handler" in job.error.lower()


def test_run_job_handler_exception(app, db, make_user):
    user = make_user()
    thread = _thread(db, user)
    job = _job(db, user, thread, kind="chat")
    mock_handler = MagicMock(side_effect=RuntimeError("GPU down"))
    with app.app_context():
        with patch("app.jobs.worker.HANDLERS", {"chat": mock_handler}):
            run_job(app, "test-wid", job.id)
    db.session.refresh(job)
    assert job.status == "error"
    assert "GPU down" in job.error


# ─── _requeue_dead ───

def test_requeue_dead_worker(app, db, make_user):
    """Jobs from dead workers should be re-enqueued."""
    user = make_user()
    thread = _thread(db, user)
    job = _job(db, user, thread, status="running")
    with app.app_context():
        mock_redis = MagicMock()
        mock_redis.scan_iter.return_value = iter(["ecolyxis:processing:dead-worker"])
        mock_redis.rpop.side_effect = [job.id, None]  # return job once then empty
        with patch("app.jobs.worker.get_redis", return_value=mock_redis), \
             patch("app.jobs.worker.worker_is_alive", return_value=False), \
             patch("app.jobs.worker.enqueue"):
            _requeue_dead(app)
    db.session.refresh(job)
    assert job.status == "queued"
    assert job.worker_id is None


def test_requeue_skip_alive_worker(app, db, make_user):
    """Jobs from alive workers should NOT be re-enqueued."""
    user = make_user()
    with app.app_context():
        mock_redis = MagicMock()
        mock_redis.scan_iter.return_value = iter(["ecolyxis:processing:alive-worker"])
        with patch("app.jobs.worker.get_redis", return_value=mock_redis), \
             patch("app.jobs.worker.worker_is_alive", return_value=True):
            _requeue_dead(app)
        # rpop should never be called
        mock_redis.rpop.assert_not_called()
