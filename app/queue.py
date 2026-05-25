"""Priority queue for LLM requests using PostgreSQL LISTEN/NOTIFY.

Replaces busy-polling (sleep 0.3s loop) with PostgreSQL NOTIFY:
- Workers LISTEN on 'llm_queue_channel'
- INSERT/DELETE triggers NOTIFY to wake waiting workers instantly
- Falls back to periodic check every 2s as safety net
"""
import time
import logging
import select
import threading
from flask import current_app, has_app_context
from app import db
from app.models import LLMQueueEntry

logger = logging.getLogger(__name__)

# Module-level listener state
_listener_lock = threading.Lock()
_listener_started = False
_listener_conn = None


def _get_dsn():
    """Build a libpq DSN from the Flask app's DATABASE_URL."""
    url = current_app.config.get("SQLALCHEMY_DATABASE_URI", "")
    # postgresql://user:pass@host:port/dbname -> key=value DSN
    if url.startswith("postgresql://"):
        url = url.replace("postgresql://", "")
        user_pass, host_db = url.split("@", 1)
        user, password = user_pass.split(":", 1)
        host_port, dbname = host_db.rsplit("/", 1)
        if ":" in host_port:
            host, port = host_port.split(":", 1)
        else:
            host, port = host_port, "5432"
        return f"host={host} port={port} dbname={dbname} user={user} password={password}"
    return ""


def _ensure_trigger():
    """Create the NOTIFY trigger on llm_queue if it doesn't exist."""
    db.session.execute(db.text("""
        CREATE OR REPLACE FUNCTION notify_llm_queue() RETURNS trigger AS $$
        BEGIN
            PERFORM pg_notify('llm_queue_channel', '');
            RETURN NULL;
        END;
        $$ LANGUAGE plpgsql;

        DROP TRIGGER IF EXISTS llm_queue_notify ON llm_queue;
        CREATE TRIGGER llm_queue_notify
            AFTER INSERT OR UPDATE OR DELETE ON llm_queue
            FOR EACH STATEMENT EXECUTE FUNCTION notify_llm_queue();
    """))
    db.session.commit()


def init_queue():
    """Set up the NOTIFY trigger and listener at app startup."""
    try:
        _ensure_trigger()
        logger.info("Queue subsystem initialized (NOTIFY mode)")
    except Exception as e:
        logger.warning("Could not set up queue NOTIFY trigger, will use polling fallback: %s", e)


def enter_queue(user_id, is_premium, timeout=90, _app=None):
    """Enter the queue. Wait via LISTEN/NOTIFY until we are highest-priority.
    Returns entry id or None on timeout.
    """
    app = _app or current_app._get_current_object()

    with app.app_context():
        now = time.time()

        # Clean up stale entries (processing for >120s = crashed worker)
        cutoff = now - 120
        LLMQueueEntry.query.filter(
            LLMQueueEntry.status == "processing",
            LLMQueueEntry.created_at < cutoff,
        ).delete()
        db.session.commit()

        entry = LLMQueueEntry(
            user_id=user_id,
            is_premium=bool(is_premium),
            created_at=now,
            status="waiting",
        )
        db.session.add(entry)
        db.session.commit()
        entry_id = entry.id

    # Set up a dedicated LISTEN connection for this wait
    dsn = None
    listen_conn = None
    try:
        with app.app_context():
            dsn = _get_dsn()
    except Exception:
        dsn = None

    if dsn:
        try:
            import psycopg2
            listen_conn = psycopg2.connect(dsn)
            listen_conn.autocommit = True
            listen_cur = listen_conn.cursor()
            listen_cur.execute("LISTEN llm_queue_channel;")
        except Exception as e:
            logger.warning("LISTEN setup failed, using polling fallback: %s", e)
            listen_conn = None

    deadline = now + timeout
    poll_interval = 2.0  # safety net poll

    try:
        while time.time() < deadline:
            with app.app_context():
                top = (
                    LLMQueueEntry.query
                    .filter_by(status="waiting")
                    .order_by(LLMQueueEntry.is_premium.desc(), LLMQueueEntry.created_at.asc())
                    .first()
                )
                if top and top.id == entry_id:
                    top.status = "processing"
                    db.session.commit()
                    return entry_id

            # Wait for NOTIFY or timeout
            remaining = deadline - time.time()
            if remaining <= 0:
                break

            if listen_conn:
                try:
                    # select() returns when NOTIFY arrives or timeout
                    wait_time = min(remaining, poll_interval)
                    select.select([listen_conn], [], [], wait_time)
                    listen_conn.poll()  # drain notifications
                except Exception:
                    time.sleep(min(remaining, 0.5))
            else:
                time.sleep(min(remaining, poll_interval))

        # Timeout
        with app.app_context():
            LLMQueueEntry.query.filter_by(id=entry_id).delete()
            db.session.commit()
        return None
    except Exception:
        try:
            with app.app_context():
                LLMQueueEntry.query.filter_by(id=entry_id).delete()
                db.session.commit()
        except Exception:
            pass
        raise
    finally:
        if listen_conn:
            try:
                listen_conn.close()
            except Exception:
                pass


def leave_queue(entry_id, _app=None):
    """Remove entry from queue after LLM request completes."""
    if entry_id is None:
        return
    try:
        app = _app or current_app._get_current_object()
        with app.app_context():
            LLMQueueEntry.query.filter_by(id=entry_id).delete()
            db.session.commit()
    except Exception:
        pass


def queue_stats():
    """Return current queue stats for admin dashboard."""
    waiting_free = LLMQueueEntry.query.filter_by(status="waiting", is_premium=False).count()
    waiting_premium = LLMQueueEntry.query.filter_by(status="waiting", is_premium=True).count()
    processing = LLMQueueEntry.query.filter_by(status="processing").count()
    return {"waiting_free": waiting_free, "waiting_premium": waiting_premium, "processing": processing}
