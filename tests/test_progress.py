"""TurnProgress capture + Correlator R1–R5 rules (M1 core logic, issue #4 §4.1/§5)."""

from __future__ import annotations

import time
from datetime import UTC, datetime, timedelta

from conftest import MESSAGE_ID, load_fixture
from localai_steer.progress import Correlator, TurnProgress, apply_event
from localai_steer.sse import SSEEvent, parse_sse_text


def make_progress() -> TurnProgress:
    return TurnProgress(message_id=MESSAGE_ID, started_at=time.monotonic())


def make_correlator(
    seconds_ago: float = 0.0, *, clock_skew_s: float = 2.0
) -> Correlator:
    """A correlator whose turn started ``seconds_ago`` (wall-clock UTC)."""
    started = datetime.now(UTC) - timedelta(seconds=seconds_ago)
    return Correlator(
        turn_started_utc=started,
        turn_started_monotonic=time.monotonic(),
        clock_skew_s=clock_skew_s,
    )


def fixture_events(name: str) -> list[SSEEvent]:
    return parse_sse_text(load_fixture(name))


# ------------------------------------------------------------- TurnProgress


def test_chunk_accumulation_order() -> None:
    progress = make_progress()
    progress.add_reasoning("The user wants ")
    progress.add_reasoning("me to list the cwd.")
    progress.add_content("Ahoy, ")
    progress.add_content("matey!")
    assert progress.partial_reasoning == "The user wants me to list the cwd."
    assert progress.partial_assistant_text == "Ahoy, matey!"


def test_empty_fragments_are_ignored() -> None:
    progress = make_progress()
    progress.add_reasoning("")
    progress.add_content("")
    assert progress.reasoning_chunks == []
    assert progress.content_chunks == []
    assert progress.partial_reasoning == ""


def test_tool_call_dedupe_consecutive_identical() -> None:
    progress = make_progress()
    progress.add_tool_call("execute_command", "")
    progress.add_tool_call("execute_command", "")  # replay duplicate -> deduped
    progress.add_tool_call("execute_command", '{"cmd": "date"}')  # different args -> kept
    assert len(progress.tool_calls) == 2
    assert progress.tool_calls[0].args == ""
    assert progress.tool_calls[1].args == '{"cmd": "date"}'


def test_in_flight_tool_calls_and_done_marker() -> None:
    progress = make_progress()
    progress.add_tool_call("t1", "a")
    progress.add_tool_call("t2", "b")
    assert len(progress.in_flight_tool_calls()) == 2
    progress.mark_done()
    assert progress.done_seen is True
    assert progress.in_flight_tool_calls() == []
    assert all(call.finished for call in progress.tool_calls)


def test_add_observable_learns_job_id_first_wins() -> None:
    progress = make_progress()
    progress.add_observable({"id": 12, "agent": "A", "name": "job"})
    progress.add_observable({"id": 99, "agent": "A", "name": "job"})  # later id ignored
    assert progress.job_id == 12
    assert len(progress.observable_updates) == 2


def test_add_observable_without_id_keeps_none() -> None:
    progress = make_progress()
    progress.add_observable({"agent": "A", "name": "job"})
    assert progress.job_id is None


def test_mark_error() -> None:
    progress = make_progress()
    assert progress.error is None
    progress.mark_error("tool exploded")
    assert progress.error == "tool exploded"


def test_to_history_note_none_when_empty() -> None:
    assert make_progress().to_history_note() is None


def test_to_history_note_contents() -> None:
    progress = make_progress()
    progress.add_content("Ahoy, matey! ")
    progress.add_tool_call("execute_command", '{"cmd": "ls"}')
    progress.add_status("Action taken", "ran `ls`")
    note = progress.to_history_note()
    assert note is not None
    assert "Ahoy, matey!" in note
    assert "execute_command" in note
    assert '{"cmd": "ls"}' in note
    assert "Action taken: ran `ls`" in note


# ---------------------------------------------------------------- Correlator


def test_r4_stale_old_utc_timestamp_rejected() -> None:
    # A previous run's error, timestamped well before this turn's 202 POST.
    assert make_correlator().is_stale_error("2026-10-20T00:00:00Z") is True


def test_r4_current_recent_utc_timestamp_accepted() -> None:
    # A current run's error: timestamp at/after the turn start -> raise (M0).
    now = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    assert make_correlator().is_stale_error(now) is False


