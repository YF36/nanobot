from types import SimpleNamespace
import json

import pytest

from nanobot.bus.queue import MessageBus
from nanobot.bus.events import OutboundMessage
from nanobot.channels.feishu import FeishuChannel
from nanobot.config.schema import FeishuConfig


def test_feishu_progress_message_cache_is_bounded() -> None:
    channel = FeishuChannel(FeishuConfig(), MessageBus())
    cap = channel._MAX_PROGRESS_TRACKED_MESSAGES

    for i in range(cap + 20):
        key = ("chat_1", f"mid_{i}")
        channel._remember_progress_message_id(key, f"resp_{i}")

    assert len(channel._progress_message_ids) == cap
    assert ("chat_1", "mid_0") not in channel._progress_message_ids
    assert ("chat_1", f"mid_{cap + 19}") in channel._progress_message_ids


def test_feishu_progress_message_cache_refreshes_existing_key_order() -> None:
    channel = FeishuChannel(FeishuConfig(), MessageBus())
    cap = channel._MAX_PROGRESS_TRACKED_MESSAGES

    k0 = ("chat_1", "mid_0")
    k1 = ("chat_1", "mid_1")
    k2 = ("chat_1", "mid_2")
    channel._remember_progress_message_id(k0, "resp_0")
    channel._remember_progress_message_id(k1, "resp_1")
    channel._remember_progress_message_id(k2, "resp_2")
    channel._remember_progress_message_id(k0, "resp_0b")

    for i in range(3, cap + 1):
        channel._remember_progress_message_id(("chat_1", f"mid_{i}"), f"resp_{i}")

    assert len(channel._progress_message_ids) == cap
    assert k1 not in channel._progress_message_ids
    assert k0 in channel._progress_message_ids
    assert channel._progress_message_ids[k0] == "resp_0b"


def test_feishu_progress_patch_throttle_decision() -> None:
    channel = FeishuChannel(FeishuConfig(), MessageBus())
    key = ("chat_1", "mid_1")

    channel._mark_progress_card_patched(key, "hello")

    assert channel._should_patch_progress_card(key, "hello world", force=True) is True
    assert channel._should_patch_progress_card(key, "hello world", force=False) is False

    # Simulate interval elapsed; now min-char threshold decides.
    last_at, last_len = channel._progress_patch_state[key]
    channel._progress_patch_state[key] = (last_at - 1.0, last_len)
    assert channel._should_patch_progress_card(key, "hello world", force=False) is False
    assert channel._should_patch_progress_card(key, "hello" + ("x" * 30), force=False) is True


def test_feishu_progress_state_is_cleared_on_cache_eviction() -> None:
    channel = FeishuChannel(FeishuConfig(), MessageBus())
    cap = channel._MAX_PROGRESS_TRACKED_MESSAGES
    first_key = ("chat_1", "mid_0")

    channel._remember_progress_message_id(first_key, "resp_0")
    channel._mark_progress_card_patched(first_key, "alpha")
    assert first_key in channel._progress_patch_state

    for i in range(1, cap + 10):
        key = ("chat_1", f"mid_{i}")
        channel._remember_progress_message_id(key, f"resp_{i}")

    assert first_key not in channel._progress_message_ids
    assert first_key not in channel._progress_patch_state


class _FakeUpdateResp:
    def __init__(self, success: bool, code: int, msg: str = "") -> None:
        self._success = success
        self.code = code
        self.msg = msg

    def success(self) -> bool:
        return self._success


def test_feishu_update_message_retries_on_rate_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    channel = FeishuChannel(FeishuConfig(), MessageBus())
    if not channel.supports_progress_message_editing:
        pytest.skip("Feishu SDK update API not available in this environment")

    calls = {"n": 0}

    def _update(_request):
        calls["n"] += 1
        if calls["n"] < 3:
            return _FakeUpdateResp(False, 230020, "frequency limit")
        return _FakeUpdateResp(True, 0, "ok")

    channel._client = SimpleNamespace(
        im=SimpleNamespace(
            v1=SimpleNamespace(
                message=SimpleNamespace(update=_update),
            )
        )
    )
    monkeypatch.setattr("nanobot.channels.feishu.time.sleep", lambda _s: None)

    assert channel._update_message_sync("om_xxx", "interactive", "{\"config\":{}}") is True
    assert calls["n"] == 3


def test_feishu_update_message_does_not_retry_non_retryable_code(monkeypatch: pytest.MonkeyPatch) -> None:
    channel = FeishuChannel(FeishuConfig(), MessageBus())
    if not channel.supports_progress_message_editing:
        pytest.skip("Feishu SDK update API not available in this environment")

    calls = {"n": 0}

    def _update(_request):
        calls["n"] += 1
        return _FakeUpdateResp(False, 230025, "content too long")

    channel._client = SimpleNamespace(
        im=SimpleNamespace(
            v1=SimpleNamespace(
                message=SimpleNamespace(update=_update),
            )
        )
    )
    monkeypatch.setattr("nanobot.channels.feishu.time.sleep", lambda _s: None)

    assert channel._update_message_sync("om_xxx", "interactive", "{\"config\":{}}") is False
    assert calls["n"] == 1


@pytest.mark.asyncio
async def test_feishu_progress_update_failure_does_not_create_new_card(monkeypatch: pytest.MonkeyPatch) -> None:
    channel = FeishuChannel(FeishuConfig(), MessageBus())
    channel._client = object()
    key = ("chat_1", "origin_mid_1")
    channel._remember_progress_message_id(key, "resp_mid_1")

    sent_calls = {"n": 0}
    update_calls = {"n": 0}

    def _fake_update(_message_id: str, _msg_type: str, _content: str) -> bool:
        update_calls["n"] += 1
        return False

    def _fake_send(*_args, **_kwargs):
        sent_calls["n"] += 1
        return "new_mid_should_not_happen"

    monkeypatch.setattr(channel, "_update_message_sync", _fake_update)
    monkeypatch.setattr(channel, "_send_message_sync", _fake_send)

    await channel.send(OutboundMessage(
        channel="feishu",
        chat_id="chat_1",
        content="stream chunk",
        metadata={
            "_progress_edit": True,
            "message_id": "origin_mid_1",
        },
    ))

    assert update_calls["n"] == 1
    assert sent_calls["n"] == 0


@pytest.mark.asyncio
async def test_feishu_progress_updates_use_accumulated_content(monkeypatch: pytest.MonkeyPatch) -> None:
    channel = FeishuChannel(FeishuConfig(), MessageBus())
    channel._client = object()
    key = ("chat_1", "origin_mid_2")
    channel._remember_progress_message_id(key, "resp_mid_2")

    patched_contents: list[str] = []

    def _fake_update(_message_id: str, _msg_type: str, content: str) -> bool:
        payload = json.loads(content)
        patched_contents.append(payload["elements"][0]["content"])
        return True

    monkeypatch.setattr(channel, "_should_patch_progress_card", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(channel, "_update_message_sync", _fake_update)

    await channel.send(OutboundMessage(
        channel="feishu",
        chat_id="chat_1",
        content="Hello ",
        metadata={
            "_progress_edit": True,
            "message_id": "origin_mid_2",
        },
    ))
    await channel.send(OutboundMessage(
        channel="feishu",
        chat_id="chat_1",
        content="world",
        metadata={
            "_progress_edit": True,
            "message_id": "origin_mid_2",
        },
    ))

    assert patched_contents == ["Hello ", "Hello world"]
