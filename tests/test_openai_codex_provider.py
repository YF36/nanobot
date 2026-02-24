from nanobot.providers.openai_codex_provider import _convert_messages


def test_convert_messages_accepts_system_text_blocks() -> None:
    system_blocks = [
        {"type": "text", "text": "STATIC PART", "cache_control": {"type": "ephemeral"}},
        {"type": "text", "text": "DYNAMIC PART"},
    ]
    messages = [
        {"role": "system", "content": system_blocks},
        {"role": "user", "content": "hello"},
    ]

    system_prompt, input_items = _convert_messages(messages)

    assert "STATIC PART" in system_prompt
    assert "DYNAMIC PART" in system_prompt
    assert input_items and input_items[0]["role"] == "user"
