"""CLI surface (M0): one-shot turn against a LocalAI agent.

``localai-steer --agent <name> "message"``
    POSTs the message, subscribes to the agent's SSE broadcast, and prints the
    final agent result correlated by ``message_id``.

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


async def run_once(cfg: Config, message: str) -> str:
    """Execute one turn and return the agent's final result content.

    Sequence (pinned in docs/schema-pinned.md):
    1. POST ``/api/agents/<agent>/chat`` (``history: []`` in M0) -> ``message_id``
    2. Consume the SSE broadcast until ``json_message id == "<message_id>-agent"``
    3. Return the final content; raise mapped errors otherwise.
    """
    async with LocalAIClient(cfg) as client:
        message_id = await client.chat(message, history=[])
        return await client.wait_for_result(message_id)


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
@click.argument("message")
def main(agent: str, base_url: str | None, key: str | None, timeout_s: float, verbose: bool,
message: str) -> None:
    """One-shot turn (M0): POST /chat, read SSE, print the final agent result.

    MESSAGE is the text to send to the agent.
    """
    if not (message or "").strip():
        raise click.UsageError("message cannot be empty")
    cfg = resolve_config(
        agent=agent, base_url=base_url, key=key, timeout_s=timeout_s, verbose=verbose
    )
    setup_logging(verbose=verbose)
    try:
        result = asyncio.run(run_once(cfg, message))
    except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
        click.echo(
            f"error: cannot reach LocalAI at {cfg.base_url} ({exc.__class__.__name__})", err=True
        )
        sys.exit(EXIT_ERROR)
    except httpx.InvalidURL as exc:
        click.echo(f"error: invalid --base-url: {exc}", err=True)
        sys.exit(EXIT_USAGE)
    except LocalAIError as exc:
        click.echo(f"error: {exc}", err=True)
        sys.exit(_exit_code_for(exc))
    click.echo(result)
