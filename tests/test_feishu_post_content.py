import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from nanobot.bus.queue import MessageBus
from nanobot.channels.feishu import FeishuChannel, _extract_post_content, _extract_post_text
from nanobot.config.schema import FeishuConfig


def test_extract_post_content_returns_text_and_image_keys_direct_format() -> None:
    content = {
        "title": "Weekly Update",
        "content": [[
            {"tag": "text", "text": "hello"},
            {"tag": "img", "image_key": "img_123"},
            {"tag": "at", "user_name": "alice"},
        ]]
    }

    text, images = _extract_post_content(content)

    assert text == "Weekly Update hello @alice"
    assert images == ["img_123"]


def test_extract_post_content_supports_localized_format_images_only() -> None:
    content = {
        "zh_cn": {
            "title": "",
            "content": [[{"tag": "img", "image_key": "img_a"}, {"tag": "img", "image_key": "img_b"}]],
        }
    }

    text, images = _extract_post_content(content)

    assert text == ""
    assert images == ["img_a", "img_b"]


def test_extract_post_text_legacy_wrapper_ignores_images() -> None:
    content = {"content": [[{"tag": "text", "text": "hi"}, {"tag": "img", "image_key": "img_x"}]]}
    assert _extract_post_text(content) == "hi"


@pytest.mark.asyncio
async def test_on_message_post_downloads_embedded_images_and_forwards_media() -> None:
    channel = FeishuChannel(FeishuConfig(enabled=True), MessageBus())
    channel._add_reaction = AsyncMock()
    channel._download_and_save_media = AsyncMock(side_effect=[
        ("/tmp/img1.jpg", "[image: img1.jpg]"),
        ("/tmp/img2.jpg", "[image: img2.jpg]"),
    ])
    channel._handle_message = AsyncMock()

    post_content = {
        "title": "Report",
        "content": [[
            {"tag": "text", "text": "summary"},
            {"tag": "img", "image_key": "img_1"},
            {"tag": "img", "image_key": "img_2"},
        ]]
    }
    data = SimpleNamespace(
        event=SimpleNamespace(
            message=SimpleNamespace(
                message_id="m1",
                chat_id="chat1",
                chat_type="p2p",
                message_type="post",
                content=json.dumps(post_content),
            ),
            sender=SimpleNamespace(
                sender_type="user",
                sender_id=SimpleNamespace(open_id="u_open"),
            ),
        )
    )

    await channel._on_message(data)

    assert channel._download_and_save_media.await_count == 2
    channel._handle_message.assert_awaited_once()
    kwargs = channel._handle_message.await_args.kwargs
    assert kwargs["sender_id"] == "u_open"
    assert kwargs["chat_id"] == "u_open"  # p2p routes to sender
    assert kwargs["media"] == ["/tmp/img1.jpg", "/tmp/img2.jpg"]
    assert "Report summary" in kwargs["content"]
    assert "[image: img1.jpg]" in kwargs["content"]
    assert "[image: img2.jpg]" in kwargs["content"]
