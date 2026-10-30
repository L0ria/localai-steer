"""Configuration: flag > env > default precedence, frozen dataclass."""

from __future__ import annotations

import os
from dataclasses import dataclass

ENV_BASE_URL = "LOCALAI_BASE_URL"
ENV_API_KEY = "LOCALAI_API_KEY"

DEFAULT_BASE_URL = "http://localhost:8080"
DEFAULT_TIMEOUT_S = 300.0


@dataclass(frozen=True)
class Config:
    """Resolved runtime configuration for one ``localai-steer`` invocation."""

    agent: str
    base_url: str = DEFAULT_BASE_URL
    api_key: str | None = None
    timeout_s: float = DEFAULT_TIMEOUT_S
    verbose: bool = False

    @property
    def auth_headers(self) -> dict[str, str]:
        """Authorization headers; empty when no key is configured."""
        return {"Authorization": f"Bearer {self.api_key}"} if self.api_key else {}


def resolve_config(
    *,
    agent: str,
    base_url: str | None = None,
    key: str | None = None,
    timeout_s: float | None = None,
    verbose: bool = False,
    env: dict[str, str] | None = None,
) -> Config:
    """Resolve configuration with precedence: explicit flag > environment > default.

    ``env`` is injectable for tests; defaults to ``os.environ``.
    """
    env = os.environ if env is None else env
    resolved_base = base_url or env.get(ENV_BASE_URL) or DEFAULT_BASE_URL
    resolved_key = key if key is not None else env.get(ENV_API_KEY)
    resolved_timeout = DEFAULT_TIMEOUT_S if timeout_s is None else timeout_s
    return Config(
        agent=agent,
        base_url=resolved_base,
        api_key=resolved_key,
        timeout_s=resolved_timeout,
        verbose=verbose,
    )
