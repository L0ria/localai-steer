"""Per-turn progress capture + event attribution (M1 core logic).

Pure logic: no I/O, no ``rich`` import, no network. The capture buffer
(:class:`TurnProgress`) is the foundation M2 folds into history
(issue #1 §5.1, Q3); the :class:`Correlator` implements the R1–R5
attribution rules pinned in ``docs/schema-pinned.md`` §8.

Correlation facts (pinned, re-verified 2026-10-30): the SSE channel is a
per-agent **broadcast** that **replays the previous run's tail** to new
subscribers (≥ 18 envelopes observed; bound approximate — treat as
unbounded for safety). The final result is correlated by
``id == "<message_id>-agent"`` (M0). Progress events carry **no**
``message_id``; ``observable_update`` carries an integer **job ``id``** —
the only id on any progress event; ``json_error`` carries **no** id, only
a ``timestamp`` (RFC3339/UTC).
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import UTC, datetime

from localai_steer.schema import (
    EVENT_HUD,
    EVENT_JSON_ERROR,
    EVENT_JSON_MESSAGE,
    EVENT_JSON_MESSAGE_STATUS,
    EVENT_OBSERVABLE_UPDATE,
    EVENT_STATUS,
    EVENT_STREAM_EVENT,
    STATUS_COMPLETED,
    parse_agent_message,
    parse_json_error,
    parse_observable_update,
    parse_stream_event,
    split_status_label,
    unescape_status_text,
)
from localai_steer.sse import SSEEvent

DEFAULT_CLOCK_SKEW_S = 2.0

# ``stream_event`` type values (pinned catalog).
STREAM_REASONING = "reasoning"
STREAM_CONTENT = "content"
STREAM_TOOL_CALL = "tool_call"
STREAM_DONE = "done"

_PHASE_WAITING = "waiting"
_PHASE_RUNNING = "running"
_PHASE_DONE = "done"
_PHASE_ERROR = "error"


@dataclass
class ToolCallProgress:
    """One observed ``stream_event type:"tool_call"``.

    ``args`` is opaque (pinned R2): empty string, a JSON string, or a
    Go-map repr — never parsed into a dict.
    """

    name: str
    args: str
    started_at: float  # time.monotonic()
    finished: bool = False


@dataclass
class StatusLine:
    """One ``status`` event after HTML-unescape + label split."""

    label: str  # "Thinking" | "Reasoning" | "Action taken" | "Parameters" | "Result" | ""
    text: str  # HTML-unescaped, label stripped
    at: float  # time.monotonic()


@dataclass
class TurnProgress:
    """Per-turn capture buffer — the M2 folding foundation (issue #1 §5.1, Q3).

    Accumulates delta fragments (the live stream sends *fragments*, never
    whole sentences), tool calls, status lines and ``observable_update``
    payloads for the active turn.
    """

    message_id: str
    started_at: float  # time.monotonic()
    reasoning_chunks: list[str] = field(default_factory=list)
    content_chunks: list[str] = field(default_factory=list)
    tool_calls: list[ToolCallProgress] = field(default_factory=list)
    status_lines: list[StatusLine] = field(default_factory=list)
    observable_updates: list[dict] = field(default_factory=list)
    job_id: int | None = None  # learned from observable_update.id (only id-bearing progress event)
    done_seen: bool = False  # stream_event type "done" (pinned catalog; non-terminating)
    error: str | None = None  # set by an attributed json_error

    # ------------------------------------------------------------- capture

    def add_reasoning(self, fragment: str) -> None:
        if fragment:
            self.reasoning_chunks.append(fragment)

    def add_content(self, fragment: str) -> None:
        if fragment:
            self.content_chunks.append(fragment)

    def add_tool_call(self, name: str, args: str) -> None:
        """Record a tool call; dedupe consecutive identical ``(name, args)``.

        The broadcast may deliver the same tool_call envelope more than once
        (replay/bridge); a *different* args value is a distinct call.
        """
        if self.tool_calls and (
            self.tool_calls[-1].name == name and self.tool_calls[-1].args == args
        ):
            return
        self.tool_calls.append(ToolCallProgress(name=name, args=args, started_at=time.monotonic()))

    def add_status(self, label: str, text: str) -> None:
        self.status_lines.append(StatusLine(label=label, text=text, at=time.monotonic()))

    def add_observable(self, payload: dict) -> None:
        """Record an ``observable_update`` payload; learn the job id (first-seen wins)."""
        self.observable_updates.append(payload)
        if self.job_id is None:
            parsed = parse_observable_update(payload)
            if parsed.id is not None:
                self.job_id = parsed.id

    def mark_done(self) -> None:
        """``stream_event type:"done"``: stop-accumulation marker (non-terminating, Q3)."""
        self.done_seen = True
        for call in self.tool_calls:
            if not call.finished:
                call.finished = True

    def mark_error(self, text: str) -> None:
        self.error = text

    # ------------------------------------------------- M2-facing views

    @property
    def partial_reasoning(self) -> str:
        return "".join(self.reasoning_chunks)

    @property
    def partial_assistant_text(self) -> str:
        return "".join(self.content_chunks)

    def in_flight_tool_calls(self) -> list[ToolCallProgress]:
        """Tool calls not yet marked finished (the ``done`` marker finishes all)."""
        return [call for call in self.tool_calls if not call.finished]

    def to_history_note(self) -> str | None:
        """Synthetic assistant note for M2's re-prompt history.

        Partial assistant text + in-flight tool calls (name + opaque args) +
        the last status lines. ``None`` when nothing was captured.
        """
        parts: list[str] = []
        text = self.partial_assistant_text.strip()
        if text:
            parts.append(text)
        calls = self.in_flight_tool_calls()
        if calls:
            rendered = ", ".join(
                f"{call.name}({call.args})" if call.args else call.name for call in calls
            )
            parts.append(f"[in-flight tool calls: {rendered}]")
        if self.status_lines:
            last = self.status_lines[-1]
            parts.append(f"[last status: {last.label + ': ' if last.label else ''}{last.text}]")
        if not parts:
            return None
        return " ".join(parts)


class Correlator:
    """Decides which turn an SSE event belongs to (rules R1–R5, schema-pinned §8).

    The broadcast is per-agent and progress events carry no ``message_id``,
    so attribution is by the active-turn window plus id-bearing fields. The
    CLI runs one turn at a time (issue #1 §6.8).

    Holds **both** references (coordinator R4 clarification):

    - ``turn_started_utc`` — wall-clock UTC captured at construction (i.e.
      right after the ``202`` POST). Used for the R4 ``json_error``
      timestamp comparison: the event's RFC3339/UTC ``timestamp`` is parsed
      and compared against this wall-clock moment.
    - ``turn_started_monotonic`` — monotonic reference for the R2
      arrival-order window (events accepted while the turn is active).
    """

    def __init__(
        self,
        turn_started_utc: datetime,
        turn_started_monotonic: float,
        *,
        clock_skew_s: float = DEFAULT_CLOCK_SKEW_S,
    ) -> None:
        if turn_started_utc.tzinfo is None:
            turn_started_utc = turn_started_utc.replace(tzinfo=UTC)
        self.turn_started_utc = turn_started_utc.astimezone(UTC)
        self.turn_started_monotonic = turn_started_monotonic
        self.clock_skew_s = clock_skew_s
        self._saw_turn_marker = False

    # ------------------------------------------------------- R1 markers

    def saw_turn_marker(self) -> bool:
        """True once a ``json_message``/``json_message_status`` event for THIS
        turn's ``message_id`` has been observed (first-seen heuristic)."""
        return self._saw_turn_marker

    def note_turn_marker(self) -> None:
        self._saw_turn_marker = True

    # ------------------------------------------------------ R4 stale rule

    def is_stale_error(self, error_timestamp_rfc3339: str | None) -> bool:
        """R4: reject a replayed ``json_error`` from a *previous* run.

        True when the timestamp (RFC3339/UTC) parses to a moment **older
        than the turn start (wall-clock UTC) minus ``clock_skew_s``**.
        Missing/unparseable timestamps return ``False`` (accept — M0
        behavior preserved).
        """
        if not error_timestamp_rfc3339:
            return False
        try:
            stamp = datetime.fromisoformat(error_timestamp_rfc3339.replace("Z", "+00:00"))
        except ValueError:
            return False
        if stamp.tzinfo is None:
            stamp = stamp.replace(tzinfo=UTC)
        stamp = stamp.astimezone(UTC)
        return stamp.timestamp() < self.turn_started_utc.timestamp() - self.clock_skew_s

    # ------------------------------------------------------ R3 id gating

    def accept_observable(self, job_id: int | None, progress_job_id: int | None) -> bool:
        """R3: unknown job ids are accepted (first one wins); once a job id is
        known, later ``observable_update`` events with a *different* id
        (another job's state) are dropped."""
        if job_id is None:
            return True
        if progress_job_id is None:
            return True
        return job_id == progress_job_id


def apply_event(progress: TurnProgress, event: SSEEvent, correlator: Correlator) -> None:
    """Route one parsed SSE event into the turn's capture buffer (R1–R5).

    - ``json_message`` / ``json_message_status``: turn markers (R1)
    - ``stream_event``: reasoning/content chunks, tool_call (deduped), done marker
    - ``status``: unescape + label split -> StatusLine
    - ``hud``: heartbeat — not captured (footer display only, Q2)
    - ``observable_update``: parse -> add_observable (job id learned, R3)
    - ``json_error``: attributed -> mark_error (stale-replay rejection is the
      client's job, R4)
    - unknown events: ignored (R5, M0 tolerance preserved)
    """
    name = event.event
    if name in (EVENT_JSON_MESSAGE, EVENT_JSON_MESSAGE_STATUS):
        payload = event.json()
        if payload is None:
            return
        if name == EVENT_JSON_MESSAGE:
            message = parse_agent_message(payload)
            is_marker = message.id == f"{progress.message_id}-agent" and message.sender == "agent"
        else:
            is_marker = payload.get("status") == STATUS_COMPLETED
        if is_marker:
            correlator.note_turn_marker()
        return
    if name == EVENT_STREAM_EVENT:
        payload = event.json()
        if payload is None:
            return
        stream = parse_stream_event(payload)
        if stream.type == STREAM_REASONING:
            progress.add_reasoning(stream.content or "")
        elif stream.type == STREAM_CONTENT:
            progress.add_content(stream.content or "")
        elif stream.type == STREAM_TOOL_CALL:
            progress.add_tool_call(stream.tool_name or "", stream.tool_args or "")
        elif stream.type == STREAM_DONE:
            progress.mark_done()
        # other stream_event types: ignored (tolerant)
        return
    if name == EVENT_STATUS:
        label, text = split_status_label(unescape_status_text(event.data))
        progress.add_status(label, text)
        return
    if name == EVENT_HUD:
        return  # heartbeat; not captured (Q2)
    if name == EVENT_OBSERVABLE_UPDATE:
        payload = event.json()
        if payload is not None:
            progress.add_observable(payload)
        return
    if name == EVENT_JSON_ERROR:
        payload = event.json() or {}
        progress.mark_error(parse_json_error(payload).error)
        return
    # R5: unknown event names are ignored (debug-logged by the caller)
