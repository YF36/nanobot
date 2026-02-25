from types import SimpleNamespace

import pytest

from nanobot.agent.tools.base import ToolExecutionResult
from nanobot.agent.turn_runner import TurnRunner, _session_tool_details


def test_session_tool_details_wraps_compact_data_with_version() -> None:
    details = {
        "op": "edit_file",
        "path": "/tmp/sample.txt",
        "requested_path": "sample.txt",
        "first_changed_line": 5,
        "replacement_count": 1,
        "diff_truncated": False,
        "diff_preview": "...large diff omitted...",
        "extra": "ignored",
    }

    result = _session_tool_details(details)

    assert result["schema_version"] == 1
    assert result["tool"] == "edit_file"
    assert result["data"] == {
        "op": "edit_file",
        "path": "/tmp/sample.txt",
        "requested_path": "sample.txt",
        "first_changed_line": 5,
        "replacement_count": 1,
        "diff_truncated": False,
    }
    assert "diff_preview" not in result["data"]


def test_session_tool_details_returns_empty_for_no_supported_keys() -> None:
    assert _session_tool_details({"diff_preview": "only preview"}) == {}
    assert _session_tool_details({}) == {}


class _FakeContext:
    def add_assistant_message(self, messages, content, tool_calls=None, reasoning_content=None):
        msg = {"role": "assistant", "content": content}
        if tool_calls:
            msg["tool_calls"] = tool_calls
        messages.append(msg)
        return messages

    def add_tool_result(self, messages, tool_call_id, tool_name, result, metadata=None):
        msg = {"role": "tool", "tool_call_id": tool_call_id, "name": tool_name, "content": result}
        if metadata:
            msg["_tool_details"] = metadata
        messages.append(msg)
        return messages


class _FakeTools:
    def get_definitions(self):
        return []

    async def execute_result(self, name, arguments):
        return ToolExecutionResult(text="ok", details={"op": "exec", "exit_code": 0})


class _FakeProvider:
    def __init__(self):
        self.calls = 0

    async def chat(self, **kwargs):
        self.calls += 1
        if self.calls == 1:
            tool_call = SimpleNamespace(id="call_1", name="exec", arguments={"command": "echo hello"})
            return SimpleNamespace(
                has_tool_calls=True,
                content="I will run a command",
                tool_calls=[tool_call],
                reasoning_content=None,
            )
        return SimpleNamespace(
            has_tool_calls=False,
            content="Done",
            tool_calls=[],
            reasoning_content=None,
        )


class _RetryThenSuccessProvider:
    def __init__(self):
        self.calls = 0

    async def chat(self, **kwargs):
        self.calls += 1
        if self.calls == 1:
            raise RuntimeError("temporary upstream failure")
        return SimpleNamespace(
            has_tool_calls=False,
            content="Recovered",
            tool_calls=[],
            reasoning_content=None,
            finish_reason="stop",
        )


class _OverflowThenSuccessProvider:
    def __init__(self):
        self.calls = 0

    async def chat(self, **kwargs):
        self.calls += 1
        if self.calls == 1:
            return SimpleNamespace(
                has_tool_calls=False,
                content="Error calling LLM: exceeds the model's maximum context length",
                tool_calls=[],
                reasoning_content=None,
                finish_reason="error",
            )
        return SimpleNamespace(
            has_tool_calls=False,
            content="After compaction",
            tool_calls=[],
            reasoning_content=None,
            finish_reason="stop",
        )


class _FatalExceptionProvider:
    def __init__(self):
        self.calls = 0

    async def chat(self, **kwargs):
        self.calls += 1
        raise RuntimeError("Invalid API key provided")


class _FatalErrorFinishProvider:
    def __init__(self):
        self.calls = 0

    async def chat(self, **kwargs):
        self.calls += 1
        return SimpleNamespace(
            has_tool_calls=False,
            content="Authentication failed: invalid api key",
            tool_calls=[],
            reasoning_content=None,
            finish_reason="error",
        )


class _RetryableErrorFinishThenSuccessProvider:
    def __init__(self):
        self.calls = 0

    async def chat(self, **kwargs):
        self.calls += 1
        if self.calls == 1:
            return SimpleNamespace(
                has_tool_calls=False,
                content="Service unavailable, please try again later",
                tool_calls=[],
                reasoning_content=None,
                finish_reason="error",
            )
        return SimpleNamespace(
            has_tool_calls=False,
            content="Recovered after finish_reason error",
            tool_calls=[],
            reasoning_content=None,
            finish_reason="stop",
        )


