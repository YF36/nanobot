"""LiteLLM provider implementation for multi-provider support."""

import asyncio
import json
import json_repair
import logging
import os
import time
from typing import Any

import litellm
import structlog
from litellm import acompletion

from nanobot.logging import get_logger, mask_secret
from nanobot.providers.base import LLMProvider, LLMResponse, ToolCallRequest
from nanobot.providers.registry import find_by_model, find_gateway

logger = get_logger("nanobot.providers.litellm")


# Standard OpenAI chat-completion message keys; extras (e.g. reasoning_content) are stripped for strict providers.
_ALLOWED_MSG_KEYS = frozenset({"role", "content", "tool_calls", "tool_call_id", "name"})


class LiteLLMProvider(LLMProvider):
    """
    LLM provider using LiteLLM for multi-provider support.
    
    Supports OpenRouter, Anthropic, OpenAI, Gemini, MiniMax, and many other providers through
    a unified interface.  Provider-specific logic is driven by the registry
    (see providers/registry.py) — no if-elif chains needed here.
    """
    
    def __init__(
        self,
        api_key: str | None = None,
        api_base: str | None = None,
        default_model: str = "anthropic/claude-opus-4-5",
        extra_headers: dict[str, str] | None = None,
        provider_name: str | None = None,
        langfuse_config: Any | None = None,
        resilience_config: Any | None = None,
    ):
        super().__init__(api_key, api_base)
        self.default_model = default_model
        self.extra_headers = extra_headers or {}
        self._langfuse_enabled = False

        # Resilience: timeout / retry / circuit-breaker
        self._resilience = resilience_config  # ResilienceConfig or None
        self._consecutive_failures: int = 0
        self._circuit_open_until: float = 0.0

        # Detect gateway / local deployment.
        # provider_name (from config key) is the primary signal;
        # api_key / api_base are fallback for auto-detection.
        self._gateway = find_gateway(provider_name, api_key, api_base)

        # Configure environment variables
        if api_key:
            self._setup_env(api_key, api_base, default_model)
            logger.info("provider_initialized", model=default_model, api_key=mask_secret(api_key))

        if api_base:
            litellm.api_base = api_base

        # Disable LiteLLM logging noise
        litellm.suppress_debug_info = True
        # Drop unsupported parameters for providers (e.g., gpt-5 rejects some params)
        litellm.drop_params = True

        # Setup Langfuse observability
        if langfuse_config is not None:
            self._setup_langfuse(langfuse_config)
    
    def _setup_env(self, api_key: str, api_base: str | None, model: str) -> None:
        """Set environment variables based on detected provider."""
        spec = self._gateway or find_by_model(model)
        if not spec:
            return
        if not spec.env_key:
            # OAuth/provider-only specs (for example: openai_codex)
            return

        # Gateway/local overrides existing env; standard provider doesn't
        if self._gateway:
            os.environ[spec.env_key] = api_key
        else:
            os.environ.setdefault(spec.env_key, api_key)

        # Resolve env_extras placeholders:
        #   {api_key}  → user's API key
        #   {api_base} → user's api_base, falling back to spec.default_api_base
        effective_base = api_base or spec.default_api_base
        for env_name, env_val in spec.env_extras:
            resolved = env_val.replace("{api_key}", api_key)
            resolved = resolved.replace("{api_base}", effective_base)
            os.environ.setdefault(env_name, resolved)

    def _setup_langfuse(self, config: Any) -> None:
        """Configure Langfuse callbacks if enabled and keys are available."""
        if not getattr(config, "enabled", False):
            return

        public_key = config.public_key or os.environ.get("LANGFUSE_PUBLIC_KEY", "")
        secret_key = config.secret_key or os.environ.get("LANGFUSE_SECRET_KEY", "")
        host = config.host or os.environ.get("LANGFUSE_HOST", "")

        if not public_key or not secret_key:
            logger.warning("langfuse_missing_keys", hint="Set public_key and secret_key or LANGFUSE_PUBLIC_KEY/LANGFUSE_SECRET_KEY env vars")
            return

        # Set env vars so LiteLLM's built-in Langfuse integration picks them up
        os.environ.setdefault("LANGFUSE_PUBLIC_KEY", public_key)
        os.environ.setdefault("LANGFUSE_SECRET_KEY", secret_key)
        if host:
            os.environ.setdefault("LANGFUSE_HOST", host)

        # Register callbacks
        if "langfuse" not in litellm.success_callback:
            litellm.success_callback.append("langfuse")
        if "langfuse" not in litellm.failure_callback:
            litellm.failure_callback.append("langfuse")

        self._langfuse_enabled = True
        logger.info("langfuse_enabled", host=host or "(default)")

    def _build_langfuse_metadata(self) -> dict[str, Any] | None:
        """Build Langfuse trace metadata from structlog contextvars."""
        if not self._langfuse_enabled:
            return None

        ctx = structlog.contextvars.get_contextvars()
        if not ctx:
            return None

        metadata: dict[str, Any] = {}
        if ctx.get("sender_id"):
            metadata["trace_user_id"] = ctx["sender_id"]
        if ctx.get("session_key"):
            metadata["trace_session_id"] = ctx["session_key"]

        tags = []
        if ctx.get("channel"):
            tags.append(ctx["channel"])
        if ctx.get("chat_id"):
            tags.append(f"chat:{ctx['chat_id']}")
        if tags:
            metadata["trace_tags"] = tags

        return metadata if metadata else None
    
    def _resolve_model(self, model: str) -> str:
        """Resolve model name by applying provider/gateway prefixes."""
        if self._gateway:
            # Gateway mode: apply gateway prefix, skip provider-specific prefixes
            prefix = self._gateway.litellm_prefix
            if self._gateway.strip_model_prefix:
                model = model.split("/")[-1]
            if prefix and not model.startswith(f"{prefix}/"):
                model = f"{prefix}/{model}"
            return model
        
        # Standard mode: auto-prefix for known providers
        spec = find_by_model(model)
        if spec and spec.litellm_prefix:
            model = self._canonicalize_explicit_prefix(model, spec.name, spec.litellm_prefix)
            if not any(model.startswith(s) for s in spec.skip_prefixes):
                model = f"{spec.litellm_prefix}/{model}"

        return model

    @staticmethod
    def _canonicalize_explicit_prefix(model: str, spec_name: str, canonical_prefix: str) -> str:
        """Normalize explicit provider prefixes like `github-copilot/...`."""
        if "/" not in model:
            return model
        prefix, remainder = model.split("/", 1)
        if prefix.lower().replace("-", "_") != spec_name:
            return model
        return f"{canonical_prefix}/{remainder}"
    
    def _supports_cache_control(self, model: str) -> bool:
        """Return True when the provider supports cache_control on content blocks."""
        if self._gateway is not None:
            return self._gateway.supports_prompt_caching
        spec = find_by_model(model)
        return spec is not None and spec.supports_prompt_caching

    def _apply_cache_control(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None,
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]] | None]:
        """Return copies of messages and tools with cache_control injected.

        Strategy (mirrors pi-mono):
        - System message: cache_control on last content block (stable prefix)
        - Last user message: cache_control on last content block (conversation history)
        - Tools list: cache_control on last tool definition
        """
        cache_ctrl = {"type": "ephemeral"}
        new_messages = []

        # Find index of last user message
        last_user_idx = -1
        for i in range(len(messages) - 1, -1, -1):
            if messages[i].get("role") == "user":
                last_user_idx = i
                break

        for idx, msg in enumerate(messages):
            role = msg.get("role")
            if role == "system":
                content = msg["content"]
                if isinstance(content, str):
                    # Single string: wrap as one cacheable block
                    new_content = [{"type": "text", "text": content, "cache_control": cache_ctrl}]
                elif isinstance(content, list) and len(content) >= 2:
                    # Two-block layout from build_messages: [static, dynamic]
                    # Cache only the static (first) block; dynamic block changes every request
                    new_content = [
                        {**content[0], "cache_control": cache_ctrl},
                        *content[1:],
                    ]
                elif isinstance(content, list) and content:
                    new_content = [{**content[-1], "cache_control": cache_ctrl}]
                else:
                    new_content = content
                new_messages.append({**msg, "content": new_content})
            elif role == "user" and idx == last_user_idx:
                content = msg["content"]
                if isinstance(content, str):
                    new_content = [{"type": "text", "text": content, "cache_control": cache_ctrl}]
                elif isinstance(content, list) and content:
                    new_content = list(content)
                    new_content[-1] = {**new_content[-1], "cache_control": cache_ctrl}
                else:
                    new_content = content
                new_messages.append({**msg, "content": new_content})
            else:
                new_messages.append(msg)

        new_tools = tools
        if tools:
            new_tools = list(tools)
            new_tools[-1] = {**new_tools[-1], "cache_control": cache_ctrl}

        return new_messages, new_tools

    def _apply_model_overrides(self, model: str, kwargs: dict[str, Any]) -> None:
        """Apply model-specific parameter overrides from the registry."""
        model_lower = model.lower()
        spec = find_by_model(model)
        if spec:
            for pattern, overrides in spec.model_overrides:
                if pattern in model_lower:
                    kwargs.update(overrides)
                    return
    
    @staticmethod
    def _sanitize_messages(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Strip non-standard keys and ensure assistant messages have a content key."""
        sanitized = []
        for msg in messages:
            clean = {k: v for k, v in msg.items() if k in _ALLOWED_MSG_KEYS}
            # Strict providers require "content" even when assistant only has tool_calls
            if clean.get("role") == "assistant" and "content" not in clean:
                clean["content"] = None
            # Ensure tool_calls arguments are JSON strings, not dicts
            if "tool_calls" in clean and clean["tool_calls"]:
                fixed_calls = []
                for tc in clean["tool_calls"]:
                    tc = dict(tc)  # shallow copy
                    if "function" in tc:
                        fn = dict(tc["function"])
                        if isinstance(fn.get("arguments"), dict):
                            fn["arguments"] = json.dumps(fn["arguments"], ensure_ascii=False)
                        tc["function"] = fn
                    fixed_calls.append(tc)
                clean["tool_calls"] = fixed_calls
            sanitized.append(clean)
        return sanitized

    def _check_circuit_breaker(self) -> str | None:
        """Return an error message if the circuit is open, else None."""
        rc = self._resilience
        if not rc or rc.circuit_breaker_threshold <= 0:
            return None
        if self._consecutive_failures < rc.circuit_breaker_threshold:
            return None
        now = time.monotonic()
        if now < self._circuit_open_until:
            return (
                f"Circuit breaker open: {self._consecutive_failures} consecutive failures. "
                f"Retry after {int(self._circuit_open_until - now)}s cooldown."
            )
        # Cooldown expired → half-open: allow one probe attempt
        return None

    def _record_result(self, success: bool) -> None:
        """Update circuit-breaker counters after a call."""
        rc = self._resilience
        if not rc or rc.circuit_breaker_threshold <= 0:
            return
        if success:
            self._consecutive_failures = 0
            self._circuit_open_until = 0.0
        else:
            self._consecutive_failures += 1
            if self._consecutive_failures >= rc.circuit_breaker_threshold:
                self._circuit_open_until = time.monotonic() + rc.circuit_breaker_cooldown
                logger.warning(
                    "circuit_breaker_opened",
                    failures=self._consecutive_failures,
                    cooldown=rc.circuit_breaker_cooldown,
                )

    async def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        model: str | None = None,
        max_tokens: int = 4096,
        temperature: float = 0.7,
    ) -> LLMResponse:
        """
        Send a chat completion request via LiteLLM.
        
        Args:
            messages: List of message dicts with 'role' and 'content'.
            tools: Optional list of tool definitions in OpenAI format.
            model: Model identifier (e.g., 'anthropic/claude-sonnet-4-5').
            max_tokens: Maximum tokens in response.
            temperature: Sampling temperature.
        
        Returns:
            LLMResponse with content and/or tool calls.
        """
        original_model = model or self.default_model
        model = self._resolve_model(original_model)

        if self._supports_cache_control(original_model):
            messages, tools = self._apply_cache_control(messages, tools)

        # Clamp max_tokens to at least 1 — negative or zero values cause
        # LiteLLM to reject the request with "max_tokens must be at least 1".
        max_tokens = max(1, max_tokens)
        
        kwargs: dict[str, Any] = {
            "model": model,
            "messages": self._sanitize_messages(self._sanitize_empty_content(messages)),
            "max_tokens": max_tokens,
            "temperature": temperature,
        }
        
        # Apply model-specific overrides (e.g. kimi-k2.5 temperature)
        self._apply_model_overrides(model, kwargs)
        
        # Pass api_key directly — more reliable than env vars alone
        if self.api_key:
            kwargs["api_key"] = self.api_key
        
        # Pass api_base for custom endpoints
        if self.api_base:
            kwargs["api_base"] = self.api_base
        
        # Pass extra headers (e.g. APP-Code for AiHubMix)
        if self.extra_headers:
            kwargs["extra_headers"] = self.extra_headers
        
        if tools:
            kwargs["tools"] = tools
            kwargs["tool_choice"] = "auto"

        # Inject Langfuse trace metadata
        langfuse_metadata = self._build_langfuse_metadata()
        if langfuse_metadata:
            kwargs["metadata"] = langfuse_metadata

        # DEBUG: log full kwargs sent to LiteLLM (redact secrets)
        if logging.getLogger("nanobot").isEnabledFor(logging.DEBUG):
            import copy as _copy
            _dbg = _copy.deepcopy(kwargs)
            _dbg.pop("api_key", None)
            _dbg.pop("extra_headers", None)
            # Truncate message content for readability
            if "messages" in _dbg:
                for _m in _dbg["messages"]:
                    if isinstance(_m.get("content"), str) and len(_m["content"]) > 200:
                        _m["content"] = _m["content"][:200] + f"... ({len(_m['content'])} chars)"
            logger.debug("DEBUG_litellm_kwargs", **{k: v for k, v in _dbg.items() if k != "metadata"})

        try:
            # --- Circuit breaker check ---
            cb_error = self._check_circuit_breaker()
            if cb_error:
                return LLMResponse(content=f"Error calling LLM: {cb_error}", finish_reason="error")

            # --- Inject resilience kwargs (LiteLLM built-in retry + timeout) ---
            rc = self._resilience
            if rc:
                kwargs["request_timeout"] = rc.timeout
                kwargs["num_retries"] = rc.max_retries

            # --- Call with asyncio.wait_for safety net ---
            safety_timeout = (rc.timeout + 30) if rc else None
            coro = acompletion(**kwargs)
            if safety_timeout:
                response = await asyncio.wait_for(coro, timeout=safety_timeout)
            else:
                response = await coro

            self._record_result(True)
            return self._parse_response(response)
        except asyncio.TimeoutError:
            self._record_result(False)
            logger.error("llm_call_timeout", model=model)
            return LLMResponse(
                content="Error calling LLM: request timed out",
                finish_reason="error",
            )
        except Exception as e:
            self._record_result(False)
            error_msg = str(e)
            # Mask any API keys that may appear in exception messages
            if self.api_key and self.api_key in error_msg:
                error_msg = error_msg.replace(self.api_key, mask_secret(self.api_key))
            logger.error("llm_call_failed", model=model, error=error_msg)
            return LLMResponse(
                content=f"Error calling LLM: {error_msg}",
                finish_reason="error",
            )
    
    def _parse_response(self, response: Any) -> LLMResponse:
        """Parse LiteLLM response into our standard format."""
        choice = response.choices[0]
        message = choice.message
        
        tool_calls = []
        if hasattr(message, "tool_calls") and message.tool_calls:
            for tc in message.tool_calls:
                # Parse arguments from JSON string if needed
                args = tc.function.arguments
                if isinstance(args, str):
                    args = json_repair.loads(args)
                
                tool_calls.append(ToolCallRequest(
                    id=tc.id,
                    name=tc.function.name,
                    arguments=args,
                ))
        
        usage = {}
        if hasattr(response, "usage") and response.usage:
            usage = {
                "prompt_tokens": response.usage.prompt_tokens,
                "completion_tokens": response.usage.completion_tokens,
                "total_tokens": response.usage.total_tokens,
            }
        
        reasoning_content = getattr(message, "reasoning_content", None) or None
        
        return LLMResponse(
            content=message.content,
            tool_calls=tool_calls,
            finish_reason=choice.finish_reason or "stop",
            usage=usage,
            reasoning_content=reasoning_content,
        )
    
    def get_default_model(self) -> str:
        """Get the default model."""
        return self.default_model
