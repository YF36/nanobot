"""Tests for Langfuse observability integration."""

import os
from unittest.mock import patch

import litellm
import pytest
import structlog

from nanobot.config.schema import LangfuseConfig, ObservabilityConfig, Config
from nanobot.providers.litellm_provider import LiteLLMProvider


@pytest.fixture(autouse=True)
def _clean_litellm_callbacks():
    """Reset litellm callbacks before/after each test."""
    original_success = litellm.success_callback[:]
    original_failure = litellm.failure_callback[:]
    yield
    litellm.success_callback = original_success
    litellm.failure_callback = original_failure


@pytest.fixture(autouse=True)
def _clean_env():
    """Remove Langfuse env vars after each test."""
    yield
    for key in ("LANGFUSE_PUBLIC_KEY", "LANGFUSE_SECRET_KEY", "LANGFUSE_HOST"):
        os.environ.pop(key, None)


class TestLangfuseConfig:
    """Config schema tests."""

    def test_default_disabled(self):
        cfg = LangfuseConfig()
        assert cfg.enabled is False
        assert cfg.public_key == ""
        assert cfg.secret_key == ""
        assert cfg.host == ""

    def test_observability_default(self):
        cfg = ObservabilityConfig()
        assert cfg.langfuse.enabled is False

    def test_config_has_observability(self):
        cfg = Config()
        assert hasattr(cfg, "observability")
        assert isinstance(cfg.observability, ObservabilityConfig)


class TestLangfuseCallbackSetup:
    """Callback registration tests."""

    @patch.dict(os.environ, {}, clear=False)
    def test_enabled_with_keys_registers_callbacks(self):
        cfg = LangfuseConfig(
            enabled=True,
            public_key="pk-test-123",
            secret_key="sk-test-456",
            host="https://langfuse.example.com",
        )
        provider = LiteLLMProvider(langfuse_config=cfg)
        assert "langfuse" in litellm.success_callback
        assert "langfuse" in litellm.failure_callback
        assert provider._langfuse_enabled is True
        assert os.environ["LANGFUSE_PUBLIC_KEY"] == "pk-test-123"
        assert os.environ["LANGFUSE_SECRET_KEY"] == "sk-test-456"
        assert os.environ["LANGFUSE_HOST"] == "https://langfuse.example.com"

    def test_disabled_no_callbacks(self):
        cfg = LangfuseConfig(enabled=False, public_key="pk", secret_key="sk")
        provider = LiteLLMProvider(langfuse_config=cfg)
        assert "langfuse" not in litellm.success_callback
        assert provider._langfuse_enabled is False

    def test_enabled_missing_keys_warns_no_callback(self):
        cfg = LangfuseConfig(enabled=True, public_key="", secret_key="")
        provider = LiteLLMProvider(langfuse_config=cfg)
        assert "langfuse" not in litellm.success_callback
        assert provider._langfuse_enabled is False

    @patch.dict(os.environ, {
        "LANGFUSE_PUBLIC_KEY": "env-pk",
        "LANGFUSE_SECRET_KEY": "env-sk",
    }, clear=False)
    def test_enabled_falls_back_to_env_vars(self):
        cfg = LangfuseConfig(enabled=True)
        provider = LiteLLMProvider(langfuse_config=cfg)
        assert "langfuse" in litellm.success_callback
        assert provider._langfuse_enabled is True

    def test_no_config_no_callbacks(self):
        provider = LiteLLMProvider(langfuse_config=None)
        assert provider._langfuse_enabled is False


class TestLangfuseMetadata:
    """Metadata building from structlog contextvars."""

    def _make_provider(self) -> LiteLLMProvider:
        cfg = LangfuseConfig(
            enabled=True, public_key="pk", secret_key="sk",
        )
        return LiteLLMProvider(langfuse_config=cfg)

    def test_metadata_with_contextvars(self):
        provider = self._make_provider()
        structlog.contextvars.clear_contextvars()
        structlog.contextvars.bind_contextvars(
            sender_id="user-42",
            session_key="sess-abc",
            channel="discord",
            chat_id="room-1",
        )
        meta = provider._build_langfuse_metadata()
        assert meta is not None
        assert meta["trace_user_id"] == "user-42"
        assert meta["trace_session_id"] == "sess-abc"
        assert "discord" in meta["trace_tags"]
        assert "chat:room-1" in meta["trace_tags"]
        structlog.contextvars.clear_contextvars()

    def test_metadata_without_contextvars(self):
        provider = self._make_provider()
        structlog.contextvars.clear_contextvars()
        meta = provider._build_langfuse_metadata()
        assert meta is None

    def test_metadata_disabled_returns_none(self):
        provider = LiteLLMProvider(langfuse_config=None)
        structlog.contextvars.bind_contextvars(sender_id="user-1")
        meta = provider._build_langfuse_metadata()
        assert meta is None
        structlog.contextvars.clear_contextvars()