@pytest.mark.asyncio
async def test_turn_runner_emits_minimal_events_and_keeps_progress() -> None:
    runner = TurnRunner(
        provider=_FakeProvider(),
        tools=_FakeTools(),
        context_builder=_FakeContext(),
        model="test",
        temperature=0.0,
        max_tokens=256,
        max_iterations=5,
        guard_loop_messages=lambda m, i: (m, i),
        strip_think=lambda s: s,
        tool_hint=lambda calls: f"Using {len(calls)} tool(s)",
    )

    progress_calls: list[tuple[str, bool]] = []
    events: list[dict] = []

    async def _on_progress(content: str, *, tool_hint: bool = False) -> None:
        progress_calls.append((content, tool_hint))

    async def _on_event(event: dict) -> None:
        events.append(event)

    final_content, tools_used, messages = await runner.run(
        [{"role": "user", "content": "do it"}],
        on_progress=_on_progress,
        on_event=_on_event,
    )

    assert final_content == "Done"
    assert tools_used == ["exec"]
    assert [e["type"] for e in events] == ["turn_start", "tool_start", "tool_end", "turn_end"]
    assert [e["sequence"] for e in events] == [1, 2, 3, 4]
    assert all(e.get("namespace") == "nanobot.turn" for e in events)
    assert all(e.get("version") == 1 for e in events)
    assert all(isinstance(e.get("timestamp_ms"), int) and e["timestamp_ms"] > 0 for e in events)
    assert all(e.get("source") == "turn_runner" for e in events)
    turn_ids = {e.get("turn_id") for e in events}
    assert len(turn_ids) == 1
    only_turn_id = next(iter(turn_ids))
    assert isinstance(only_turn_id, str) and only_turn_id.startswith("turn_")
    assert events[2]["detail_op"] == "exec"
    assert events[2]["has_details"] is True
    assert any(tool_hint for _, tool_hint in progress_calls)
    tool_msgs = [m for m in messages if m.get("role") == "tool"]
    assert tool_msgs and tool_msgs[0]["_tool_details"]["tool"] == "exec"


@pytest.mark.asyncio
async def test_turn_runner_interrupts_after_tool_for_followup() -> None:
    runner = TurnRunner(
        provider=_FakeProvider(),
        tools=_FakeTools(),
        context_builder=_FakeContext(),
        model="test",
        temperature=0.0,
        max_tokens=256,
        max_iterations=5,
        guard_loop_messages=lambda m, i: (m, i),
        strip_think=lambda s: s,
        tool_hint=lambda calls: f"Using {len(calls)} tool(s)",
    )

    checks = 0
    events: list[dict] = []

    async def _steer_check() -> dict:
        nonlocal checks
        checks += 1
        return {
            "interrupt": True,
            "reason": "pending_followup",
            "pending_followup_count": 2,
            "next_followup_preview": "second request",
        }

    async def _on_event(event: dict) -> None:
        events.append(event)

    final_content, tools_used, messages = await runner.run(
        [{"role": "user", "content": "do it"}],
        on_event=_on_event,
        should_interrupt_after_tool=_steer_check,
    )

    assert checks == 1
    assert tools_used == ["exec"]
    assert "paused this task" in (final_content or "")
    assert "second request" in (final_content or "")
    assert [e["type"] for e in events] == ["turn_start", "tool_start", "tool_end", "turn_end"]
    assert events[-1]["interrupted_for_followup"] is True
    assert events[-1]["interruption_reason"] == "pending_followup"
    assert events[-1]["interrupted_after_tool"] == "exec"
    assert events[-1]["pending_followup_count"] == 2
    assert events[-1]["next_followup_preview"] == "second request"
    assert any(m.get("role") == "tool" for m in messages)


@pytest.mark.asyncio
async def test_turn_runner_retries_transient_provider_exception() -> None:
    runner = TurnRunner(
        provider=_RetryThenSuccessProvider(),
        tools=_FakeTools(),
        context_builder=_FakeContext(),
        model="test",
        temperature=0.0,
        max_tokens=256,
        max_iterations=2,
        guard_loop_messages=lambda m, i: (m, i),
        strip_think=lambda s: s,
        tool_hint=lambda calls: f"Using {len(calls)} tool(s)",
    )

    events: list[dict] = []

    async def _on_event(event: dict) -> None:
        events.append(event)

    final_content, tools_used, messages = await runner.run(
        [{"role": "user", "content": "retry please"}],
        on_event=_on_event,
    )

    assert final_content == "Recovered"
    assert tools_used == []
    assert messages[-1]["content"] == "Recovered"
    assert events[-1]["llm_retry_count"] == 1
    assert events[-1]["llm_exception_retry_count"] == 1
    assert events[-1].get("llm_error_finish_retry_count", 0) == 0


