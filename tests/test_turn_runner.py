from nanobot.agent.turn_runner import _session_tool_details


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
