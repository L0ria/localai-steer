# Pinned wire contract — LocalAI agent API

**Single source of truth for M1/M2.** Every fact below was **observed on the
wire** against a live LocalAI instance, not just read from source.

- **LocalAI version:** `v4.11.0-68-gc03acb064` (`c03acb0647a867a7437683f34fc8711c22738785`)
- **Instance:** `http://172.19.0.1:8080` (17 agents, no auth)
- **Pinned:** 2026-10-30, agent `Drunken_Pirate`
- **Re-probe any time:** `scripts/probe_live.sh` (env: `BASE_URL`, `AGENT`, `MESSAGE`)

> Version drift (R5): older/newer releases may differ (e.g. field names).
> All client parsers are tolerant and degrade to defaults; re-run the probe
> and update this file if the contract changes.

---

## 1. `POST /api/agents/:name/chat`

- **Request:** `Content-Type: application/json`, body
  `{"message": "<text>", "history": [{"role","content"}, ...]}` (`history` may be `[]`).
- **Success → `HTTP 202 Accepted`**, `Content-Type: application/json`, body:

  ```json
  {"message_id": "1793336346624571020", "status": "message_received"}
  ```

  - **The "job-id field" is `message_id`** — a Unix-nanosecond integer as a string.
  - **The POST returns immediately (async).** The run executes in a detached
    server goroutine; **the final result is NOT in the POST response** — it
    arrives on the SSE stream (§2).
- **Errors (observed):**
  - `404` `{"error":"agent not found: NO_SUCH_AGENT_XYZ"}`
  - `400` `{"error":"Message cannot be empty"}`
  - `500` `{"error":"<msg>"}` (server fault)
  - `401`/`403` when an API key is required and missing/wrong (client-side mapping; shape `{"error": ...}`)

## 2. `GET /api/agents/:name/sse` (SSE observation channel)

- `200`, `Content-Type: text/event-stream`, `Cache-Control: no-cache`, `Connection: keep-alive`.
- **Frame format:** `event: <name>\ndata: <payload>\n\n` (blank-line delimited).
  `data` is a **single line**; JSON payloads are single-line; some events
  (`status`, `hud`) carry **HTML-escaped plain text, not JSON** — the parser
  must not assume all `data` is JSON.
- **It is a per-agent broadcast to all connected clients**, and it **replays
  the previous run's tail** to a newly-connected client — **≥ 18 envelopes
  observed** (2026-10-30 re-probe; the earlier "~10" bound is approximate —
  **treat the replay as unbounded for safety**). Correlation must therefore
  be by the `message_id`-derived `id` field, not by "first event
  after POST"; stale-replayed `json_error`s are filtered by timestamp (R4, §8).
- **Event catalog (observed on the wire):** `hud`, `json_message`,
  `json_message_status`, `observable_update`, `status`, `stream_event`
  (+ `json_error` on failure, from source).

| event | data shape (observed) | role in M0 |
|---|---|---|
| `json_message` | `{"content": str, "id": "<mid>-user"\|"<mid>-agent", "sender": "user"\|"agent", "timestamp": RFC3339, "metadata"?: {...}}` | **user echo** (`-user`); **FINAL RESULT** (`sender:"agent"`, `id:"<mid>-agent"`) |
| `json_message_status` | `{"status": "processing"\|"completed", "timestamp": RFC3339}` | turn lifecycle marker |
| `json_error` | `{"error": str, "timestamp": RFC3339}` | failure / cancellation signal |
| `stream_event` | `{"type": "reasoning"\|"content"\|"tool_call"\|"done", "content"?: str, "tool_name"?: str, "tool_args"?: any, "timestamp": RFC3339}` | live tokens; **tool calls** |
| `status` | HTML-escaped text: `Thinking: ...` / `Reasoning: ...` / `Action taken: ...` / `Parameters: ...` / `Result: ...` | progress (M1) |
| `hud` | **plain text with `<br>` separators** (not JSON, not HTML): `=====...<br>NowDoing: <br>DoingNext: <br>Your current goal is: <br>You have done: []<br>...<br>=====...` (sent ~every 1 s) | heartbeat; footer only (M1, Q2) |
| `observable_update` | JSON job/observable state: `id` (**integer job id** — the only id on any progress event), `agent`, `name:"job"`, `icon`, `creation.chat_completion_message.{role,content}`; `progress`/`completion` **optional (not observed in re-probe)** | job-id attribution (M1, R3) |

## 3. Correlation key (pinned)

- The POST returns `message_id`. The final result on SSE is the `json_message`
  whose **`id == "<message_id>-agent"`** and **`sender == "agent"`**. The user
  echo is `id == "<message_id>-user"`.
- **This is the exact, robust correlation key** — strictly better than the
  time-window fallback in issue #1 §5.1. M0 correlates on `id == f"{message_id}-agent"`.
- `stream_event` / `status` / `hud` do **not** carry `message_id`; they are
  attributed to the active turn by the correlation window (M1/M2 concern).

## 4. Tool-call name+args exposure — **Q3 input (ANSWER: YES, exposed)**

- **Observed on the wire:**
  `{"timestamp":"2026-10-30T04:27:58Z","tool_args":"","tool_name":"execute_command","type":"tool_call"}`.
- So `stream_event` with `type:"tool_call"` exposes **`tool_name`** and
  **`tool_args`**. This satisfies Q3's precondition ("fold tool-call state,
  *if* the event schema exposes them").
- **Caveat to pin in code (R2):** `tool_args` is a **string** and its content
  varies (empty, a JSON string, or a Go-map repr). The client treats it as an
  opaque string and attempts best-effort JSON parse; never assume a dict.