@pytest.mark.asyncio
async def test_turn_runner_retries_after_context_overflow_with_compaction() -> None:
    provider = _OverflowThenSuccessProvider()
    runner = TurnRunner(
        provider=provider,
        tools=_FakeTools(),
        context_builder=_FakeContext(),
        model="test",
        temperature=0.0,
        max_tokens=256,
        max_iterations=2,
        guard_loop_messages=lambda m, i: (m[1:], max(0, i - 1)) if len(m) > 1 else (m, i),
        strip_think=lambda s: s,
        tool_hint=lambda calls: f"Using {len(calls)} tool(s)",
    )

    events: list[dict] = []

    async def _on_event(event: dict) -> None:
        events.append(event)

    final_content, tools_used, messages = await runner.run(
        [
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "old"},
            {"role": "user", "content": "current"},
        ],
        on_event=_on_event,
    )

    assert provider.calls == 2
    assert final_content == "After compaction"
    assert tools_used == []
    assert messages[-1]["content"] == "After compaction"
    assert events[-1]["llm_overflow_compaction_retries"] == 1
    assert events[-1]["llm_error_finish_overflow_count"] == 1
    assert events[-1].get("llm_error_finish_retryable_count", 0) == 0
    assert events[-1].get("llm_error_finish_fatal_count", 0) == 0


@pytest.mark.asyncio
async def test_turn_runner_does_not_retry_fatal_provider_exception() -> None:
    provider = _FatalExceptionProvider()
    runner = TurnRunner(
        provider=provider,
        tools=_FakeTools(),
        context_builder=_FakeContext(),
        model="test",
        temperature=0.0,
        max_tokens=256,
        max_iterations=2,
        guard_loop_messages=lambda m, i: (m, i),
        strip_think=lambda s: s,
        tool_hint=lambda calls: f"Using {len(calls)} tool(s)",
    )

    with pytest.raises(RuntimeError, match="Invalid API key"):
        await runner.run([{"role": "user", "content": "hello"}])

    assert provider.calls == 1


@pytest.mark.asyncio
async def test_turn_runner_does_not_retry_fatal_finish_reason_error() -> None:
    provider = _FatalErrorFinishProvider()
    runner = TurnRunner(
        provider=provider,
        tools=_FakeTools(),
        context_builder=_FakeContext(),
        model="test",
        temperature=0.0,
        max_tokens=256,
        max_iterations=2,
        guard_loop_messages=lambda m, i: (m, i),
        strip_think=lambda s: s,
        tool_hint=lambda calls: f"Using {len(calls)} tool(s)",
    )
    events: list[dict] = []

    async def _on_event(event: dict) -> None:
        events.append(event)

    final_content, tools_used, messages = await runner.run(
        [{"role": "user", "content": "hello"}],
        on_event=_on_event,
    )

    assert provider.calls == 1
    assert final_content == "Authentication failed: invalid api key"
    assert tools_used == []
    assert messages[-1]["content"] == "Authentication failed: invalid api key"
    assert events[-1].get("llm_retry_count", 0) == 0
    assert events[-1].get("llm_error_finish_retry_count", 0) == 0
    assert events[-1]["llm_error_finish_fatal_count"] == 1
    assert events[-1].get("llm_error_finish_retryable_count", 0) == 0
    assert events[-1].get("llm_error_finish_overflow_count", 0) == 0


@pytest.mark.asyncio
async def test_turn_runner_retries_retryable_finish_reason_error() -> None:
    provider = _RetryableErrorFinishThenSuccessProvider()
    runner = TurnRunner(
        provider=provider,
        tools=_FakeTools(),
        context_builder=_FakeContext(),
        model="test",
        temperature=0.0,
        max_tokens=256,
        max_iterations=2,
        guard_loop_messages=lambda m, i: (m, i),
        strip_think=lambda s: s,
        tool_hint=lambda calls: f"Using {len(calls)} tool(s)",
    )
    events: list[dict] = []

    async def _on_event(event: dict) -> None:
        events.append(event)

    final_content, tools_used, _ = await runner.run(
        [{"role": "user", "content": "hello"}],
        on_event=_on_event,
    )

    assert provider.calls == 2
    assert final_content == "Recovered after finish_reason error"
    assert tools_used == []
    assert events[-1]["llm_retry_count"] == 1
    assert events[-1]["llm_error_finish_retry_count"] == 1
    assert events[-1]["llm_error_finish_retryable_count"] == 1
    assert events[-1].get("llm_error_finish_fatal_count", 0) == 0
    assert events[-1].get("llm_error_finish_overflow_count", 0) == 0
