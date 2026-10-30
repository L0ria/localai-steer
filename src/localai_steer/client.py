"""Async LocalAI agent client (httpx).

Wire contract (pinned live against LocalAI ``v4.11.0-68-gc03acb064``; see
``docs/schema-pinned.md``):

- ``POST /api/agents/<name>/chat`` returns **202 immediately** with
  ``{"message_id": ..., "status": "message_received"}``. The run executes in a
  detached server goroutine; the final result is **not** in the POST body.
- ``GET /api/agents/<name>/sse`` is a per-agent **broadcast** that also
  replays the last ~10 envelopes to a new subscriber. The final result is the
  ``json_message`` whose ``id == f"{message_id}-agent"`` and
  ``sender == "agent"``.
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Self
from urllib.parse import quote

import httpx

from localai_steer.config import Config
from localai_steer.logging import get_logger
from localai_steer.progress import Correlator
from localai_steer.schema import (
    EVENT_JSON_ERROR,
    EVENT_JSON_MESSAGE,
    EVENT_JSON_MESSAGE_STATUS,
    STATUS_COMPLETED,
    parse_agent_message,
    parse_chat_response,
    parse_json_error,
)
from localai_steer.sse import SSEEvent, iter_sse

_CONNECT_TIMEOUT_S = 10.0
_DETAIL_MAX_CHARS = 200


class LocalAIError(Exception):
    """Base error for LocalAI client failures."""

    def __init__(
        self,
        message: str = "",
        *,
        status_code: int | None = None,
        detail: str | None = None,
    ) -> None:
        super().__init__(message or detail or "LocalAI error")
        self.status_code = status_code
        self.detail = detail if detail is not None else (message or "LocalAI error")


class AgentNotFound(LocalAIError):
    """The agent does not exist on this LocalAI instance (HTTP 404)."""


class AuthError(LocalAIError):
    """Authentication/authorization rejected (HTTP 401/403)."""


class ServerError(LocalAIError):
    """LocalAI reported a server-side fault (HTTP 5xx)."""


class TurnTimeout(LocalAIError):
    """The final agent result did not arrive within the configured timeout."""

    def __init__(self, message_id: str, timeout_s: float) -> None:
        super().__init__(
            f"timed out after {timeout_s:g}s waiting for agent result (message_id={message_id})"
        )
        self.message_id = message_id
        self.timeout_s = timeout_s


def _extract_error_detail(body_text: str) -> str:
    """Pull a human-readable detail out of an error body (``{"error": ...}`` or raw text)."""
    text = (body_text or "").strip()
    if not text:
        return "no error detail"
    if text.startswith("{"):
        try:
            payload = json.loads(text)
            if isinstance(payload, dict) and isinstance(payload.get("error"), str):
                return payload["error"][:_DETAIL_MAX_CHARS]
        except (json.JSONDecodeError, ValueError):
            pass
    return text[:_DETAIL_MAX_CHARS]


def _map_http_error(status_code: int, body_text: str) -> LocalAIError:
    detail = _extract_error_detail(body_text)
    if status_code == 404:
        return AgentNotFound(detail, status_code=404, detail=detail)
    if status_code in (401, 403):
        return AuthError(detail, status_code=status_code, detail=detail)
    if 500 <= status_code < 600:
        return ServerError(detail, status_code=status_code, detail=detail)
    return LocalAIError(detail, status_code=status_code, detail=detail)


class LocalAIClient:
    """Thin async client for the LocalAI agent chat + SSE endpoints.

    Owns an ``httpx.AsyncClient``; use as an async context manager. A custom
    ``transport`` may be injected for tests (``httpx.MockTransport``).
    """

    def __init__(
        self,
        cfg: Config,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._cfg = cfg
        self._log = get_logger("client")
        agent_path = quote(cfg.agent, safe="")
        self._chat_path = f"/api/agents/{agent_path}/chat"
        self._sse_path = f"/api/agents/{agent_path}/sse"
        self._client = httpx.AsyncClient(
            base_url=cfg.base_url.rstrip("/"),
            headers=cfg.auth_headers,
            timeout=httpx.Timeout(cfg.timeout_s, connect=_CONNECT_TIMEOUT_S),
            transport=transport,
        )

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.close()

    async def close(self) -> None:
        await self._client.aclose()

    # ------------------------------------------------------------------ chat

    async def chat(self, message: str, history: list[dict] | None = None) -> str:
        """Post one turn to the agent; returns the ``message_id`` correlation key.

        The POST returns ``202`` immediately — the result arrives on the SSE
        stream, correlated by this ``message_id``.
        """
        payload = {"message": message, "history": history if history is not None else []}
        self._log.debug("POST %s body=%r", self._chat_path, payload)
        response = await self._client.post(self._chat_path, json=payload)
        if response.status_code != 202:
            raise _map_http_error(response.status_code, response.text)
        try:
            accepted = parse_chat_response(response.json())
        except ValueError as exc:
            raise LocalAIError(str(exc), status_code=202) from exc
        self._log.debug("chat accepted: message_id=%s status=%s", accepted.message_id,
        accepted.status)
        return accepted.message_id

    # ------------------------------------------------------------------- sse

    @asynccontextmanager
    async def stream_events(self) -> AsyncIterator[AsyncIterator[SSEEvent]]:
        """Open the agent SSE broadcast; yields an async iterator of events.

        The stream is always closed on exit.
        """
        headers = {"Accept": "text/event-stream"}
        self._log.debug("GET %s (SSE)", self._sse_path)
        # No per-chunk read timeout on the stream: the overall turn deadline is
        # enforced by wait_for_result's asyncio.timeout, and LocalAI keeps the
        # connection alive with ~1s `hud` heartbeats.
        request = self._client.build_request("GET", self._sse_path, headers=headers)
        request.extensions["timeout"] = {
            "connect": _CONNECT_TIMEOUT_S,
            "read": None,
            "write": self._cfg.timeout_s,
            "pool": _CONNECT_TIMEOUT_S,
        }
        response = await self._client.send(request, stream=True)
        try:
            if response.status_code != 200:
                body = (await response.aread()).decode("utf-8", errors="replace")
                raise _map_http_error(response.status_code, body)
            yield iter_sse(response)
        finally:
            await response.aclose()

    # ------------------------------------------------------- wait_for_result

    async def wait_for_result(
        self,
        message_id: str,
        *,
        timeout: float | None = None,
        on_event: Callable[[SSEEvent], None] | None = None,
    ) -> str:
        """Consume the SSE stream until the correlated final agent result arrives.

        M0 behavior plus an optional ``on_event`` callback (M1): every parsed
        SSE event is forwarded to it in arrival order — but never after the
        final result has been found.

        Event order per SSE frame (pinned, docs/schema-pinned.md §8):
          1. ``json_message`` for THIS ``message_id`` -> ``correlator.note_turn_marker()``
          2. ``on_event(event)`` (progress capture / rendering)
          3. ``json_error`` -> rejected (logged, not raised) when the
             correlator's R4 rule deems it a stale replay; otherwise raised
             (M0 behavior)
          4. correlated final result -> return content (stream closed in ``finally``)

        The :class:`Correlator` is constructed at call time — the call
        happens right after the ``202`` POST, so ``turn_started_utc`` is the
        wall-clock UTC turn-start reference for the R4 stale-``json_error``
        rule (coordinator clarification).

        Returns the final ``content``. Raises :class:`TurnTimeout` when the
        deadline passes, :class:`LocalAIError` on an attributed ``json_error``
        or a dead stream, and mapped errors for HTTP failures.
        """
        deadline = self._cfg.timeout_s if timeout is None else timeout
        target_id = f"{message_id}-agent"
        # Turn-start reference: wall-clock UTC (R4) + monotonic (R2 window).
        correlator = Correlator(
            turn_started_utc=datetime.now(UTC),
            turn_started_monotonic=time.monotonic(),
        )
        saw_completed = False
        try:
            async with asyncio.timeout(deadline):
                async with self.stream_events() as events:
                    async for event in events:
                        result = self._handle_event(event, target_id, correlator)
                        if result is not None:
                            return result
                        if on_event is not None:
                            on_event(event)
                        saw_completed = saw_completed or (
                            event.event == EVENT_JSON_MESSAGE_STATUS
                            and (event.json() or {}).get("status") == STATUS_COMPLETED
                        )
            raise LocalAIError(
                "SSE stream ended before the agent result arrived"
                + (" (agent reported 'completed' but no correlated result)"
                   if saw_completed else ""),
                detail=f"message_id={message_id}",
            )
        except TimeoutError:
            raise TurnTimeout(message_id, deadline) from None

    def _handle_event(
        self, event: SSEEvent, target_id: str, correlator: Correlator
    ) -> str | None:
        """Process one SSE event; returns the final content when correlated.

        R1: ``json_message``/``json_message_status`` for this turn are markers.
        R4: a stale-replayed ``json_error`` (timestamp older than the turn
        start minus the clock-skew grace) is logged and ignored; an
        attributed one is raised (M0 behavior).
        """
        if event.event == EVENT_JSON_MESSAGE:
            payload = event.json()
            if payload is None:
                self._log.debug("skipping non-JSON json_message payload")
                return None
            message = parse_agent_message(payload)
            if message.id == target_id and message.sender == "agent":
                self._log.debug("final result correlated: id=%s", message.id)
                return message.content
            self._log.debug("ignoring json_message id=%s sender=%s", message.id, message.sender)
            return None
        if event.event == EVENT_JSON_ERROR:
            payload = event.json() or {}
            error = parse_json_error(payload)
            if correlator.is_stale_error(error.timestamp):
                self._log.warning(
                    "ignoring stale replayed json_error (timestamp=%s, turn started %s)",
                    error.timestamp or "<missing>",
                    correlator.turn_started_utc.isoformat(),
                )
                return None
            raise LocalAIError(error.error, detail=f"message_id target={target_id}")
        # stream_event / status / hud / observable_update / unknown: progress
        # (forwarded to on_event by the caller) — ignored here in M0/M1.
        self._log.debug("progress event %r", event.event)
        return None
