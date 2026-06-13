"""Redis client tests: Sentinel discovery, direct URL, ping, reset."""
import os
from unittest.mock import patch, MagicMock
from app.redis_client import _try_sentinel, get_redis, reset_client, ping_redis, init_redis


def test_try_sentinel_no_env():
    """No REDIS_SENTINEL_HOSTS set → returns None."""
    with patch.dict(os.environ, {}, clear=False):
        os.environ.pop("REDIS_SENTINEL_HOSTS", None)
        result = _try_sentinel()
        assert result is None


def test_try_sentinel_empty_env():
    """Empty REDIS_SENTINEL_HOSTS → returns None."""
    with patch.dict(os.environ, {"REDIS_SENTINEL_HOSTS": ""}):
        result = _try_sentinel()
        assert result is None


def test_get_redis_no_sentinel_returns_client(app):
    """Without Sentinel, get_redis should return a working client."""
    reset_client()
    with app.app_context():
        with patch.dict(os.environ, {"REDIS_SENTINEL_HOSTS": "", "REDIS_URL": "redis://localhost:6379/0"}):
            client = get_redis()
            assert client is not None


def test_reset_client():
    """reset_client sets _client to None."""
    reset_client()
    # Calling get_redis will re-init — but we just verify reset works
    from app.redis_client import _client
    assert _client is None


def test_ping_redis_success(app):
    reset_client()
    mock_redis = MagicMock()
    mock_redis.ping.return_value = True
    with patch("app.redis_client.get_redis", return_value=mock_redis):
        assert ping_redis() is True


def test_ping_redis_failure(app):
    mock_redis = MagicMock()
    mock_redis.ping.side_effect = Exception("Connection refused")
    with patch("app.redis_client.get_redis", return_value=mock_redis):
        assert ping_redis() is False


def test_init_redis_creates_client():
    client = init_redis("redis://localhost:6379/2")
    assert client is not None


def test_get_redis_caches_client(app):
    """get_redis returns the same client on subsequent calls."""
    reset_client()
    with app.app_context():
        with patch.dict(os.environ, {"REDIS_SENTINEL_HOSTS": "", "REDIS_URL": "redis://localhost:6379/0"}):
            c1 = get_redis()
            c2 = get_redis()
            assert c1 is c2
