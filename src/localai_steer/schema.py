"""Pinned response models + tolerant parsers (contract pinned in docs/schema-pinned.md).

All parsers are tolerant: unknown/missing fields fall back to defaults, and
``tool_args`` is always kept as an opaque string (it may be empty, a JSON
string, or a Go-map repr — never assume a dict).
"""

from __future__ import annotations

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
