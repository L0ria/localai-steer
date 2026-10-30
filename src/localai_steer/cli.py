"""CLI surface (M0 + M1): one-shot turn + SSE progress rendering.

``localai-steer --agent <name> "message"``
    POSTs the message, subscribes to the agent's SSE broadcast, and prints
    the final agent result correlated by ``message_id``.

M1 adds live progress rendering (issue #4 §7, Q1 approved):
- TTY (default)          -> rich Live panel on **stderr**
- piped / ``--progress`` -> plain progress lines on **stderr**
- ``--quiet``            -> no progress at all

stdout carries **only** the final result in every mode (one-shot contract).

Exit codes:
    0  success
    1  generic failure (unreachable server, timeout, agent ``json_error``)
    2  usage error (empty message, bad flags)
    3  agent not found (HTTP 404)
    4  unauthorized (HTTP 401/403)
    5  server error (HTTP 5xx)
"""

from __future__ import annotations

import asyncio
import sys
import time
from datetime import UTC, datetime

import click
import httpx

from localai_steer import __version__
from localai_steer.client import (
    AgentNotFound,
    AuthError,
    LocalAIClient,
    LocalAIError,
    ServerError,
)
from localai_steer.config import Config, resolve_config
from localai_steer.logging import setup_logging
from localai_steer.progress import Correlator, TurnProgress, apply_event
from localai_steer.render import (
    PlainProgress,
    ProgressPanel,
    make_panel_console,
    select_progress_mode,
)
from localai_steer.sse import SSEEvent

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_USAGE = 2
EXIT_AGENT_NOT_FOUND = 3
EXIT_AUTH = 4
EXIT_SERVER = 5


def _exit_code_for(exc: LocalAIError) -> int:
    if isinstance(exc, AgentNotFound):
        return EXIT_AGENT_NOT_FOUND
    if isinstance(exc, AuthError):
        return EXIT_AUTH
    if isinstance(exc, ServerError):
        return EXIT_SERVER
    return EXIT_ERROR


class _LiveTurnRenderer:
    """rich Live panel scoped to one turn (stderr; stdout stays clean)."""

    def __init__(self, agent: str) -> None:
        self._panel = ProgressPanel(make_panel_console(), agent=agent, message_id="")

    def start(self) -> None:
        self._panel.__enter__()
        self._panel.set_phase("running")

    def update(self, progress: TurnProgress) -> None:
        self._panel.update(progress)

    def finish(self, phase: str) -> None:
        self._panel.set_phase(phase)
        self._panel.__exit__(None, None, None)


class _PlainTurnRenderer:
    """Plain stderr lines scoped to one turn (pipe mode)."""

    def __init__(self) -> None:
        self._plain = PlainProgress()

    def start(self) -> None:
        self._plain.set_phase("running")

    def update(self, progress: TurnProgress) -> None:
        self._plain.update(progress)

    def finish(self, phase: str) -> None:
        self._plain.set_phase(phase)


async def run_once(
    cfg: Config,
    message: str,
    *,
    on_event: object = None,
    progress: TurnProgress | None = None,
) -> str:
    """Execute one turn and return the agent's final result content.

    M0 signature + optional ``on_event`` callback (default ``None`` = M0
    behavior). When ``progress`` is supplied, its ``message_id`` is set
    from the ``202`` POST response (the correlation key).

    Sequence (pinned in docs/schema-pinned.md):
    1. POST ``/api/agents/<agent>/chat`` (``history: []`` in M0/M1) -> ``message_id``
    2. Consume the SSE broadcast until ``json_message id == "<message_id>-agent"``
    3. Return the final content; raise mapped errors otherwise.
    """
    async with LocalAIClient(cfg) as client:
        message_id = await client.chat(message, history=[])
        if progress is not None:
            progress.message_id = message_id
        return await client.wait_for_result(message_id, on_event=on_event)


@click.command(context_settings={"help_option_names": ["-h", "--help"]})
@click.version_option(__version__, "--version", prog_name="localai-steer")
@click.option("--agent", "-a", required=True, help="LocalAI agent name")
@click.option("--base-url", "base_url", default=None, help="LocalAI base URL (default http://localhost:8080)")
@click.option("--key", "key", default=None, help="API key (default: env LOCALAI_API_KEY)")
@click.option(
    "--timeout",
    "timeout_s",
    type=float,
    default=300.0,
    show_default=True,
    help="Seconds to wait for the final agent result",
)
@click.option("--verbose", "-v", is_flag=True, help="Debug logs (API key redacted)")
@click.option("--quiet", is_flag=True, help="Suppress progress output (stdout = final result only)")
@click.option(
    "--progress",
    "force_progress",
    is_flag=True,
    help="Force plain progress lines to stderr (e.g. when piped)",
)
@click.argument("message")
def main(
    agent: str,
    base_url: str | None,
    key: str | None,
    timeout_s: float,
    verbose: bool,
    quiet: bool,
    force_progress: bool,
    message: str,
) -> None:
    """One-shot turn (M0) + live progress rendering (M1).

    POST /chat, read SSE, print the final agent result. Progress is
    rendered live on stderr (rich panel for a TTY, plain lines when piped
    or with ``--progress``); stdout carries only the final result.

    MESSAGE is the text to send to the agent.
    """
    if not (message or "").strip():
        raise click.UsageError("message cannot be empty")
    cfg = resolve_config(
        agent=agent, base_url=base_url, key=key, timeout_s=timeout_s, verbose=verbose
    )
    setup_logging(verbose=verbose)

    mode = select_progress_mode(
        quiet=quiet, force_progress=force_progress, stdout_is_tty=sys.stdout.isatty()
    )

    progress: TurnProgress | None = None
    correlator: Correlator | None = None
    renderer: _LiveTurnRenderer | _PlainTurnRenderer | None = None
    if mode == "live":
        renderer = _LiveTurnRenderer(agent)
    elif mode == "plain":
        renderer = _PlainTurnRenderer()
    if renderer is not None:
        progress = TurnProgress(message_id="", started_at=time.monotonic())
        correlator = Correlator(
            turn_started_utc=datetime.now(UTC),
            turn_started_monotonic=time.monotonic(),
        )

    def _on_event(event: SSEEvent) -> None:
        if progress is not None and correlator is not None:
            apply_event(progress, event, correlator)
        if renderer is not None and progress is not None:
            renderer.update(progress)

    try:
        if renderer is not None:
            renderer.start()
        result = asyncio.run(
            run_once(
                cfg,
                message,
                on_event=_on_event if renderer is not None else None,
                progress=progress,
            )
        )
    except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
        if renderer is not None:
            renderer.finish("error")
        click.echo(
            f"error: cannot reach LocalAI at {cfg.base_url} ({exc.__class__.__name__})", err=True
        )
        sys.exit(EXIT_ERROR)
    except httpx.InvalidURL as exc:
        click.echo(f"error: invalid --base-url: {exc}", err=True)
        sys.exit(EXIT_USAGE)
    except LocalAIError as exc:
        if renderer is not None:
            renderer.finish("error")
        click.echo(f"error: {exc}", err=True)
        sys.exit(_exit_code_for(exc))

    if renderer is not None:
        renderer.finish("done")
    click.echo(result)
