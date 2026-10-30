"""LocalAIClient: request construction, error mapping, SSE correlation, timeout."""

import json

import pytest

from conftest import MESSAGE_ID, make_config, mock_transport, stall_transport
from localai_steer.client import (
    AgentNotFound,
    AuthError,
    LocalAIClient,
    LocalAIError,
    ServerError,
    TurnTimeout,
)


async def test_chat_sends_exact_body_and_returns_message_id() -> None:
    transport = mock_transport()
    async with LocalAIClient(make_config(), transport=transport) as client:
        message_id = await client.chat("Say a short greeting.")
    assert message_id == MESSAGE_ID
    request = transport.requests[0]
    assert request.method == "POST"
    assert request.url.path == "/api/agents/Drunken_Pirate/chat"
    assert request.headers["content-type"].startswith("application/json")
    body = json.loads(request.content)
    assert body == {"message": "Say a short greeting.", "history": []}


async def test_auth_header_present_when_key_configured() -> None:
    transport = mock_transport(expected_auth="Bearer sekret")
    async with LocalAIClient(make_config(api_key="sekret"), transport=transport) as client:
        await client.chat("hi")
    assert transport.requests[0].headers["authorization"] == "Bearer sekret"


async def test_agent_not_found_maps_404() -> None:
    transport = mock_transport(chat_status=404, chat_body={"error": "agent not found: X"})
    async with LocalAIClient(make_config(), transport=transport) as client:
        with pytest.raises(AgentNotFound) as excinfo:
            await client.chat("hi")
    assert excinfo.value.status_code == 404
    assert "agent not found" in excinfo.value.detail


async def test_bad_request_maps_400() -> None:
    transport = mock_transport(chat_status=400, chat_body={"error": "Message cannot be empty"})
    async with LocalAIClient(make_config(), transport=transport) as client:
        with pytest.raises(LocalAIError) as excinfo:
            await client.chat("hi")
    assert excinfo.value.status_code == 400
    assert not isinstance(excinfo.value, (AgentNotFound, AuthError, ServerError))


async def test_auth_error_maps_401() -> None:
    transport = mock_transport(chat_status=401, chat_body={"error": "unauthorized"})
    async with LocalAIClient(make_config(), transport=transport) as client:
        with pytest.raises(AuthError) as excinfo:
            await client.chat("hi")
    assert excinfo.value.status_code == 401


async def test_server_error_maps_500() -> None:
    transport = mock_transport(chat_status=500, chat_body={"error": "internal error: boom"})
    async with LocalAIClient(make_config(), transport=transport) as client:
        with pytest.raises(ServerError) as excinfo:
            await client.chat("hi")
    assert excinfo.value.status_code == 500
    assert "boom" in excinfo.value.detail


async def test_chat_202_without_message_id_raises() -> None:
    transport = mock_transport(chat_status=202, chat_body={"status": "message_received"})
    async with LocalAIClient(make_config(), transport=transport) as client:
        with pytest.raises(LocalAIError) as excinfo:
            await client.chat("hi")
    assert "message_id" in str(excinfo.value)


async def test_wait_for_result_returns_final_content() -> None:
    transport = mock_transport()
    async with LocalAIClient(make_config(), transport=transport) as client:
        result = await client.wait_for_result(MESSAGE_ID)
    assert result == "Ahoy, matey! Welcome aboard."


async def test_wait_for_result_ignores_other_message_ids() -> None:
    other_mid = "9999999999999999999"
    transcript = (
        "event: json_message\n"
        f'data: {{"content": "WRONG", "id": "{other_mid}-agent", "sender": "agent"}}\n\n'
        "event: json_message\n"
        f'data: {{"content": "RIGHT", "id": "{MESSAGE_ID}-agent", "sender": "agent"}}\n\n'
    )
    transport = mock_transport(sse_transcript=transcript)
    async with LocalAIClient(make_config(), transport=transport) as client:
        result = await client.wait_for_result(MESSAGE_ID)
    assert result == "RIGHT"


async def test_wait_for_result_ignores_user_echo() -> None:
    transcript = (
        "event: json_message\n"
        f'data: {{"content": "echo", "id": "{MESSAGE_ID}-user", "sender": "user"}}\n\n'
        "event: json_message\n"
        f'data: {{"content": "FINAL", "id": "{MESSAGE_ID}-agent", "sender": "agent"}}\n\n'
    )
    transport = mock_transport(sse_transcript=transcript)
    async with LocalAIClient(make_config(), transport=transport) as client:
        assert await client.wait_for_result(MESSAGE_ID) == "FINAL"


async def test_wait_for_result_raises_on_json_error() -> None:
    transcript = (
        "event: json_message_status\ndata: {\"status\": \"processing\"}\n\n"
        "event: json_error\n"
        "data: {\"error\": \"tool exploded\", \"timestamp\": \"2026-10-30T00:00:00Z\"}\n\n"
    )
    transport = mock_transport(sse_transcript=transcript)
    async with LocalAIClient(make_config(), transport=transport) as client:
        with pytest.raises(LocalAIError) as excinfo:
            await client.wait_for_result(MESSAGE_ID)
    assert "tool exploded" in str(excinfo.value)


async def test_wait_for_result_times_out_while_stream_open() -> None:
    # SSE connection stays open but no correlated result ever arrives: the
    # deadline (asyncio.timeout) must fire and raise TurnTimeout.
    transport = stall_transport()
    async with LocalAIClient(make_config(timeout_s=0.3), transport=transport) as client:
        with pytest.raises(TurnTimeout) as excinfo:
            await client.wait_for_result(MESSAGE_ID)
    assert excinfo.value.message_id == MESSAGE_ID
    assert "timed out" in str(excinfo.value)


async def test_wait_for_result_stream_closes_without_result() -> None:
    # Stream replays an unrelated event then closes: a clear error, not a hang.
    transcript = (
        "event: json_message\n"
        'data: {"content": "stale", "id": "123-user", "sender": "user"}\n\n'
    )
    transport = mock_transport(sse_transcript=transcript)
    async with LocalAIClient(make_config(), transport=transport) as client:
        with pytest.raises(LocalAIError) as excinfo:
            await client.wait_for_result(MESSAGE_ID)
    assert "ended before" in str(excinfo.value)


async def test_wait_for_result_completed_but_no_result_is_an_error() -> None:
    transcript = "event: json_message_status\ndata: {\"status\": \"completed\"}\n\n"
    transport = mock_transport(sse_transcript=transcript)
    async with LocalAIClient(make_config(), transport=transport) as client:
        with pytest.raises(LocalAIError) as excinfo:
            await client.wait_for_result(MESSAGE_ID)
    assert "completed" in str(excinfo.value)


async def test_stream_events_sends_accept_header() -> None:
    transport = mock_transport()
    client = LocalAIClient(make_config(), transport=transport)
    async with client, client.stream_events() as events:
        async for _ in events:
            break
    sse_request = transport.requests[-1]
    assert sse_request.headers["accept"] == "text/event-stream"
