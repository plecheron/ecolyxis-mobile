"""Redis connection helper.

Single shared client used for the durable job queue and the resumable
per-job event log (Redis Streams). Created lazily so importing this module
never requires a live Redis. Values are decoded to str — only JSON/text
events pass through Redis; binary artifacts (images, audio) never do.
"""
import os

import redis

_client = None


def init_redis(url):
    """Create (or replace) the shared client for the given URL."""
    global _client
    _client = redis.Redis.from_url(
        url,
        decode_responses=True,
        socket_connect_timeout=5,
        socket_timeout=30,
        health_check_interval=30,
        retry_on_timeout=True,
    )
    return _client


def get_redis():
    """Return the shared client, creating it from config/env on first use."""
    global _client
    if _client is None:
        url = None
        try:
            from flask import current_app

            if current_app:
                url = current_app.config.get("REDIS_URL")
        except Exception:
            url = None
        url = url or os.environ.get("REDIS_URL", "redis://127.0.0.1:6379/0")
        init_redis(url)
    return _client


def ping_redis():
    """Return True if Redis answers PING, False otherwise (never raises)."""
    try:
        return bool(get_redis().ping())
    except Exception:
        return False
