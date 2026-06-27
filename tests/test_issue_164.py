"""Tests for #164: Sprint mode no longer falls back silently."""
from unittest.mock import patch, MagicMock
from app.sprint import SprintClient, VariantNotReadyError


def _make_client(variant="sprint"):
    return SprintClient(
        base_url="http://gpu.test:8081/v1",
        model="test-model",
        variant=variant,
        manager_url="http://gpu.test:8090",
    )


def test_switch_failure_raises_error():
    """_ensure_variant should raise VariantNotReadyError when /switch returns non-200 (#164)."""
    SprintClient._current_variant = None  # reset cache
    SprintClient._variant_cached_at = 0

    client = _make_client("sprint")

    # Mock: status says wrong variant, switch returns 500
    status_resp = MagicMock()
    status_resp.json.return_value = {"ready": True, "switching": False, "variant": "standard"}
    switch_resp = MagicMock()
    switch_resp.status_code = 500

    with patch("app.sprint.requests.get", return_value=status_resp), \
         patch("app.sprint.requests.post", return_value=switch_resp):
        try:
            client._ensure_variant()
            assert False, "Should have raised VariantNotReadyError"
        except VariantNotReadyError as e:
            assert "500" in str(e) or "rejected" in str(e).lower()


def test_switch_exception_raises_error():
    """_ensure_variant should raise when the switch request itself fails (#164)."""
    SprintClient._current_variant = None
    SprintClient._variant_cached_at = 0

    client = _make_client("sprint")

    with patch("app.sprint.requests.get", side_effect=Exception("conn refused")), \
         patch("app.sprint.requests.post", side_effect=Exception("conn refused")):
        try:
            client._ensure_variant()
            assert False, "Should have raised VariantNotReadyError"
        except VariantNotReadyError as e:
            assert "failed" in str(e).lower() or "refused" in str(e).lower()


def test_stale_cache_invalidated():
    """Cache should be verified against gpu-manager status, not trusted blindly (#164)."""
    import time
    # Set stale cache claiming sprint is loaded
    SprintClient._current_variant = "sprint"
    SprintClient._variant_cached_at = time.time() - 120  # 2 minutes ago — stale

    client = _make_client("sprint")

    # Mock: gpu-manager says standard is loaded now (keep-warm killed sprint)
    status_resp = MagicMock()
    status_resp.json.return_value = {"ready": True, "switching": False, "variant": "standard"}
    switch_resp = MagicMock()
    switch_resp.status_code = 200
    # After switch, status says sprint is loaded
    ready_resp = MagicMock()
    ready_resp.json.return_value = {"ready": True, "switching": False, "variant": "sprint"}

    with patch("app.sprint.requests.get", side_effect=[status_resp, ready_resp] + [ready_resp] * 200), \
         patch("app.sprint.requests.post", return_value=switch_resp):
        client._ensure_variant()  # should NOT raise
        assert SprintClient._current_variant == "sprint"


def test_fresh_cache_skips_switch():
    """When cache is fresh and matches, no switch request should be made."""
    import time
    SprintClient._current_variant = "sprint"
    SprintClient._variant_cached_at = time.time()  # fresh

    client = _make_client("sprint")

    post_called = []
    def track_post(*a, **kw):
        post_called.append(True)
        resp = MagicMock()
        resp.status_code = 200
        return resp

    # Status check should succeed (variant matches), no switch needed
    status_resp = MagicMock()
    status_resp.json.return_value = {"ready": True, "switching": False, "variant": "sprint"}

    with patch("app.sprint.requests.get", return_value=status_resp), \
         patch("app.sprint.requests.post", side_effect=track_post):
        client._ensure_variant()
        assert len(post_called) == 0, "Switch should not be called when variant is verified loaded"


def test_timeout_raises_error():
    """_ensure_variant should raise after polling timeout (#164)."""
    SprintClient._current_variant = None
    SprintClient._variant_cached_at = 0

    client = _make_client("sprint")

    # Mock: switch succeeds but status never shows ready
    status_resp = MagicMock()
    status_resp.json.return_value = {"ready": False, "switching": True, "variant": None}
    switch_resp = MagicMock()
    switch_resp.status_code = 200

    with patch("app.sprint.requests.get", return_value=status_resp), \
         patch("app.sprint.requests.post", return_value=switch_resp), \
         patch("app.sprint.time.sleep"):  # skip sleeps
        try:
            client._ensure_variant()
            assert False, "Should have raised VariantNotReadyError on timeout"
        except VariantNotReadyError as e:
            assert "timed out" in str(e).lower()
