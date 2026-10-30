# localai-steer

A thin terminal client that gives **LocalAI agents** a Claude-CLI-style steering experience:
**add input while the agent is working**.

## The problem

LocalAI agents (powered by the embedded LocalAGI engine) execute each chat message as a
self-contained job run to completion — single worker by default, and **no API primitive
exists to inject additional user input into a currently-running agent run**. A message typed
mid-run is simply queued as a brand-new turn that only starts after the current run finishes.

In the Claude CLI, you can just hit **Enter** mid-run to add information to the original
prompt or steer the model's current thought process. `localai-steer` brings the same UX to
LocalAI agents.

## The approach (agreed workaround)

A thin terminal client that achieves the same effect via **interrupt + re-prompt with
accumulated context**:

1. Keeps the conversation buffer **locally** — the LocalAI server keeps no run history, so
   the client is the source of truth.
2. Starts a turn with `POST /api/agents/<name>/chat`
   (`{"message": "<user input>", "history": [{"role": "user"|"assistant", "content": "..."}, ...]}`).
3. Streams the agent's progress/output via the SSE endpoint `GET /api/agents/<name>/sse`
   (job/agent events).
4. When the user presses Enter with an addendum while the run is in flight: **abort the
   in-flight HTTP/SSE request**, append the addendum to the local history (optionally
   folding in whatever partial assistant output was streamed so far as an assistant
   message), then **re-send the full history + addendum as a fresh chat call**.
5. Result: functionally equivalent to Claude CLI's "add to the current prompt",
   implemented as interrupt + re-prompt.

## Status

**M0 — implemented (this PR).** One-shot turn against a live LocalAI:

- Installable Python package (Python ≥ 3.11, `httpx`/`click`/`rich`/`prompt_toolkit`),
  console script `localai-steer`.
- `localai-steer --agent <name> "message"` → `POST /api/agents/<name>/chat` (202,
  `message_id`) → subscribe to `GET /api/agents/<name>/sse` → print the final agent
  result **correlated by `message_id`**.
- Tolerant SSE parser, pinned response models, typed errors + exit codes
  (0 ok · 1 generic/timeout · 2 usage · 3 agent-not-found · 4 auth · 5 server).
- Unit tests (mock transport + recorded live fixtures) and a re-runnable live probe
  (`scripts/probe_live.sh`).
- **Wire contract pinned live** (LocalAI `v4.11.0-68-gc03acb064`):
  [docs/schema-pinned.md](docs/schema-pinned.md) — including the **R1 finding**:
  client-disconnect does **not** cancel the server-side job (no abortable POST, SSE is a
  pure observation broadcast, a 2nd message queues, no cancel route). M2's "interrupt"
  mechanism must be re-designed around this.

**Not yet (later milestones):** live input line + interrupt/re-prompt (M2), live progress
rendering (M1), history persistence (M3), config file / `--once` / CI (M4).

## Usage (M0)

```bash
# install (editable)
uv venv --python 3.11 .venv && source .venv/bin/activate
uv pip install -e '.[dev]'

# one-shot turn
localai-steer --agent Drunken_Pirate "Say a short greeting."

# options
localai-steer -a Drunken_Pirate --base-url http://172.19.0.1:8080 \
              --key "$LOCALAI_API_KEY" --timeout 300 -v "Say a short greeting."
```

Flags: `--agent/-a` (required) · `--base-url` (default `http://localhost:8080`,
env `LOCALAI_BASE_URL`) · `--key` (env `LOCALAI_API_KEY`) · `--timeout` (default 300 s) ·
`--verbose/-v` (debug logs, API key redacted) · `--version`.

## Development

```bash
uv pip install -e '.[dev]'
pytest            # unit tests (no live instance needed)
ruff check .      # lint
scripts/probe_live.sh   # live probe of the pinned contract (BASE_URL/AGENT env)
```

## Related

- [Issue #1](https://github.com/L0ria/localai-steer/issues/1) — goals, tech stack, architecture, milestones.
- [Issue #2](https://github.com/L0ria/localai-steer/issues/2) — M0 detailed implementation plan (probe & pin, R1 finding).
- [LocalAI](https://github.com/mudler/localai) — the open-source AI engine this targets.
