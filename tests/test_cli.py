"""CLI end-to-end (mock transport): exit codes + stdout/stderr contracts."""

from __future__ import annotations

import pytest
from click.testing import CliRunner

import localai_steer.cli as cli
from conftest import MESSAGE_ID, load_fixture, mock_transport, stall_transport
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


# -------------------------------------------------------------------- M1: stdout/stderr contract


def test_non_tty_default_stdout_only_result_stderr_progress(
    runner: CliRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Non-TTY (CliRunner): default mode is "plain" -> stdout = final result
    # only, stderr = progress lines (one-shot contract, issue #4 §7).
    _install(
        monkeypatch, mock_transport(transcript=load_fixture("sse_multistep_toolcalls.txt"))
    )

    result = runner.invoke(cli.main, ["--agent", "Drunken_Pirate", "Say a short greeting."])
    assert result.exit_code == 0, result.output
    # stdout: exactly the final result (single line)
    stdout_lines = result.stdout.splitlines()
    assert stdout_lines == ["Ahoy, matey! The date is printed."]
    # stderr: plain progress lines
    assert "• running" in result.stderr
    assert any(
        line.startswith("• tool call: execute_command") for line in result.stderr.splitlines()
    )
    assert "• done" in result.stderr


def test_quiet_suppresses_all_progress(
    runner: CliRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install(
        monkeypatch, mock_transport(transcript=load_fixture("sse_multistep_toolcalls.txt"))
    )

    result = runner.invoke(
        cli.main, ["--agent", "Drunken_Pirate", "--quiet", "Say a short greeting."]
    )
    assert result.exit_code == 0, result.output
    assert result.stdout.splitlines() == ["Ahoy, matey! The date is printed."]
    # no progress lines on stderr
    assert "• running" not in result.stderr
    assert "• tool call" not in result.stderr
    assert "• done" not in result.stderr


def test_progress_flag_forces_plain_lines(
    runner: CliRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install(
        monkeypatch, mock_transport(transcript=load_fixture("sse_multistep_toolcalls.txt"))
    )

    result = runner.invoke(
        cli.main, ["--agent", "Drunken_Pirate", "--progress", "Say a short greeting."]
    )
    assert result.exit_code == 0, result.output
    assert "• running" in result.stderr
    assert "• done" in result.stderr
    assert result.stdout.splitlines() == ["Ahoy, matey! The date is printed."]


def test_quiet_stale_error_fixture_exits_0(
    runner: CliRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    # M0 carry-over #1 (R4): a replayed stale json_error does not abort the
    # turn, even with --quiet (no progress rendering).
    _install(monkeypatch, mock_transport(transcript=load_fixture("sse_stale_error_replay.txt")))

    result = runner.invoke(
        cli.main, ["--agent", "Drunken_Pirate", "--quiet", "Say a short greeting."]
    )
    assert result.exit_code == 0, result.output
    assert result.stdout.splitlines() == ["Ahoy, matey! Welcome aboard."]


def test_live_mode_panel_on_stderr(
    runner: CliRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Force mode "live" -> rich panel on stderr (bypasses the TTY check).
    _install(
        monkeypatch, mock_transport(transcript=load_fixture("sse_multistep_toolcalls.txt"))
    )
    monkeypatch.setattr(cli, "select_progress_mode", lambda **kw: "live")
    result = runner.invoke(cli.main, ["--agent", "Drunken_Pirate", "Say a short greeting."])

    assert result.exit_code == 0, result.output
    # stdout: final result only
    assert result.stdout.splitlines() == ["Ahoy, matey! The date is printed."]
    # stderr: rich panel (not plain lines)
    assert "REASONING (live)" in result.stderr
    assert "TOOL CALLS" in result.stderr
    assert "• running" not in result.stderr


def test_exit_codes_unchanged(runner: CliRunner, monkeypatch: pytest.MonkeyPatch) -> None:
    # 3: agent not found
    _install(
        monkeypatch, mock_transport(chat_status=404, chat_body={"error": "agent not found: NOPE"})
    )
    result = runner.invoke(cli.main, ["--agent", "NOPE", "hi"])
    assert result.exit_code == 3

    # 4: auth
    _install(monkeypatch, mock_transport(chat_status=401, chat_body={"error": "unauthorized"}))
    result = runner.invoke(cli.main, ["--agent", "Drunken_Pirate", "hi"])
    assert result.exit_code == 4

    # 5: server
    _install(monkeypatch, mock_transport(chat_status=500, chat_body={"error": "boom"}))
    result = runner.invoke(cli.main, ["--agent", "Drunken_Pirate", "hi"])
    assert result.exit_code == 5

    # 2: usage
    result = runner.invoke(cli.main, ["--agent", "Drunken_Pirate", "   "])
    assert result.exit_code == 2

    # 1: timeout
    _install(monkeypatch, stall_transport())
    result = runner.invoke(cli.main, ["--agent", "Drunken_Pirate", "--timeout", "0.3", "hi"])
    assert result.exit_code == 1
