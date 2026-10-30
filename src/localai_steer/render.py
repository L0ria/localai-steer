"""Progress rendering (M1): rich Live panel (TTY) + plain stderr lines (pipe).

Both renderers write to **stderr only** — stdout carries the final result
alone (one-shot contract, issue #1 §7 / issue #4 §7). All text is inserted
via ``rich.text.Text`` (no markup parsing): ``status``/``hud`` payloads
contain HTML-escaped angle brackets that must render literally after
``html.unescape``.
"""

from __future__ import annotations

import sys
import time
from typing import IO

from rich import box
from rich.console import Console, Group
from rich.live import Live
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from localai_steer.progress import TurnProgress

REASONING_TAIL_CHARS = 400  # show the last ~400 chars with a leading "…"
ARGS_MAX_CHARS = 60  # tool-call args truncated to ~60 chars with "…"
OUTPUT_PREVIEW_CHARS = 80  # plain-mode output preview
PLAIN_RATE_LIMIT_S = 0.5  # max 1 line / 0.5 s for output-growth lines

PHASE_WAITING = "waiting"
PHASE_RUNNING = "running"
PHASE_DONE = "done"
PHASE_ERROR = "error"

_PHASE_STYLES = {
    PHASE_WAITING: "dim",
    PHASE_RUNNING: "cyan",
    PHASE_DONE: "green",
    PHASE_ERROR: "red bold",
}


