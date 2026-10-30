"""CLI end-to-end (mock transport): exit codes + stdout/stderr contracts."""

from __future__ import annotations

import pytest
from click.testing import CliRunner

import localai_steer.cli as cli
from conftest import MESSAGE_ID, mock_transport, stall_transport
from localai_steer.client import LocalAIClient

# Module-level holder so the patched client can grab the current mock transport.
_transport_holder: list[object] = []


def _install(monkeypatch: pytest.MonkeyPatch, transport: object) -> None:
    """Route ``cli.LocalAIClient`` through ``transport`` for the next invoke."""
    _transport_holder.clear()
    _transport_holder.append(transport)

    class _Patched(LocalAIClient):
        def __init__(self, cfg, transport=None):  # type: ignore[no-untyped-def]
            super().__init__(cfg, transport=
            transport if transport is not None else _transport_holder[0])

    monkeypatch.setattr(cli, "LocalAIClient", _Patched)


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


def test_one_shot_success_prints_result_and_exits_0(
    runner: CliRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install(monkeypatch, mock_transport())

    result = runner.invoke(cli.main, ["--agent", "Drunken_Pirate", "Say a short greeting."])
    assert result.exit_code == 0, result.output
    assert "Ahoy, matey! Welcome aboard." in result.output


def test_unknown_agent_exits_3(runner: CliRunner, monkeypatch: pytest.MonkeyPatch) -> None:
    _install(
        monkeypatch,
        mock_transport(chat_status=404, chat_body={"error": "agent not found: NOPE"}),
    )

    result = runner.invoke(cli.main, ["--agent", "NOPE", "hi"])
    assert result.exit_code == 3, result.output
    assert "not found" in result.output


def test_auth_error_exits_4(runner: CliRunner, monkeypatch: pytest.MonkeyPatch) -> None:
    _install(monkeypatch, mock_transport(chat_status=401, chat_body={"error": "unauthorized"}))

    result = runner.invoke(cli.main, ["--agent", "Drunken_Pirate", "hi"])
    assert result.exit_code == 4, result.output


def test_server_error_exits_5(runner: CliRunner, monkeypatch: pytest.MonkeyPatch) -> None:
    _install(monkeypatch, mock_transport(chat_status=500, chat_body={"error": "boom"}))

    result = runner.invoke(cli.main, ["--agent", "Drunken_Pirate", "hi"])
    assert result.exit_code == 5, result.output
    assert "boom" in result.output


def test_empty_message_is_usage_error_exit_2(runner: CliRunner) -> None:
    result = runner.invoke(cli.main, ["--agent", "Drunken_Pirate", "   "])
    assert result.exit_code == 2
    assert "empty" in result.output


def test_missing_agent_flag_is_usage_error(runner: CliRunner) -> None:
    result = runner.invoke(cli.main, ["hello"])
    assert result.exit_code == 2


def test_unreachable_server_exits_1(runner: CliRunner) -> None:
    # No mock installed: the real transport tries to connect and fails.
    result = runner.invoke(
        cli.main, ["--agent", "Drunken_Pirate", "--base-url", "http://localai.test:8080", "hi"]
    )
    assert result.exit_code == 1, result.output
    assert "cannot reach LocalAI" in result.output


def test_timeout_exits_1(runner: CliRunner, monkeypatch: pytest.MonkeyPatch) -> None:
    _install(monkeypatch, stall_transport())

    result = runner.invoke(cli.main, ["--agent", "Drunken_Pirate", "--timeout", "0.3", "hi"])
    assert result.exit_code == 1, result.output
    assert "timed out" in result.output
    assert MESSAGE_ID in result.output


def test_verbose_does_not_leak_api_key() -> None:
    from localai_steer.logging import redact

    masked = redact("authorization: Bearer supersecretkey123")
    assert "supersecretkey123" not in masked
    assert "[REDACTED]" in masked
    masked2 = redact("api_key: anothersecret")
    assert "anothersecret" not in masked2
