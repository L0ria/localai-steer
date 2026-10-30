#!/usr/bin/env bash
# Re-runnable live probe of the LocalAI agent API (pinned contract in docs/schema-pinned.md).
#
# Usage:
#   BASE_URL=http://172.19.0.1:8080 AGENT=Drunken_Pirate scripts/probe_live.sh
set -euo pipefail

BASE_URL="${BASE_URL:-http://localhost:8080}"
AGENT="${AGENT:-Drunken_Pirate}"
MESSAGE="${MESSAGE:-Say a short greeting.}"

echo "== version =="
curl -sS "$BASE_URL/version"; echo

echo "== agents (first 400 chars) =="
curl -sS "$BASE_URL/api/agents" | head -c 400; echo

echo "== POST /api/agents/$AGENT/chat (status + body) =="
curl -si -X POST "$BASE_URL/api/agents/$AGENT/chat" \
  -H 'Content-Type: application/json' \
  -d "{\"message\":\"$MESSAGE\",\"history\":[]}" | sed -n '1,12p'

echo "== error: unknown agent (expect 404) =="
curl -si -X POST "$BASE_URL/api/agents/NO_SUCH_AGENT_XYZ/chat" \
  -H 'Content-Type: application/json' \
  -d '{"message":"hi","history":[]}' | sed -n '1,8p'

echo "== error: empty message (expect 400) =="
curl -si -X POST "$BASE_URL/api/agents/$AGENT/chat" \
  -H 'Content-Type: application/json' \
  -d '{"message":"   ","history":[]}' | sed -n '1,8p'

echo
echo "NOTE: the final result arrives on the SSE broadcast, not in the POST body."
echo "Open the stream in another terminal BEFORE re-POSTing, or use the client:"
echo "  localai-steer --agent $AGENT --base-url $BASE_URL \"Say a short greeting.\""
echo "  (raw stream: curl -N -s $BASE_URL/api/agents/$AGENT/sse)"
