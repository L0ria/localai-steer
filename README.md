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
   in-flight HTTP/SSE request** (this cancels the current job server-side), append the
   addendum to the local history (optionally folding in whatever partial assistant output
   was streamed so far as an assistant message), then **re-send the full history + addendum
   as a fresh chat call**.
5. Result: functionally equivalent to Claude-CLI "hit enter to add to the current prompt",
   implemented as interrupt + re-prompt.

## Status

**Planning stage.** The detailed implementation plan (goals, tech stack, architecture,
core flow pseudocode, edge cases, milestones, testing strategy) is tracked in
[Issue #1](https://github.com/L0ria/localai-steer/issues/1).

## Related

- [LocalAI](https://github.com/mudler/localai) — the open-source AI engine this targets.
