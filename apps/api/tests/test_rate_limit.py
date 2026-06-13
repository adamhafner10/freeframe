"""Tests for rate limiting functionality."""
import pytest
from unittest.mock import MagicMock, patch


class TestCheckRateLimit:
    """Tests for the Redis-based rate limit checker."""

    @patch("apps.api.services.redis_service.get_redis")
    def test_allows_request_under_limit(self, mock_get_redis):
        mock_redis = MagicMock()
        mock_redis.get.return_value = None
        mock_pipe = MagicMock()
        mock_redis.pipeline.return_value = mock_pipe
        mock_get_redis.return_value = mock_redis

        from apps.api.services.redis_service import check_rate_limit

        allowed, retry_after = check_rate_limit("127.0.0.1", "test_action", 5, 60)

        assert allowed is True
        assert retry_after == 0
        mock_pipe.incr.assert_called_once()
        mock_pipe.expire.assert_called_once()
        mock_pipe.execute.assert_called_once()

    @patch("apps.api.services.redis_service.get_redis")
    def test_blocks_request_at_limit(self, mock_get_redis):
        mock_redis = MagicMock()
        mock_redis.get.return_value = "5"
        mock_redis.ttl.return_value = 42
        mock_get_redis.return_value = mock_redis

        from apps.api.services.redis_service import check_rate_limit

        allowed, retry_after = check_rate_limit("127.0.0.1", "test_action", 5, 60)

        assert allowed is False
        assert retry_after == 42

    @patch("apps.api.services.redis_service.get_redis")
    def test_allows_request_below_limit(self, mock_get_redis):
        mock_redis = MagicMock()
        mock_redis.get.return_value = "3"
        mock_pipe = MagicMock()
        mock_redis.pipeline.return_value = mock_pipe
        mock_get_redis.return_value = mock_redis

        from apps.api.services.redis_service import check_rate_limit

        allowed, retry_after = check_rate_limit("127.0.0.1", "test_action", 5, 60)

        assert allowed is True
        assert retry_after == 0

    @patch("apps.api.services.redis_service.get_redis")
    def test_retry_after_minimum_is_one(self, mock_get_redis):
        mock_redis = MagicMock()
        mock_redis.get.return_value = "10"
        mock_redis.ttl.return_value = -1
        mock_get_redis.return_value = mock_redis

        from apps.api.services.redis_service import check_rate_limit

        allowed, retry_after = check_rate_limit("127.0.0.1", "test_action", 10, 60)

        assert allowed is False
        assert retry_after == 1


class TestRateLimitDependency:
    """Tests for the FastAPI rate_limit dependency."""

    @patch("apps.api.middleware.rate_limit.check_rate_limit")
    def test_allows_when_under_limit(self, mock_check):
        mock_check.return_value = (True, 0)
        mock_request = MagicMock()
        # Untrusted public peer — forwarding headers must be ignored; the
        # connecting peer address is the rate-limit key.
        mock_request.client.host = "203.0.113.9"
        mock_request.headers.get.return_value = None

        from apps.api.middleware.rate_limit import rate_limit

        dep = rate_limit("test", 5, 60)
        dep(mock_request)  # Should not raise

        mock_check.assert_called_once_with("203.0.113.9", "test", 5, 60)

    @patch("apps.api.middleware.rate_limit.check_rate_limit")
    def test_raises_429_when_over_limit(self, mock_check):
        mock_check.return_value = (False, 30)
        mock_request = MagicMock()
        mock_request.client.host = "203.0.113.9"
        mock_request.headers.get.return_value = None

        from apps.api.middleware.rate_limit import rate_limit

        dep = rate_limit("test", 5, 60)
        with pytest.raises(Exception) as exc_info:
            dep(mock_request)
        assert exc_info.value.status_code == 429

    @patch("apps.api.middleware.rate_limit.check_rate_limit")
    def test_uses_connecting_peer_ip(self, mock_check):
        mock_check.return_value = (True, 0)
        mock_request = MagicMock()
        mock_request.client.host = "192.0.2.50"
        mock_request.headers.get.return_value = None

        from apps.api.middleware.rate_limit import rate_limit

        dep = rate_limit("test", 5, 60)
        dep(mock_request)

        mock_check.assert_called_once_with("192.0.2.50", "test", 5, 60)

    @patch("apps.api.middleware.rate_limit.check_rate_limit")
    def test_spoofed_headers_ignored_for_untrusted_peer(self, mock_check):
        # An attacker rotating X-Real-Ip / X-Forwarded-For from a public peer
        # must NOT be able to change the rate-limit key.
        mock_check.return_value = (True, 0)
        mock_request = MagicMock()
        mock_request.client.host = "203.0.113.9"  # untrusted public peer
        mock_request.headers.get.side_effect = lambda key: {
            "x-forwarded-for": "1.2.3.4",
            "x-real-ip": "5.6.7.8",
        }.get(key)

        from apps.api.middleware.rate_limit import rate_limit

        dep = rate_limit("test", 5, 60)
        dep(mock_request)

        # Keyed by the connecting peer, not the spoofed headers.
        mock_check.assert_called_once_with("203.0.113.9", "test", 5, 60)

    @patch("apps.api.middleware.rate_limit.check_rate_limit")
    def test_trusted_proxy_honors_forwarded_for_last_hop(self, mock_check):
        # When the peer is a trusted proxy (loopback/RFC1918), the last hop of
        # X-Forwarded-For is the real client and IS honored.
        mock_check.return_value = (True, 0)
        mock_request = MagicMock()
        mock_request.client.host = "127.0.0.1"  # trusted proxy (Traefik)
        mock_request.headers.get.side_effect = lambda key: {
            "x-forwarded-for": "9.9.9.9, 198.51.100.7",
            "x-real-ip": "198.51.100.7",
        }.get(key)

        from apps.api.middleware.rate_limit import rate_limit

        dep = rate_limit("test", 5, 60)
        dep(mock_request)

        mock_check.assert_called_once_with("198.51.100.7", "test", 5, 60)


