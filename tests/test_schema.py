"""Tolerant schema parsers: pinned models + edge cases."""

import pytest

from localai_steer.schema import (
    parse_agent_message,
    parse_chat_response,
    parse_json_error,
    parse_stream_event,
)


def test_parse_chat_response_pinned_shape() -> None:
    accepted = parse_chat_response({"message_id": "1793336346624571020",
    "status": "message_received"})
    assert accepted.message_id == "1793336346624571020"
    assert accepted.status == "message_received"


def test_parse_chat_response_missing_message_id_raises() -> None:
    with pytest.raises(ValueError, match="message_id"):
        parse_chat_response({"status": "message_received"})


def test_parse_chat_response_non_dict_raises() -> None:
    with pytest.raises(ValueError):
        parse_chat_response("nope")  # type: ignore[arg-type]


def test_parse_agent_message_tolerant_defaults() -> None:
    msg = parse_agent_message({"content": "hi"})
    assert msg.content == "hi"
    assert msg.id == ""
    assert msg.sender == ""
    assert msg.metadata is None


def test_parse_agent_message_metadata_kept_when_dict() -> None:
    msg = parse_agent_message(
        {"id": "1-agent", "sender": "agent", "content": "x", "metadata": {"a": 1}}
    )
    assert msg.metadata == {"a": 1}


def test_parse_stream_event_tool_call_empty_args() -> None:
    ev = parse_stream_event(
        {
            "timestamp": "2026-10-30T04:27:58Z",
            "tool_args": "",
            "tool_name": "execute_command",
            "type": "tool_call",
        }
    )
    assert ev.type == "tool_call"
    assert ev.tool_name == "execute_command"
    assert ev.tool_args == ""
    assert ev.content is None


def test_parse_stream_event_tool_args_json_string_stays_opaque() -> None:
    ev = parse_stream_event({"type": "tool_call", "tool_name": "t", "tool_args": '{"cmd": "ls"}'})
    assert ev.tool_args == '{"cmd": "ls"}'  # string, not parsed into a dict


def test_parse_stream_event_tool_args_structured_tolerated() -> None:
    ev = parse_stream_event({"type": "tool_call", "tool_name": "t", "tool_args": {"cmd": "ls"}})
    assert isinstance(ev.tool_args, str)
    assert "ls" in ev.tool_args


def test_parse_stream_event_reasoning_content() -> None:
    ev = parse_stream_event({"type": "reasoning", "content": "thinking..."})
    assert ev.type == "reasoning"
    assert ev.content == "thinking..."
    assert ev.tool_name is None


def test_parse_json_error() -> None:
    err = parse_json_error({"error": "boom", "timestamp": "2026-10-30T00:00:00Z"})
    assert err.error == "boom"
    assert err.timestamp == "2026-10-30T00:00:00Z"


def test_parse_json_error_missing_text_gets_default() -> None:
    assert parse_json_error({}).error == "unknown agent error"
