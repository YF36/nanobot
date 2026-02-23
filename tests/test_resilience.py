"""Tests for resilience: ResilienceConfig defaults, circuit breaker, timeout/retry kwargs."""

import asyncio
import time
from unittest.mock import AsyncMock, patch

import pytest

from nanobot.config.schema import ResilienceConfig, ProviderConfig
from nanobot.providers.litellm_provider import LiteLLMProvider


# ---------------------------------------------------------------------------
# 1. ResilienceConfig defaults & backward compat
# ---------------------------------------------------------------------------

class TestResilienceConfigDefaults:
    def test_defaults(self):
        rc = ResilienceConfig()
        assert rc.timeout == 120
        assert rc.max_retries == 3
        assert rc.circuit_breaker_threshold == 5
        assert rc.circuit_breaker_cooldown == 60

    def test_custom_values(self):
        rc = ResilienceConfig(timeout=30, max_retries=1, circuit_breaker_threshold=2, circuit_breaker_cooldown=10)
        assert rc.timeout == 30
        assert rc.max_retries == 1
        assert rc.circuit_breaker_threshold == 2
        assert rc.circuit_breaker_cooldown == 10

    def test_provider_config_has_resilience(self):
        pc = ProviderConfig()
        assert isinstance(pc.resilience, ResilienceConfig)
        assert pc.resilience.timeout == 120

    def test_provider_config_backward_compat(self):
        """ProviderConfig without explicit resilience still works."""
        pc = ProviderConfig(api_key="test-key")
        assert pc.api_key == "test-key"
        assert pc.resilience.max_retries == 3


# ---------------------------------------------------------------------------
# 2. Circuit breaker logic
# ---------------------------------------------------------------------------

class TestCircuitBreaker:
    def _make_provider(self, threshold=3, cooldown=1):
        rc = ResilienceConfig(circuit_breaker_threshold=threshold, circuit_breaker_cooldown=cooldown)
        return LiteLLMProvider(api_key="fake", resilience_config=rc)

    def test_initially_closed(self):
        p = self._make_provider()
        assert p._check_circuit_breaker() is None

    def test_opens_after_threshold(self):
        p = self._make_provider(threshold=3)
        for _ in range(3):
            p._record_result(False)
        err = p._check_circuit_breaker()
        assert err is not None
        assert "Circuit breaker open" in err

    def test_success_resets_counter(self):
        p = self._make_provider(threshold=3)
        p._record_result(False)
        p._record_result(False)
        p._record_result(True)
        assert p._consecutive_failures == 0
        assert p._check_circuit_breaker() is None

    def test_half_open_after_cooldown(self):
        p = self._make_provider(threshold=2, cooldown=0)
        p._record_result(False)
        p._record_result(False)
        # cooldown=0 means it expires immediately
        time.sleep(0.01)
        assert p._check_circuit_breaker() is None  # half-open

    def test_no_circuit_breaker_when_disabled(self):
        rc = ResilienceConfig(circuit_breaker_threshold=0)
        p = LiteLLMProvider(api_key="fake", resilience_config=rc)
        for _ in range(10):
            p._record_result(False)
        assert p._check_circuit_breaker() is None


# ---------------------------------------------------------------------------
# 3. Timeout / retry kwargs injection
# ---------------------------------------------------------------------------

class TestTimeoutRetryKwargs:
    @pytest.mark.asyncio
    async def test_acompletion_receives_timeout_and_retries(self):
        rc = ResilienceConfig(timeout=60, max_retries=2)
        p = LiteLLMProvider(api_key="fake", resilience_config=rc)

        captured_kwargs = {}

        async def fake_acompletion(**kwargs):
            captured_kwargs.update(kwargs)
            from unittest.mock import MagicMock
            choice = MagicMock()
            choice.message.content = "ok"
            choice.message.tool_calls = None
            choice.finish_reason = "stop"
            resp = MagicMock()
            resp.choices = [choice]
            return resp

        with patch("nanobot.providers.litellm_provider.acompletion", side_effect=fake_acompletion):
            await p.chat(messages=[{"role": "user", "content": "hi"}])

        assert captured_kwargs["request_timeout"] == 60
        assert captured_kwargs["num_retries"] == 2

    @pytest.mark.asyncio
    async def test_timeout_returns_error_response(self):
        rc = ResilienceConfig(timeout=1)
        p = LiteLLMProvider(api_key="fake", resilience_config=rc)

        async def slow_acompletion(**kwargs):
            await asyncio.sleep(999)

        with patch("nanobot.providers.litellm_provider.acompletion", side_effect=slow_acompletion):
            with patch("nanobot.providers.litellm_provider.asyncio.wait_for", side_effect=asyncio.TimeoutError):
                resp = await p.chat(messages=[{"role": "user", "content": "hi"}])

        assert resp.finish_reason == "error"
        assert "timed out" in resp.content

    @pytest.mark.asyncio
    async def test_timeout_triggers_circuit_breaker(self):
        rc = ResilienceConfig(timeout=1, circuit_breaker_threshold=2)
        p = LiteLLMProvider(api_key="fake", resilience_config=rc)

        with patch("nanobot.providers.litellm_provider.acompletion", side_effect=asyncio.TimeoutError):
            with patch("nanobot.providers.litellm_provider.asyncio.wait_for", side_effect=asyncio.TimeoutError):
                await p.chat(messages=[{"role": "user", "content": "hi"}])
                await p.chat(messages=[{"role": "user", "content": "hi"}])

        assert p._consecutive_failures == 2
        assert p._check_circuit_breaker() is not None

    @pytest.mark.asyncio
    async def test_no_resilience_config_skips_injection(self):
        p = LiteLLMProvider(api_key="fake", resilience_config=None)

        captured_kwargs = {}

        async def fake_acompletion(**kwargs):
            captured_kwargs.update(kwargs)
            from unittest.mock import MagicMock
            choice = MagicMock()
            choice.message.content = "ok"
            choice.message.tool_calls = None
            choice.finish_reason = "stop"
            resp = MagicMock()
            resp.choices = [choice]
            return resp

        with patch("nanobot.providers.litellm_provider.acompletion", side_effect=fake_acompletion):
            await p.chat(messages=[{"role": "user", "content": "hi"}])

        assert "request_timeout" not in captured_kwargs
        assert "num_retries" not in captured_kwargs
