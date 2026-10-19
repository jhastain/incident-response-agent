#!/usr/bin/env bash
# Measure Bifrost MCP gateway concurrent capacity: fire N parallel MCP requests
# at escalating concurrency levels and report success count + latency spread per
# level. Answers whether true parallel tool access is available (vs the current
# single-shared-session serialization). Each request is an independent
# initialize+tools/list (stateless), so this tests the GATEWAY, not the ADK
# client-side session lock.
set -u
# Gateway and VK must come from the SAME environment. This paired the PROD gateway URL
# with a VK read from the DEV project, which is a mismatch that 401s (loud) — but it also
# aimed a concurrency probe at the prod gateway, which is not something to do by default.
# Both are now required with no default: the gateway selects an environment, and this
# script deliberately hammers whatever it is pointed at.
#
#   BIFROST_MCP_URL=https://llm-gateway-test.internal.example/mcp \
#   VK_PROJECT=aiops-test-project \
#   bash tests/eval/bifrost_capacity.sh
URL="${BIFROST_MCP_URL:-}"
VK_PROJECT="${VK_PROJECT:-}"
VK_SECRET="${VK_SECRET:-incident-response-agent-bfvk}"
if [ -z "$URL" ] || [ -z "$VK_PROJECT" ]; then
  echo "ERROR: set BIFROST_MCP_URL and VK_PROJECT — this probe sends sustained concurrent" >&2
  echo "       load to whichever gateway it is given, so neither is defaulted." >&2
  exit 1
fi
echo "capacity probe target: gateway=${URL} vk=${VK_SECRET}@${VK_PROJECT}"
TOK=$(gcloud secrets versions access latest --secret="$VK_SECRET" --project="$VK_PROJECT" 2>/dev/null)
if [ -z "$TOK" ]; then
  echo "ERROR: could not read secret ${VK_SECRET} from ${VK_PROJECT}" >&2
  exit 1
fi
BODY='{"jsonrpc":"2.0","id":1,"method":"tools/list","params":{}}'
INIT='{"jsonrpc":"2.0","id":0,"method":"initialize","params":{"protocolVersion":"2024-11-05","capabilities":{},"clientInfo":{"name":"cap","version":"1"}}}'

one() {
  # initialize then tools/list; print http/time of the tools/list
  curl -s -m 40 -X POST "$URL" \
    -H "Authorization: Bearer ${TOK}" -H "Content-Type: application/json" \
    -H "Accept: application/json, text/event-stream" \
    -d "$INIT" -o /dev/null 2>/dev/null
  curl -s -m 40 -X POST "$URL" \
    -H "Authorization: Bearer ${TOK}" -H "Content-Type: application/json" \
    -H "Accept: application/json, text/event-stream" \
    -d "$BODY" -w "%{http_code}/%{time_total}" -o /dev/null 2>/dev/null
}

for LVL in 1 3 5 10 15; do
  echo "=== concurrency ${LVL} ==="
  tmp=$(mktemp)
  pids=()
  for i in $(seq 1 "$LVL"); do
    ( one >> "$tmp"; echo >> "$tmp" ) &
    pids+=($!)
  done
  for p in "${pids[@]}"; do wait "$p"; done
  ok=$(grep -c "^200/" "$tmp" 2>/dev/null || echo 0)
  echo "  ok=${ok}/${LVL}"
  echo "  results: $(tr '\n' ' ' < "$tmp")"
  rm -f "$tmp"
done
echo "=== capacity test done ==="
