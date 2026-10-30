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


class ClosingByteStream(httpx.AsyncByteStream):
    """An async byte stream that records whether ``aclose()`` was called.

    Used by the teardown tests: the client's ``stream_events()`` context
    manager must ``aclose()`` the SSE response on every exit path.
    """

    def __init__(self, data: bytes) -> None:
        self._data = data
        self.closed = False

    async def __aiter__(self):  # type: ignore[async-coroutine]
        yield self._data

    async def aclose(self) -> None:
        self.closed = True


def _default_handler(request: httpx.Request) -> httpx.Response:
    return httpx.Response(500, json={"error": "no handler configured"})


class RecordingTransport(httpx.MockTransport):
    """MockTransport that records every request it handles (``.requests``).

    ``last_stream`` is the :class:`ClosingByteStream` backing the most recent
    SSE response (teardown probe: assert it was ``aclose()``d).
    """

    def __init__(self, handler: Callable[[httpx.Request], httpx.Response]) -> None:
        super().__init__(handler)
        self.requests: list[httpx.Request] = []
        self._streams: list[ClosingByteStream] = []

    @property
    def last_stream(self) -> ClosingByteStream | None:
        return self._streams[-1] if self._streams else None

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
    transcript: str | None = None,
    expected_auth: str | None = None,
) -> RecordingTransport:
    """Build a recording MockTransport emulating the pinned LocalAI agent API.

    The handler is agent-agnostic (matches ``/api/agents/<any>/chat|sse``) so
    the same transport can serve both the configured agent and unknown-agent
    error tests. The SSE body is served through a :class:`ClosingByteStream`
    (exposed as ``transport.last_stream``) so teardown tests can assert the
    stream was closed (issue #4 §7/§8).

    ``transcript``: the SSE transcript to serve (defaults to the recorded
    live fixture).
    """
    chat_body = {"message_id": MESSAGE_ID,
    "status": "message_received"} if chat_body is None else chat_body
    streams: list[ClosingByteStream] = []

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/chat") and path.startswith("/api/agents/"):
            if expected_auth is not None:
                assert request.headers.get("authorization") == expected_auth
            return httpx.Response(chat_status, json=chat_body)
        if path.endswith("/sse") and path.startswith("/api/agents/"):
            body = transcript if transcript is not None else sse_transcript()
            stream = ClosingByteStream(body.encode("utf-8"))
            streams.append(stream)
            return httpx.Response(
                200,
                headers={"Content-Type": "text/event-stream", "Cache-Control": "no-cache"},
                stream=stream,
            )
        return httpx.Response(404, json={"error": f"unexpected path {path}"})

    transport = RecordingTransport(handler)
    transport._streams = streams  # type: ignore[attr-defined]
    return transport


@pytest.fixture
def cfg() -> Config:
    return make_config()


class StallingClosingStream(ClosingByteStream):
    """A :class:`ClosingByteStream` that stalls forever (deadline must fire)."""

    async def __aiter__(self):  # type: ignore[async-coroutine]
        import asyncio

        # Accept the connection, then stall forever (deadline must fire).
        await asyncio.sleep(3600)
        yield b""


def stall_transport() -> RecordingTransport:
    """Recording transport whose SSE endpoint accepts the connection but never sends data.

    Used to exercise the client's deadline path (``asyncio.timeout``) without
    waiting on a real network. The SSE stream is a :class:`ClosingByteStream`
    so the teardown probe (``transport.last_stream.closed``) works here too.
    """
    stream = StallingClosingStream(b"")

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/chat") and request.url.path.startswith("/api/agents/"):
            return httpx.Response(
                202, json={"message_id": MESSAGE_ID, "status": "message_received"}
            )
        return httpx.Response(
            200,
            headers={"Content-Type": "text/event-stream"},
            stream=stream,
        )

    transport = RecordingTransport(handler)
    transport._streams = [stream]  # type: ignore[attr-defined]
    return transport
