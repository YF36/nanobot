"""Tests for channel rate limiting and audit logging."""

from __future__ import annotations

import asyncio
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from nanobot.channels.ratelimit import RateLimiter


# ---------------------------------------------------------------------------
# RateLimiter unit tests
# ---------------------------------------------------------------------------

class TestRateLimiter:
    def test_allows_under_limit(self):
        rl = RateLimiter(max_messages=5, window_seconds=60)
        for _ in range(5):
            assert rl.is_allowed("user1") is True

    def test_blocks_over_limit(self):
        rl = RateLimiter(max_messages=3, window_seconds=60)
        for _ in range(3):
            assert rl.is_allowed("user1") is True
        assert rl.is_allowed("user1") is False

    def test_different_senders_independent(self):
        rl = RateLimiter(max_messages=2, window_seconds=60)
        assert rl.is_allowed("a") is True
        assert rl.is_allowed("a") is True
        assert rl.is_allowed("a") is False
        # Different sender still allowed
        assert rl.is_allowed("b") is True
        assert rl.is_allowed("b") is True
        assert rl.is_allowed("b") is False

    def test_window_expiry(self, monkeypatch):
        """After the window passes, sender should be allowed again."""
        fake_time = [100.0]
        monkeypatch.setattr(time, "monotonic", lambda: fake_time[0])

        rl = RateLimiter(max_messages=2, window_seconds=10)
        assert rl.is_allowed("user1") is True
        assert rl.is_allowed("user1") is True
        assert rl.is_allowed("user1") is False

        # Advance past the window
        fake_time[0] = 111.0
        assert rl.is_allowed("user1") is True

    def test_cleanup_removes_stale_senders(self, monkeypatch):
        fake_time = [100.0]
        monkeypatch.setattr(time, "monotonic", lambda: fake_time[0])

        rl = RateLimiter(max_messages=10, window_seconds=10)
        rl.is_allowed("stale_user")
        assert "stale_user" in rl._buckets

        # Advance past 2× window to trigger cleanup
        fake_time[0] = 125.0
        rl.is_allowed("active_user")

        assert "stale_user" not in rl._buckets
        assert "active_user" in rl._buckets


# ---------------------------------------------------------------------------
# BaseChannel integration tests (audit log + rate limiting)
# ---------------------------------------------------------------------------

class _DummyConfig:
    allow_from: list[str] = []


class _DummyChannel:
    """Minimal concrete channel for testing BaseChannel logic."""

    name = "test"

    def __init__(self, config=None, bus=None, rate_limiter=None):
        from nanobot.channels.base import BaseChannel
        # We can't instantiate ABC directly, so we patch
        self._base = BaseChannel.__dict__
        self.config = config or _DummyConfig()
        self.bus = bus or AsyncMock()
        self._running = False
        self._rate_limiter = rate_limiter

    async def start(self):
        pass

    async def stop(self):
        pass

    async def send(self, msg):
        pass


def _make_channel(allow_from=None, rate_limiter=None):
    """Create a testable channel using BaseChannel directly."""
    from nanobot.channels.base import BaseChannel

    cfg = _DummyConfig()
    if allow_from is not None:
        cfg.allow_from = allow_from

    bus = AsyncMock()

    # Create a concrete subclass inline
    class ConcreteChannel(BaseChannel):
        name = "test"
        async def start(self): pass
        async def stop(self): pass
        async def send(self, msg): pass

    ch = ConcreteChannel(cfg, bus, rate_limiter=rate_limiter)
    return ch, bus


class TestBaseChannelAudit:
    @pytest.mark.asyncio
    async def test_accepted_message_audit(self):
        ch, bus = _make_channel()
        with patch("nanobot.channels.base.audit_log") as mock_audit:
            await ch._handle_message("user1", "chat1", "hello")
            mock_audit.info.assert_called_once()
            args = mock_audit.info.call_args
            assert args[0][0] == "channel_message_accepted"

    @pytest.mark.asyncio
    async def test_denied_message_audit(self):
        ch, bus = _make_channel(allow_from=["allowed_user"])
        with patch("nanobot.channels.base.audit_log") as mock_audit:
            await ch._handle_message("blocked_user", "chat1", "hi")
            mock_audit.warning.assert_called_once()
            args = mock_audit.warning.call_args
            assert args[0][0] == "channel_access_denied"
            bus.publish_inbound.assert_not_called()

    @pytest.mark.asyncio
    async def test_rate_limited_message_audit(self):
        rl = RateLimiter(max_messages=1, window_seconds=60)
        ch, bus = _make_channel(rate_limiter=rl)
        with patch("nanobot.channels.base.audit_log") as mock_audit:
            await ch._handle_message("user1", "chat1", "msg1")
            await ch._handle_message("user1", "chat1", "msg2")
            # First accepted, second rate-limited
            assert mock_audit.info.call_count == 1
            assert mock_audit.warning.call_count == 1
            warn_args = mock_audit.warning.call_args
            assert warn_args[0][0] == "channel_rate_limited"
            assert bus.publish_inbound.call_count == 1

    @pytest.mark.asyncio
    async def test_no_rate_limit_when_disabled(self):
        ch, bus = _make_channel(rate_limiter=None)
        with patch("nanobot.channels.base.audit_log"):
            for _ in range(50):
                await ch._handle_message("user1", "chat1", "msg")
            assert bus.publish_inbound.call_count == 50