def _truncate_tail(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return "…" + text[-limit:]


def _truncate(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[: limit - 1] + "…"


def select_progress_mode(*, quiet: bool, force_progress: bool, stdout_is_tty: bool) -> str:
    """Pick the progress mode (issue #4 §7, Q1 approved).

    ``'live'`` (rich panel on stderr) | ``'plain'`` (stderr lines) | ``'off'``.
    ``--quiet`` wins; then ``--progress`` forces plain; otherwise a TTY gets
    the live panel and a pipe gets plain lines.
    """
    if quiet:
        return "off"
    if force_progress:
        return "plain"
    return "live" if stdout_is_tty else "plain"


class ProgressPanel:
    """rich ``Live`` panel (TTY mode). Renders to stderr; never touches stdout.

    Usage::

        with ProgressPanel(console, agent="A", message_id="123") as panel:
            panel.set_phase("running")
            panel.update(turn_progress)
    """

    def __init__(
        self,
        console: Console,
        *,
        agent: str,
        message_id: str,
        transient: bool = False,
    ) -> None:
        self._console = console
        self._agent = agent
        self._message_id = message_id
        self._transient = transient
        self._phase = PHASE_WAITING
        self._progress: TurnProgress | None = None
        self._started_at = time.monotonic()
        self._event_count = 0
        self._live: Live | None = None

    # ------------------------------------------------------------ context

    def __enter__(self) -> ProgressPanel:
        self._live = Live(
            self._build(),
            console=self._console,
            auto_refresh=False,  # event-driven refresh only (no timer churn)
            transient=self._transient,
            redirect_stdout=False,
            redirect_stderr=False,
        )
        self._live.start(refresh=True)
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        if self._live is not None:
            self._live.stop()
            self._live = None

    # ------------------------------------------------------------- state

    def set_phase(self, phase: str) -> None:
        self._phase = phase
        self._refresh()

    def update(self, progress: TurnProgress) -> None:
        """Event-driven update: capture the buffer state and refresh once."""
        self._progress = progress
        self._event_count += 1
        self._refresh()

    def note_heartbeat(self) -> None:
        """A ``hud`` heartbeat arrived (footer display only, Q2)."""
        self._refresh()

    def _refresh(self) -> None:
        if self._live is not None:
            self._live.update(self._build())
            self._live.refresh()

    # ----------------------------------------------------------- rendering

    def _elapsed(self) -> str:
        elapsed = int(time.monotonic() - self._started_at)
        return f"{elapsed // 60:02d}:{elapsed % 60:02d}"

    def _title(self) -> Text:
        style = _PHASE_STYLES.get(self._phase, "white")
        if self._message_id:
            if len(self._message_id) > 12:
                short_mid = self._message_id[:4] + "…" + self._message_id[-4:]
            else:
                short_mid = self._message_id
            mid_part = f" · msg {short_mid}"
        else:
            mid_part = ""
        return Text.assemble(
            (self._agent, "bold"),
            (mid_part, "dim"),
            (f" · {self._elapsed()} · ", "dim"),
            (self._phase, style),
        )

    def _footer(self) -> Text:
        progress = self._progress
        bits = [f"events: {self._event_count}"]
        if progress is not None and progress.job_id is not None:
            bits.append(f"job id: {progress.job_id}")
        bits.append("hud heartbeat ok" if self._event_count else "waiting for events…")
        return Text(" · ".join(bits), style="dim")

    def _reasoning_section(self) -> Text | None:
        progress = self._progress
        if progress is None or not progress.reasoning_chunks:
            return None
        tail = _truncate_tail(progress.partial_reasoning, REASONING_TAIL_CHARS)
        return Group(Text("REASONING (live)", style="bold"), Text(f"  {tail}", style="dim"))

    def _tool_calls_section(self) -> Text | None:
        progress = self._progress
        if progress is None or not progress.tool_calls:
            return None
        table = Table(
            box=box.SIMPLE_HEAVY,
            expand=True,
            pad_edge=False,
        )
        for index, call in enumerate(progress.tool_calls, start=1):
            state = "done" if call.finished else "running"
            table.add_row(
                str(index),
                call.name or "?",
                _truncate(call.args, ARGS_MAX_CHARS) if call.args else "—",
                state,
            )
        return Group(Text("TOOL CALLS", style="bold"), table)

    def _output_section(self) -> Text | None:
        progress = self._progress
        if progress is None or not progress.content_chunks:
            return None
        text = progress.partial_assistant_text
        return Group(Text("OUTPUT (partial)", style="bold"), Text(f"  {text}"))

    def _status_section(self) -> Text | None:
        progress = self._progress
        if progress is None or not progress.status_lines:
            return None
        line = progress.status_lines[-1]
        body = f"{line.label}: {line.text}" if line.label else line.text
        return Group(Text("STATUS", style="bold"), Text(f"  {body}"))

    def _error_section(self) -> Text | None:
        progress = self._progress
        if progress is None or progress.error is None:
            return None
        return Group(Text("ERROR", style="red bold"), Text(f"  {progress.error}", style="red"))

    def _build(self) -> Panel:
        sections: list[object] = []
        for section in (
            self._reasoning_section(),
            self._tool_calls_section(),
            self._output_section(),
            self._status_section(),
            self._error_section(),
        ):
            if section is not None:
                sections.append(section)
        if not sections:
            sections.append(Text("waiting for agent events…", style="dim"))
        return Panel(
            Group(*sections),
            title=self._title(),
            subtitle=self._footer(),
            border_style="cyan" if self._phase != PHASE_ERROR else "red",
            expand=False,
        )


class PlainProgress:
    """Line-oriented fallback (non-TTY / ``--progress``). Writes to stderr.

    Emits only on *meaningful* state changes — never one line per token
    fragment; output-growth lines are rate-limited to 1 line / 0.5 s.
    """

    def __init__(self, stream: IO[str] | None = None, *, clock: object = time.monotonic) -> None:
        self._stream = stream if stream is not None else sys.stderr
        self._clock = clock
        self._last_output_line_at: float | None = None
        self._last_output_len = 0
        self._last_tool_count = 0
        self._last_status_count = 0
        self._last_phase: str | None = None
        self._last_job_id: int | None = None

    def set_phase(self, phase: str) -> None:
        if phase != self._last_phase:
            self._last_phase = phase
            self._write(f"• {phase}")

    def update(self, progress: TurnProgress) -> None:
        # New tool calls (state change, always emitted).
        if len(progress.tool_calls) > self._last_tool_count:
            new = progress.tool_calls[self._last_tool_count :]
            self._last_tool_count = len(progress.tool_calls)
            for call in new:
                args = f"({call.args})" if call.args else ""
                self._write(f"• tool call: {call.name}{args}")
        # New status lines (state change, always emitted).
        if len(progress.status_lines) > self._last_status_count:
            new = progress.status_lines[self._last_status_count :]
            self._last_status_count = len(progress.status_lines)
            for line in new:
                text = f"{line.label}: {line.text}" if line.label else line.text
                self._write(f"• status: {text}")
        # Job id learned (state change, once).
        if progress.job_id is not None and progress.job_id != self._last_job_id:
            self._last_job_id = progress.job_id
            self._write(f"• job id: {progress.job_id}")
        # Output growth: one preview line, rate-limited.
        out_len = len(progress.partial_assistant_text)
        if out_len > self._last_output_len:
            now = self._clock()
            if (
                self._last_output_line_at is None
                or now - self._last_output_line_at >= PLAIN_RATE_LIMIT_S
            ):
                preview = _truncate(progress.partial_assistant_text, OUTPUT_PREVIEW_CHARS)
                self._write(f"• output: {preview}")
                self._last_output_line_at = now
            self._last_output_len = out_len

    def note_heartbeat(self) -> None:
        return None  # heartbeats are not line-oriented noise

    def _write(self, line: str) -> None:
        self._stream.write(line + "\n")
        flush = getattr(self._stream, "flush", None)
        if callable(flush):
            flush()


def make_panel_console() -> Console:
    """A stderr console for the live panel (stdout stays clean)."""
    return Console(stderr=True, highlight=False, markup=False)
