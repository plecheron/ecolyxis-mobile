"""Redis connection helper.

Uses Sentinel for master discovery when available, with direct URL fallback.
The single shared client is used for the durable job queue and the resumable
per-job event log (Redis Streams). Created lazily so importing this module
never requires a live Redis. Values are decoded to str.
"""
import os
import logging

import redis

_client = None
log = logging.getLogger(__name__)


def _try_sentinel():
    """Attempt to discover the master via Sentinel. Returns a Redis client or None."""
    sentinel_hosts_env = os.environ.get("REDIS_SENTINEL_HOSTS", "")
    if not sentinel_hosts_env:
        return None

    try:
        from redis.sentinel import Sentinel

        sentinel_hosts = []
        for pair in sentinel_hosts_env.split(","):
            pair = pair.strip()
            if ":" in pair:
                host, port = pair.rsplit(":", 1)
                sentinel_hosts.append((host, int(port)))

        if not sentinel_hosts:
            return None

        master_name = os.environ.get("REDIS_SENTINEL_MASTER", "ecolyxis-master")
        password = os.environ.get("REDIS_SENTINEL_PASS", "")

        sentinel = Sentinel(
            sentinel_hosts,
            socket_timeout=5,
            socket_connect_timeout=5,
        )
        master = sentinel.master_for(
            master_name,
            password=password if password else None,
            decode_responses=True,
            socket_connect_timeout=5,
            socket_timeout=30,
            health_check_interval=30,
            retry_on_timeout=True,
        )
        # Verify the connection works
        master.ping()
        log.info("Redis: connected via Sentinel to master %s", master_name)
        return master
    except Exception as e:
        log.warning("Redis: Sentinel discovery failed (%s), falling back to direct URL", e)
        return None


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
    """Return the shared client.

    Priority:
    1. Sentinel discovery (if REDIS_SENTINEL_HOSTS is set)
    2. Flask app config REDIS_URL
    3. Environment REDIS_URL
    4. Default redis://127.0.0.1:6379/0
    """
    global _client
    if _client is None:
        # Try Sentinel first
        _client = _try_sentinel()
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
            log.info("Redis: connected via direct URL")
    return _client


def reset_client():
    """Force re-initialization on next get_redis() call (after failover)."""
    global _client
    _client = None


def ping_redis():
    """Return True if Redis answers PING, False otherwise (never raises)."""
    try:
        return bool(get_redis().ping())
    except Exception:
        return False
