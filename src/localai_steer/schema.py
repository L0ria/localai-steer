"""Pinned response models + tolerant parsers (contract pinned in docs/schema-pinned.md).

All parsers are tolerant: unknown/missing fields fall back to defaults, and
``tool_args`` is always kept as an opaque string (it may be empty, a JSON
string, or a Go-map repr — never assume a dict).
"""

from __future__ import annotations

import html
from dataclasses import dataclass


@dataclass(frozen=True)
class ChatAccepted:
    """``202`` response of ``POST /api/agents/<name>/chat``.

    ``message_id`` is the correlation key: the final result on the SSE stream
    is the ``json_message`` whose ``id == f"{message_id}-agent"``.
    """

    status: str
    message_id: str


@dataclass(frozen=True)
class AgentMessage:
    """A ``json_message`` SSE event (user echo or final agent result)."""

    id: str
    sender: str
    content: str
    timestamp: str = ""
    metadata: dict | None = None


@dataclass(frozen=True)
class StreamEvent:
    """A ``stream_event`` SSE event (live tokens / tool calls).

    ``tool_args`` is opaque: empty string, a JSON string, or a Go-map repr.
    """

    type: str
    content: str | None = None
    tool_name: str | None = None
    tool_args: str | None = None
    timestamp: str = ""


@dataclass(frozen=True)
class JsonError:
    """A ``json_error`` SSE event (failure / cancellation signal)."""

    error: str
    timestamp: str = ""


@dataclass(frozen=True)
class ObservableUpdate:
    """An ``observable_update`` SSE event (job/observable state).

    ``id`` is an integer job id — the only id-bearing field on any progress
    event (pinned §2, re-verified 2026-10-30). ``progress``/``completion``
    are optional: they were not present in the observed payloads.
    """

    id: int | None
    agent: str
    name: str
    icon: str = ""
    creation: dict | None = None
    progress: float | None = None
    completion: float | None = None


def _as_str(value: object, default: str = "") -> str:
    if isinstance(value, str):
        return value
    if value is None:
        return default
    return str(value)


def parse_chat_response(payload: dict) -> ChatAccepted:
    """Parse the ``202`` chat-accepted body.

    Tolerant: a missing ``message_id`` raises ``ValueError`` (nothing to
    correlate on), anything else degrades to defaults.
    """
    if not isinstance(payload, dict):
        raise ValueError(f"chat response is not a JSON object: {type(payload)!r}")
    message_id = _as_str(payload.get("message_id"))
    if not message_id:
        raise ValueError("chat response has no 'message_id' to correlate on")
    return ChatAccepted(status=_as_str(payload.get("status"), "message_received"), message_id=
    message_id)


def parse_agent_message(payload: dict) -> AgentMessage:
    """Parse a ``json_message`` payload into an :class:`AgentMessage`."""
    metadata = payload.get("metadata")
    return AgentMessage(
        id=_as_str(payload.get("id")),
        sender=_as_str(payload.get("sender")),
        content=_as_str(payload.get("content")),
        timestamp=_as_str(payload.get("timestamp")),
        metadata=metadata if isinstance(metadata, dict) else None,
    )


def parse_stream_event(payload: dict) -> StreamEvent:
    """Parse a ``stream_event`` payload; ``tool_args`` stays an opaque string."""
    tool_args = payload.get("tool_args")
    if tool_args is None:
        tool_args_str: str | None = None
    elif isinstance(tool_args, str):
        tool_args_str = tool_args
    else:  # tolerate a structured value by stringifying it (best-effort)
        tool_args_str = str(tool_args)
    return StreamEvent(
        type=_as_str(payload.get("type")),
        content=payload.get("content") if isinstance(payload.get("content"), str) else None,
        tool_name=payload.get("tool_name") if isinstance(payload.get("tool_name"), str) else None,
        tool_args=tool_args_str,
        timestamp=_as_str(payload.get("timestamp")),
    )


def parse_json_error(payload: dict) -> JsonError:
    """Parse a ``json_error`` payload."""
    error = payload.get("error")
    return JsonError(
        error=_as_str(error, "unknown agent error"),
        timestamp=_as_str(payload.get("timestamp")),
    )




def _as_int(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        try:
            return int(value.strip())
        except ValueError:
            return None
    return None


def _as_float(value: object) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value.strip())
        except ValueError:
            return None
    return None


def parse_observable_update(payload: dict) -> ObservableUpdate:
    """Parse an ``observable_update`` payload into an :class:`ObservableUpdate`.

    Tolerant: a missing ``id`` becomes ``None`` (the event is still usable);
    a non-dict ``creation`` becomes ``None``; non-numeric ``progress``/
    ``completion`` become ``None``.
    """
    creation = payload.get("creation")
    return ObservableUpdate(
        id=_as_int(payload.get("id")),
        agent=_as_str(payload.get("agent")),
        name=_as_str(payload.get("name")),
        icon=_as_str(payload.get("icon")),
        creation=creation if isinstance(creation, dict) else None,
        progress=_as_float(payload.get("progress")),
        completion=_as_float(payload.get("completion")),
    )


# Labels observed on the wire in ``status`` events (pinned §2).
STATUS_LABELS = ("Thinking", "Reasoning", "Action taken", "Parameters", "Result")


def unescape_status_text(raw: str) -> str:
    """HTML-unescape a ``status``/``hud`` payload (pinned §2: HTML-escaped text)."""
    return html.unescape(raw or "")


def split_status_label(text: str) -> tuple[str, str]:
    """Split a status line into ``(label, text)``.

    ``'Thinking: <html>...'`` -> ``('Thinking', '<html>...')``; a line with
    no known label -> ``('', text)``. Known labels: :data:`STATUS_LABELS`.
    """
    for label in STATUS_LABELS:
        prefix = label + ":"
        if text.startswith(prefix):
            return label, text[len(prefix):].strip()
    return "", text


# Event names (pinned catalog; unknown names pass through, see sse.py).
EVENT_JSON_MESSAGE = "json_message"
EVENT_JSON_MESSAGE_STATUS = "json_message_status"
EVENT_JSON_ERROR = "json_error"
EVENT_STREAM_EVENT = "stream_event"
EVENT_STATUS = "status"
EVENT_HUD = "hud"
EVENT_OBSERVABLE_UPDATE = "observable_update"

# Status values of ``json_message_status``.
STATUS_PROCESSING = "processing"
STATUS_COMPLETED = "completed"
