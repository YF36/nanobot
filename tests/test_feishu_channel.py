from nanobot.bus.queue import MessageBus
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
    assert channel._should_patch_progress_card(key, "hello" + ("x" * 300), force=False) is True


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
