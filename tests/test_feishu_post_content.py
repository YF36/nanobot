from nanobot.channels.feishu import _extract_post_content, _extract_post_text


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

