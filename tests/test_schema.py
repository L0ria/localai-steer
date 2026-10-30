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


# ------------------------------------------------------------------ M1 schema


def test_parse_observable_update_pinned_shape() -> None:
    from localai_steer.schema import parse_observable_update

    update = parse_observable_update(
        {
            "id": 12,
            "agent": "Drunken_Pirate",
            "name": "job",
            "icon": "agent",
            "creation": {"chat_completion_message": {"role": "user", "content": "hi"}},
        }
    )
    assert update.id == 12
    assert update.agent == "Drunken_Pirate"
    assert update.name == "job"
    assert update.icon == "agent"
    assert update.creation is not None
    assert update.progress is None
    assert update.completion is None


def test_parse_observable_update_missing_id_is_none() -> None:
    from localai_steer.schema import parse_observable_update

    update = parse_observable_update({"agent": "A", "name": "job"})
    assert update.id is None
    assert update.creation is None


def test_parse_observable_update_non_dict_creation_is_none() -> None:
    from localai_steer.schema import parse_observable_update

    update = parse_observable_update({"id": "not-an-int", "agent": "A", "creation": "nope"})
    assert update.id is None
    assert update.creation is None


def test_parse_observable_update_progress_completion_optional() -> None:
    from localai_steer.schema import parse_observable_update

    update = parse_observable_update({"id": 1, "agent": "A", "progress": 0.5, "completion": 1})
    assert update.progress == 0.5
    assert update.completion == 1.0


def test_unescape_status_text() -> None:
    from localai_steer.schema import unescape_status_text

    assert unescape_status_text("&lt;b&gt;bold&lt;/b&gt; &amp; more") == "<b>bold</b> & more"
    assert unescape_status_text("") == ""
    assert unescape_status_text("plain text") == "plain text"


def test_split_status_label_all_five_labels() -> None:
    from localai_steer.schema import split_status_label, unescape_status_text

    # Pipeline: unescape first (apply_event), then split the label.
    assert split_status_label(unescape_status_text("Thinking: &lt;html&gt;x&lt;/html&gt;")) == (
        "Thinking", "<html>x</html>"
    )
    assert split_status_label("Reasoning: r") == ("Reasoning", "r")
    assert split_status_label("Action taken: ran `ls`") == ("Action taken", "ran `ls`")
    assert split_status_label("Parameters: p") == ("Parameters", "p")
    assert split_status_label("Result: done") == ("Result", "done")


def test_split_status_label_no_label() -> None:
    from localai_steer.schema import split_status_label

    assert split_status_label("no label here") == ("", "no label here")
    assert split_status_label("") == ("", "")
