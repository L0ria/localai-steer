"""SSE frame parser: tolerant of comments, missing event, multi-line data, non-JSON."""

from conftest import sse_transcript
from localai_steer.sse import SSEEvent, parse_sse_text


def test_parse_recorded_live_transcript() -> None:
    events = parse_sse_text(sse_transcript())
    names = [e.event for e in events]
    # comment line produced no event; every real frame parsed
    assert names == [
        "hud",
        "json_message",
        "json_message_status",
        "stream_event",
        "stream_event",
        "status",
        "stream_event",
        "json_message",
        "json_message_status",
    ]
    final = events[7]
    assert final.event == "json_message"
    payload = final.json()
    assert payload is not None
    assert payload["id"] == "1793336346624571020-agent"
    assert payload["sender"] == "agent"


def test_missing_event_line_defaults_to_message() -> None:
    events = parse_sse_text("data: {\"x\": 1}\n\n")
    assert len(events) == 1
    assert events[0].event == "message"
    assert events[0].json() == {"x": 1}


def test_comment_lines_ignored() -> None:
    assert parse_sse_text(": hello\n: world\n\n") == []


def test_multiline_data_joined_with_newline() -> None:
    events = parse_sse_text("event: t\ndata: line1\ndata: line2\n\n")
    assert len(events) == 1
    assert events[0].data == "line1\nline2"


def test_non_json_payload_json_returns_none() -> None:
    ev = SSEEvent(event="status", data="Thinking: &lt;html&gt;stuff&lt;/html&gt;")
    assert ev.json() is None


def test_non_object_json_returns_none() -> None:
    ev = SSEEvent(event="x", data="[1, 2, 3]")
    assert ev.json() is None


def test_broken_json_returns_none() -> None:
    ev = SSEEvent(event="x", data="{not json")
    assert ev.json() is None


def test_data_without_space_after_colon() -> None:
    events = parse_sse_text("data:plain\n\n")
    assert len(events) == 1
    assert events[0].data == "plain"


def test_trailing_frame_without_blank_line() -> None:
    events = parse_sse_text("event: x\ndata: {\"a\": 1}")
    assert len(events) == 1
    assert events[0].json() == {"a": 1}
