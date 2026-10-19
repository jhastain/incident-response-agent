#!/usr/bin/env python
"""Isolate the concurrency bottleneck: fire N concurrent FULL MCP sessions
(initialize -> tools/list) against live Bifrost, OUTSIDE ADK. This is the exact
operation that hangs when 9 Tier-1 agents start at once.

If all N succeed -> Bifrost's MCP gateway handles concurrent sessions; the
bottleneck is ADK's MCP client. If they hang/fail -> it's the Bifrost MCP path.

Uses raw HTTP StreamableHTTP MCP (initialize, then tools/list with the
mcp-session-id header the server returns). No ADK, no shared state.
Usage: GCLOUD_TOKEN=... mcp_concurrent_sessions.py <N>
"""
import concurrent.futures as cf
import json
import os
import sys
import time
import urllib.request

URL = "https://llm-gateway.internal.example/mcp"
TOKEN = os.environ["GCLOUD_TOKEN"]
N = int(sys.argv[1]) if len(sys.argv) > 1 else 9


def post(body, session_id=None):
    headers = {
        "Authorization": "Bearer " + TOKEN,
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
    }
    if session_id:
        headers["mcp-session-id"] = session_id
    req = urllib.request.Request(URL, data=json.dumps(body).encode(), headers=headers)
    resp = urllib.request.urlopen(req, timeout=40)
    sid = resp.headers.get("mcp-session-id")
    raw = resp.read().decode("utf-8", "replace")
    # StreamableHTTP may return SSE-framed JSON
    text = raw
    if raw.startswith("event:") or "data:" in raw:
        for line in raw.splitlines():
            if line.startswith("data:"):
                text = line[5:].strip()
                break
    return resp.status, sid, text


def one_session(i):
    t0 = time.time()
    try:
        # 1) initialize
        st, sid, _ = post({
            "jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {"protocolVersion": "2024-11-05", "capabilities": {},
                       "clientInfo": {"name": f"conc-{i}", "version": "1"}},
        })
        # 2) tools/list on the SAME session (the call that hangs in-agent)
        st2, _, body2 = post({"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}}, session_id=sid)
        dt = round(time.time() - t0, 2)
        ntools = None
        try:
            ntools = len(json.loads(body2).get("result", {}).get("tools", []))
        except Exception:
            ntools = "parse?"
        return (i, "OK", st, st2, ntools, dt)
    except Exception as e:
        return (i, "FAIL", type(e).__name__, str(e)[:80], None, round(time.time() - t0, 2))


print(f"=== {N} concurrent full MCP sessions (initialize -> tools/list) ===")
t0 = time.time()
with cf.ThreadPoolExecutor(max_workers=N) as ex:
    results = list(ex.map(one_session, range(N)))
for r in sorted(results):
    print(f"  sess{r[0]}: {r[1]} init={r[2]} list={r[3]} tools={r[4]} t={r[5]}s")
ok = sum(1 for r in results if r[1] == "OK")
print(f"=== {ok}/{N} sessions completed tools/list in {round(time.time()-t0,2)}s total ===")