- **Caveat (M2, R3):** tool *results* arrive as free text in the `status`
  event (`Result: ...`) and in `observable_update`, not as a structured field
  — folding tool *results* is best-effort text parsing.

## 5. Server-side history bounds (from source; constrains M2/M3)

- Server sanitizes `history`: only `user`/`assistant` roles with non-empty
  text are kept; **max 40 messages** and **max 64 000 chars** total (most
  recent kept). M0 always sends `history: []`; the bounds matter for M2/M3
  trimming.

## 6. ⚠️ CRITICAL FINDING (R1) — the core cancellation assumption does NOT hold

Verified live + from source against `v4.11.0-68` local mode:

1. **There is no in-flight POST to abort.** `POST /chat` returns `202`
   immediately; the run is a **detached server goroutine**
   (`go func(){ ag.Ask(...) }()` in `core/services/agentpool/agent_pool.go`).
   Closing the POST connection does nothing (it already returned).
2. **SSE disconnect does NOT cancel the job.** Empirically: a long run was
   started, the **only** SSE observer was killed mid-run, and after waiting
   with **no** observer connected the job **still completed** (final
   `json_message` + `json_message_status:completed` observed on re-subscribe).
   SSE is a pure observation broadcast; disconnect only unregisters the
   subscriber.
3. **A second `POST /chat` does NOT cancel the first.** The single-worker pool
   **queues** it: in a controlled test, JOB1 (long) finished first, JOB2
   (short) second — both completed. No `cancel-previous` fires because the
   web-chat path does not set a `conversation_id` in job metadata.
4. **No agent cancel/stop HTTP route exists** (`core/http/routes/agents.go`
   has no `cancel`/`stop`/`abort`). The internal `LocalDispatcher.Cancel` /
   `EventBridge.CancelExecution` exist but are only wired for the
   **distributed/NATS** path, not exposed for local chat.

**Implication:** the issue #1 §1.1 "client disconnect cancels the server-side
job" premise is **FALSE** for this LocalAI. Consequence for M2: the
"interrupt" half of "interrupt + re-prompt" is **not achievable by closing
connections** on this API. Options (issue #2 §13): (a) accept that the old run
continues server-side and the re-prompt is a **queued** new turn (the only
thing the current API supports); (b) wait for LocalAI PR #12918 (native
steering); (c) a LocalAI-side cancel endpoint (out of scope N1). **M0 is
unaffected** (one-shot works fine) but M2's core mechanism must be
re-validated before build.

## 7. Auth

- Standard LocalAI API key: `Authorization: Bearer <key>` (or
  `X-LocalAI-API-Key`). The client sends `Authorization` when a key is set
  (flag `--key` or env `LOCALAI_API_KEY`), and no auth header otherwise.

## 8. Correlation rules (M1, approved in issue #4 §5 + coordinator review)

**Facts:** the SSE channel is a per-agent **broadcast** that **replays the
previous run's tail** to new subscribers (≥ 18 envelopes observed; bound
approximate — treat as unbounded for safety). The final result is correlated
by `id == "<message_id>-agent"` (M0). Progress events carry **no**
`message_id`. `observable_update` carries an integer **job `id`** — the only
id on any progress event. `json_error` carries **no** id, only a
`timestamp` (RFC3339/UTC).

| # | Event | Attribution |
|---|---|---|
| R1 | `json_message`, `json_message_status` | Correlated by `id`/`message_id` exactly as M0 (final result: `id == "<mid>-agent"`, `sender == "agent"`). Observed markers for the current turn call `correlator.note_turn_marker()`. |
| R2 | `stream_event`, `status`, `hud` | **Correlation window:** accepted iff they arrive **after** the `202` POST (turn start) and **before** the correlated final result / attributed error. In the single-turn CLI this is effectively "arrived while the turn is active". |
| R3 | `observable_update` | Accepted per R2, **plus** id check via `correlator.accept_observable()`: once `progress.job_id` is known, a later update with a *different* `id` is dropped (another job's state). First-seen `id` wins. |
| R4 | `json_error` (the stale-replay hazard, M0 carry-over #1) | **Rejected (logged, not raised) when** its `timestamp` (RFC3339/UTC) parses to a moment **older than the turn start (wall-clock UTC captured at `Correlator` construction, i.e. right after the `202` POST) minus a `clock_skew_s` grace** (default 2 s). **Accepted (raised — M0 behavior) when** the timestamp is missing/unparseable, or is at/after turn start. |
| R5 | unknown event names | Ignored, debug-logged (M0 tolerance preserved). |

**Known limitation (documented):** a previous failed run's `json_error`
whose timestamp falls within the `clock_skew_s` grace *before* the current
turn's POST could still be raised. In practice the previous run finished
(and its error was timestamped) well before the next POST, so the grace only
guards against clock skew between client and server, not against real
replay. Acceptable for M1.

**`done` event type:** the pinned catalog lists `stream_event type:"done"`.
It was not observed in the re-probe runs. M1 treats it as a
**stop-accumulation marker** (`progress.mark_done()`, phase → "done") and
logs it; it does not terminate the wait (the correlated final result still
does that) (Q3, approved).

**POST-then-SSE ordering (M0 carry-over #2):** the client keeps M0's order
(`POST /chat` → subscribe to SSE). This is safe **because** the final result
is always in the replay buffer (observed: replay carries the previous
run's tail, ≥ 18 envelopes), and the R4 rule makes the same buffer safe for
errors (a stale replayed `json_error` is rejected by timestamp).