class TestGlobalRateLimitMiddleware:
    """Tests for the global rate limit middleware."""

    def test_exempt_paths_skipped(self):
        from apps.api.middleware.global_rate_limit import GlobalRateLimitMiddleware, EXEMPT_PATHS

        assert "/health" in EXEMPT_PATHS
        assert "/docs" in EXEMPT_PATHS
        assert "/redoc" in EXEMPT_PATHS
        assert "/openapi.json" in EXEMPT_PATHS

    @patch("apps.api.middleware.global_rate_limit.get_redis")
    def test_get_identity_extracts_user_from_jwt(self, mock_get_redis):
        from apps.api.middleware.global_rate_limit import GlobalRateLimitMiddleware
        from apps.api.config import settings

        middleware = GlobalRateLimitMiddleware(app=MagicMock())

        from jose import jwt
        token = jwt.encode(
            {"sub": "user-123", "type": "access"},
            settings.jwt_secret,
            algorithm=settings.jwt_algorithm,
        )
        mock_request = MagicMock()
        mock_request.headers.get.return_value = f"Bearer {token}"

        identity = middleware._get_identity(mock_request)
        assert identity == "user:user-123"

    def test_get_identity_falls_back_to_ip(self):
        from apps.api.middleware.global_rate_limit import GlobalRateLimitMiddleware

        middleware = GlobalRateLimitMiddleware(app=MagicMock())
        mock_request = MagicMock()
        # Untrusted public peer with no auth — keyed by the connecting peer.
        mock_request.client.host = "203.0.113.1"
        mock_request.headers.get.side_effect = lambda key, default="": {
            "authorization": "",
        }.get(key, default)

        identity = middleware._get_identity(mock_request)
        assert identity == "ip:203.0.113.1"

    @patch("apps.api.middleware.global_rate_limit.get_redis")
    def test_check_allows_under_limit(self, mock_get_redis):
        from apps.api.middleware.global_rate_limit import GlobalRateLimitMiddleware

        mock_redis = MagicMock()
        mock_redis.get.return_value = None
        mock_pipe = MagicMock()
        mock_redis.pipeline.return_value = mock_pipe
        mock_get_redis.return_value = mock_redis

        middleware = GlobalRateLimitMiddleware(app=MagicMock())
        allowed, retry_after = middleware._check("user:123", "global_r", 600)

        assert allowed is True
        assert retry_after == 0

    @patch("apps.api.middleware.global_rate_limit.get_redis")
    def test_check_blocks_over_limit(self, mock_get_redis):
        from apps.api.middleware.global_rate_limit import GlobalRateLimitMiddleware

        mock_redis = MagicMock()
        mock_redis.get.return_value = "600"
        mock_redis.ttl.return_value = 25
        mock_get_redis.return_value = mock_redis

        middleware = GlobalRateLimitMiddleware(app=MagicMock())
        allowed, retry_after = middleware._check("user:123", "global_r", 600)

        assert allowed is False
        assert retry_after == 25