def test_r4_missing_timestamp_accepted_m0_compatible() -> None:
    correlator = make_correlator()
    assert correlator.is_stale_error(None) is False
    assert correlator.is_stale_error("") is False


def test_r4_unparseable_timestamp_accepted_m0_compatible() -> None:
    assert make_correlator().is_stale_error("not-a-timestamp") is False


def test_r4_within_skew_grace_is_accepted() -> None:
    # 1 s before the turn start is inside the 2 s clock-skew grace -> accept.
    just_before = (datetime.now(UTC) - timedelta(seconds=1)).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )
    assert make_correlator(clock_skew_s=2.0).is_stale_error(just_before) is False
    # 5 s before is outside the grace -> stale.
    well_before = (datetime.now(UTC) - timedelta(seconds=5)).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )
    assert make_correlator(clock_skew_s=2.0).is_stale_error(well_before) is True


def test_r4_naive_timestamp_treated_as_utc() -> None:
    assert make_correlator().is_stale_error("2026-10-20T00:00:00") is True


def test_r4_offset_timestamp_normalized_to_utc() -> None:
    # 02:00+02:00 == 00:00Z on 2026-10-20 -> stale.
    assert make_correlator().is_stale_error("2026-10-20T02:00:00+02:00") is True


def test_accept_observable_first_id_wins_mismatch_dropped() -> None:
    correlator = make_correlator()
    assert correlator.accept_observable(None, None) is True
    assert correlator.accept_observable(12, None) is True  # unknown id: accept
    assert correlator.accept_observable(12, 12) is True
    assert correlator.accept_observable(99, 12) is False  # different job: drop
    assert correlator.accept_observable(None, 12) is True


def test_saw_turn_marker_gating() -> None:
    correlator = make_correlator()
    assert correlator.saw_turn_marker() is False
    correlator.note_turn_marker()
    assert correlator.saw_turn_marker() is True


def test_correlator_holds_both_references() -> None:
    correlator = make_correlator()
    assert correlator.turn_started_utc.tzinfo is not None
    assert isinstance(correlator.turn_started_monotonic, float)
    assert correlator.clock_skew_s == 2.0


# -------------------------------------------------------------- apply_event


def test_apply_event_multistep_fixture_table() -> None:
    progress = make_progress()
    correlator = make_correlator()
    events = fixture_events("sse_multistep_toolcalls.txt")
    for event in events:
        apply_event(progress, event, correlator)
    # reasoning fragments joined in order
    assert progress.partial_reasoning == "The user wants me to list the cwd."
    # both tool calls captured (empty args + JSON-string args)
    assert [call.name for call in progress.tool_calls] == ["execute_command"] * 2
    assert progress.tool_calls[0].args == ""
    assert progress.tool_calls[1].args == '{"cmd": "date"}'
    # all three status labels captured, unescaped
    assert [line.label for line in progress.status_lines] == [
        "Action taken",
        "Parameters",
        "Result",
    ]
    assert "ran `ls`" in progress.status_lines[0].text
    # job id learned from observable_update (only id-bearing progress event)
    assert progress.job_id == 12
    # content fragments joined
    assert progress.partial_assistant_text == "Ahoy, matey! The date is printed."
    # R1: the final json_message for this turn was a marker
    assert correlator.saw_turn_marker() is True


def test_apply_event_unknown_event_ignored() -> None:
    progress = make_progress()
    correlator = make_correlator()
    apply_event(progress, SSEEvent(event="mystery_event", data="{}"), correlator)
    assert progress.reasoning_chunks == []
    assert progress.tool_calls == []
    assert progress.status_lines == []


def test_apply_event_hud_not_captured() -> None:
    progress = make_progress()
    correlator = make_correlator()
    apply_event(
        progress,
        SSEEvent(event="hud", data="=====...<br>NowDoing: <br>=====..."),
        correlator,
    )
    assert progress.status_lines == []
    assert progress.observable_updates == []


def test_apply_event_done_marker_non_terminating() -> None:
    progress = make_progress()
    correlator = make_correlator()
    events = fixture_events("sse_done_event.txt")
    for event in events:
        apply_event(progress, event, correlator)
    assert progress.done_seen is True
    assert progress.partial_reasoning == "thinking..."
    assert progress.partial_assistant_text == "Hello."


def test_apply_event_json_error_marks_error() -> None:
    progress = make_progress()
    correlator = make_correlator()
    events = fixture_events("sse_stale_error_replay.txt")
    for event in events:
        apply_event(progress, event, correlator)
    assert progress.error == "previous run tool exploded"
