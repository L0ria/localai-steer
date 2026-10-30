"""Tolerant Server-Sent Events frame parser.

LocalAI's agent SSE channel sends ``event: <name>\\ndata: <payload>\\n\\n`` frames.
Payloads are single-line JSON for most events, but ``status``/``hud`` carry
HTML-escaped plain text — so the parser must never assume ``data`` is JSON.

The parser is deliberately tolerant:
- missing ``event:`` line  -> default event name ``message``
- comment lines (``: ...``) -> ignored
- multi-line ``data:``      -> joined with ``\\n``
- empty frames              -> skipped
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from dataclasses import dataclass

import httpx

DEFAULT_EVENT_NAME = "message"


@dataclass
class SSEEvent:
    """One parsed SSE frame: the event name plus the raw payload line(s)."""

    event: str
    data: str

    def json(self) -> dict | None:
        """Best-effort JSON decode; ``None`` when the payload is not a JSON object.

        ``status``/``hud`` events carry HTML-escaped text, so ``None`` here is
        normal, not an error.
        """
        text = self.data.strip()
        if not text.startswith("{"):
            return None
        try:
            payload = json.loads(text)
        except (json.JSONDecodeError, ValueError):
            return None
        return payload if isinstance(payload, dict) else None


def _parse_block(lines: list[str]) -> SSEEvent | None:
    """Parse one blank-line-delimited block of raw SSE lines into an event."""
    event_name = DEFAULT_EVENT_NAME
    data_lines: list[str] = []
    for raw in lines:
        line = raw.rstrip("\r")
        if not line or line.startswith(":"):
            continue  # blank line or comment
        if line.startswith("event:"):
            value = line[len("event:"):].strip()
            if value:
                event_name = value
        elif line.startswith("data:"):
            value = line[len("data:"):]
            # SSE spec: strip at most one leading space
            if value.startswith(" "):
                value = value[1:]
            data_lines.append(value)
        # other fields (id:, retry:) are irrelevant for M0 — ignored
    if not data_lines:
        return None
    return SSEEvent(event=event_name, data="\n".join(data_lines))


def parse_sse_text(text: str) -> list[SSEEvent]:
    """Parse a complete SSE transcript (bytes or str) into a list of events.

    Useful for tests and for replaying recorded fixtures.
    """
    if isinstance(text, (bytes, bytearray)):
        text = bytes(text).decode("utf-8", errors="replace")
    events: list[SSEEvent] = []
    block: list[str] = []
    for line in text.split("\n"):
        if line == "":
            event = _parse_block(block)
            if event is not None:
                events.append(event)
            block = []
        else:
            block.append(line)
    if block:  # tolerate a trailing frame without the final blank line
        event = _parse_block(block)
        if event is not None:
            events.append(event)
    return events


async def iter_sse(response: httpx.Response) -> AsyncIterator[SSEEvent]:
    """Async iterator over SSE events from a streaming ``httpx.Response``.

    Accumulates ``event:``/``data:`` lines and yields one :class:`SSEEvent`
    per blank-line-delimited frame, in arrival order.
    """
    block: list[str] = []
    async for raw_line in response.aiter_lines():
        line = raw_line.rstrip("\r")
        if line == "":
            event = _parse_block(block)
            if event is not None:
                yield event
            block = []
        else:
            block.append(line)
    if block:
        event = _parse_block(block)
        if event is not None:
            yield event
