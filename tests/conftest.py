"""Shared fixtures: mock LocalAI transport (httpx.MockTransport) + config helper."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import httpx
import pytest

from localai_steer.config import Config

FIXTURES = Path(__file__).parent / "fixtures"
AGENT = "Drunken_Pirate"
MESSAGE_ID = "1793336346624571020"


def load_fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def sse_transcript() -> str:
    """The recorded live SSE transcript (pinned frame format)."""
    return load_fixture("sse_stream.txt")


def make_config(**overrides: object) -> Config:
    base = {
        "agent": AGENT,
        "base_url": "http://localai.test:8080",
        "api_key": None,
        "timeout_s": 30.0,
        "verbose": False,
    }
    base.update(overrides)
    return Config(**base)  # type: ignore[arg-type]


def sse_handler(request: httpx.Request, *, transcript: str | None = None) -> httpx.Response:
    """SSE endpoint handler: streams the transcript (or a custom one)."""
    body = transcript if transcript is not None else sse_transcript()
    return httpx.Response(
        200,
        headers={"Content-Type": "text/event-stream", "Cache-Control": "no-cache"},
        content=body.encode("utf-8"),
    )


class RecordingTransport(httpx.MockTransport):
    """MockTransport that records every request it handles (``.requests``)."""

    def __init__(self, handler: Callable[[httpx.Request], httpx.Response]) -> None:
        super().__init__(handler)
        self.requests: list[httpx.Request] = []

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return super().handle_request(request)

    def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return super().handle_async_request(request)


def mock_transport(
    *,
    chat_status: int = 202,
    chat_body: dict | None = None,
    sse_transcript: str | None = None,
    expected_auth: str | None = None,
) -> RecordingTransport:
    """Build a recording MockTransport emulating the pinned LocalAI agent API.

    The handler is agent-agnostic (matches ``/api/agents/<any>/chat|sse``) so
    the same transport can serve both the configured agent and unknown-agent
    error tests.
    """
    chat_body = {"message_id": MESSAGE_ID,
    "status": "message_received"} if chat_body is None else chat_body

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/chat") and path.startswith("/api/agents/"):
            if expected_auth is not None:
                assert request.headers.get("authorization") == expected_auth
            return httpx.Response(chat_status, json=chat_body)
        if path.endswith("/sse") and path.startswith("/api/agents/"):
            return sse_handler(request, transcript=sse_transcript)
        return httpx.Response(404, json={"error": f"unexpected path {path}"})

    return RecordingTransport(handler)


@pytest.fixture
def cfg() -> Config:
    return make_config()


def stall_transport() -> RecordingTransport:
    """Recording transport whose SSE endpoint accepts the connection but never sends data.

    Used to exercise the client's deadline path (``asyncio.timeout``) without
    waiting on a real network.
    """
    import asyncio

    async def _stall_stream():
        # Accept the connection, then stall forever (deadline must fire).
        await asyncio.sleep(3600)
        yield b""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/chat") and request.url.path.startswith("/api/agents/"):
            return httpx.Response(
                202, json={"message_id": MESSAGE_ID, "status": "message_received"}
            )
        return httpx.Response(
            200,
            headers={"Content-Type": "text/event-stream"},
            content=_stall_stream(),
        )

    return RecordingTransport(handler)
