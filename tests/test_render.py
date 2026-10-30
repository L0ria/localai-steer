"""Progress rendering: mode selection, PlainProgress, ProgressPanel (issue #4 §4.2/§6/§8)."""

from __future__ import annotations

import io
import time

import pytest
from rich.console import Console

from conftest import MESSAGE_ID, load_fixture
from localai_steer.progress import TurnProgress, apply_event
from localai_steer.render import (
    PlainProgress,
    ProgressPanel,
    select_progress_mode,
)
from localai_steer.sse import parse_sse_text


def make_progress() -> TurnProgress:
    return TurnProgress(message_id=MESSAGE_ID, started_at=time.monotonic())


def progress_from_fixture(name: str) -> TurnProgress:
    progress = make_progress()
    from localai_steer.progress import Correlator

    correlator = Correlator(
        turn_started_utc=__import__("datetime").datetime.now(__import__("datetime").UTC),
        turn_started_monotonic=time.monotonic(),
    )
    for event in parse_sse_text(load_fixture(name)):
        apply_event(progress, event, correlator)
    return progress


# ---------------------------------------------------------- select_progress_mode


@pytest.mark.parametrize(
    ("quiet", "force", "tty", "expected"),
    [
        (True, False, True, "off"),
        (True, False, False, "off"),
        (True, True, True, "off"),  # --quiet wins over --progress
        (False, True, True, "plain"),
        (False, True, False, "plain"),
        (False, False, True, "live"),
        (False, False, False, "plain"),
    ],
)
def test_select_progress_mode_truth_table(
    quiet: bool, force: bool, tty: bool, expected: str
) -> None:
    assert select_progress_mode(quiet=quiet, force_progress=force, stdout_is_tty=tty) == expected


# ------------------------------------------------------------------ PlainProgress


def test_plain_progress_emits_state_changes_only() -> None:
    stream = io.StringIO()
    plain = PlainProgress(stream)
    progress = progress_from_fixture("sse_multistep_toolcalls.txt")
    plain.set_phase("running")
    plain.update(progress)
    plain.set_phase("done")
    lines = stream.getvalue().splitlines()
    assert "• running" in lines
    assert "• done" in lines
    assert any(line.startswith("• tool call: execute_command") for line in lines)
    assert any(line.startswith("• status: Action taken:") for line in lines)
    assert any(line.startswith("• output:") for line in lines)
    assert "• job id: 12" in lines


def test_plain_progress_rate_limits_output_growth() -> None:
    # N fragments -> bounded line count (rate-limited to 1 line / 0.5 s).
    now = [0.0]

    def fake_clock() -> float:
        now[0] += 0.1
        return now[0]

    stream = io.StringIO()
    plain = PlainProgress(stream, clock=fake_clock)
    for _ in range(50):
        progress = make_progress()
        progress.add_content("x" * 20)
        plain.update(progress)
    output_lines = [
        line for line in stream.getvalue().splitlines() if line.startswith("• output:")
    ]
    # 50 fragments at 0.1 s spacing with a 0.5 s rate limit -> at most ~11 lines
    assert len(output_lines) <= 11
    assert len(output_lines) >= 1


def test_plain_progress_no_line_per_fragment() -> None:
    stream = io.StringIO()
    plain = PlainProgress(stream, clock=lambda: 0.0)  # frozen clock: rate limit bites
    for _ in range(20):
        progress = make_progress()
        progress.add_content("word ")
        plain.update(progress)
    lines = stream.getvalue().splitlines()
    # only the first output line (frozen clock keeps the rate limit closed)
    assert sum(1 for line in lines if line.startswith("• output:")) == 1


def test_plain_progress_heartbeat_is_silent() -> None:
    stream = io.StringIO()
    plain = PlainProgress(stream)
    plain.note_heartbeat()
    assert stream.getvalue() == ""


# ------------------------------------------------------------------ ProgressPanel


def test_progress_panel_renders_expected_sections() -> None:
    console = Console(record=True, file=io.StringIO(), width=100, force_terminal=False)
    progress = progress_from_fixture("sse_multistep_toolcalls.txt")
    with ProgressPanel(console, agent="Drunken_Pirate", message_id=MESSAGE_ID) as panel:
        panel.set_phase("running")
        panel.update(progress)
    out = console.export_text()
    assert "REASONING (live)" in out
    assert "The user wants me to list the cwd." in out
    assert "TOOL CALLS" in out
    assert "execute_command" in out
    assert "OUTPUT (partial)" in out
    assert "Ahoy, matey! The date is printed." in out
    assert "STATUS" in out
    # panel shows the LAST status line (Result), unescaped
    assert "Result: <html>main  src  tests</html>" in out
    assert "job id: 12" in out
    assert "running" in out


def test_progress_panel_error_phase_renders_error_section() -> None:
    console = Console(record=True, file=io.StringIO(), width=100, force_terminal=False)
    progress = make_progress()
    progress.mark_error("tool exploded")
    with ProgressPanel(console, agent="A", message_id="1") as panel:
        panel.set_phase("error")
        panel.update(progress)
    out = console.export_text()
    assert "ERROR" in out
    assert "tool exploded" in out


def test_progress_panel_empty_state_renders_waiting() -> None:
    console = Console(record=True, file=io.StringIO(), width=100, force_terminal=False)
    with ProgressPanel(console, agent="A", message_id="1") as panel:
        panel.update(make_progress())
    out = console.export_text()
    assert "waiting for agent events" in out


def test_progress_panel_html_escaped_text_renders_literally() -> None:
    # status payloads carry HTML-escaped angle brackets; after unescape they
    # must render literally (no markup parsing).
    console = Console(record=True, file=io.StringIO(), width=100, force_terminal=False)
    progress = make_progress()
    progress.add_status("Thinking", "<html>choosing a greeting...</html>")
    with ProgressPanel(console, agent="A", message_id="1") as panel:
        panel.update(progress)
    out = console.export_text()
    assert "<html>choosing a greeting...</html>" in out
