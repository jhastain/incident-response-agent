# Handoff — IR Agent reliable e2e (container concurrency fix)

**Date:** 2026-07-12 · **Branch:** `ado-200001` (dev, **not pushed, no PR** — user works this in a dev-side env)
**Engine:** `projects/000000000002/locations/us-central1/reasoningEngines/1000000000000000003` (dev project `aiops-dev-project`)

> **Note (ported to AIOps 2026-07-29):** the engine ID below (`1000000000000000003`) was current on the capture date and has since been replaced. Resolve the live engine by `displayName: incident_response_agent` — see the `investigate` skill.

## Goal (MVP)
The deployed Incident Response Agent must **reliably provide triage answers end-to-end, with all tool calls working.** Success = the 3 fixed e2e scenarios each return: no stream cut, a full 6/6-section report, verification present, correct classification (category/service/environment), and zero failed/dead tool calls.

## Status: NOT met — one remaining defect

The agent **logic is proven sound**. On a *freshly deployed* container, scenario 1 (cloud-infra) passed FULLY e2e: no cut, 71 working tool calls, 6/6 report, verification present, correct classification, 16 agents. Migration, deploy, tool calls, and all prior fixes work.

**The blocker is a serving-layer defect in the migrated 1.1.0 container:** it **wedges under concurrent (and even sequential-after-load) requests and never recovers.**

### Confirmed evidence (not hypothesis)
- Fresh container: 1 `async_create_session` → HTTP 200 in ~1s. ✅
- Then 3 parallel `async_create_session` → all hang (HTTP 000, 40s). ❌
- After that, even a **single** `async_create_session` → hangs (HTTP 000, 30s). The instance stays wedged. ❌
- Container logs during wedge: continuous `WARNING:asyncio:socket.send() raised exception.` (single event loop stuck writing to dead/closed connections).
- `min_instances=1` (and `NUM_WORKERS=1`) → the one wedged instance is never cycled out.

### Root cause (high confidence, needs the fix to confirm)
The 1.1.0 migration made `agent_runtime` a **self-hosted container** (FastAPI + uvicorn; we own serving). Two things block the single async event loop:
1. **Single uvicorn worker.** `Dockerfile` CMD: `uv run uvicorn incident_response_agent.fast_api_app:app --host 0.0.0.0 --port 8080` — no `--workers`. `NUM_WORKERS=1`.
2. **Per-request synchronous runtime build.** `app/app_utils/reasoning_engine_adapter.py` `get_runtime()` lazily builds the entire 16-agent `AdkApp` and calls `runtime.set_up()` **inside the first request handler**. That synchronous, heavy init blocks the event loop; concurrent requests starve, connections die, loop wedges.

## The fix to implement (Option B)
1. **Build the runtime eagerly at startup, not per-request.** In `app/fast_api_app.py` `lifespan()` (runs before serving), build the AdkApp runtime once and stash it on `app.state`, and have `reasoning_engine_adapter.get_runtime()` return that pre-built instance instead of building on first request. Removes the synchronous build from the request path.
2. **Run multiple uvicorn workers** (Dockerfile CMD `--workers 4` or set via env) so one blocked request doesn't starve all traffic. NOTE: multiple workers = multiple processes; ensure the runtime build works per-worker and session service is external (VertexAiSessionService — it is, when engine id present).
3. **Consider raising `min_instances`** and/or a healthcheck so a wedged instance gets replaced.
4. Verify the instance **recovers** after a burst (the key regression: today it does not).

## Verification (how to test)
Deploy, then:
```bash
ENGINE="https://us-central1-aiplatform.googleapis.com/v1beta1/projects/000000000002/locations/us-central1/reasoningEngines/1000000000000000003"
TOKEN="$(gcloud auth print-access-token)"
# single create — must be fast
curl -s -m 30 -X POST "${ENGINE}:query" -H "Authorization: Bearer ${TOKEN}" -H "Content-Type: application/json" \
  -d '{"class_method":"async_create_session","input":{"user_id":"first.last@acme.example"}}' -w "\nHTTP=%{http_code} t=%{time_total}s\n"
# 3 parallel — the real test; all must return 200 fast
for i in 1 2 3; do ( curl -s -m 40 -X POST "${ENGINE}:query" -H "Authorization: Bearer ${TOKEN}" -H "Content-Type: application/json" -d '{"class_method":"async_create_session","input":{"user_id":"x"}}' -w "P$i=%{http_code}/%{time_total}s\n" -o /dev/null ) & done; wait
```
Then full e2e (parallel harness is the goal; sequential is the fallback):
```bash
cd <workspace>/aiops/incident_response_agent
bash tests/eval/run_agent_eval.sh "tests/eval/results/<label>"          # parallel (target)
SEQUENTIAL=1 bash tests/eval/run_agent_eval.sh "tests/eval/results/<label>"  # one-at-a-time fallback
```
PASS gate per scenario: `stream_cut==false`, `report_sections_score==6`, `verification_present==true`, classification matches ground truth (`tests/eval/ground-truth/`), zero dead tool calls.

## Environment facts (all fixed, don't re-litigate)
- **Deploy command** (canonical — in `deploy.ps1`): `uvx google-agents-cli@1.1.0 deploy --project aiops-dev-project --region us-central1 --no-confirm-project --deployment-target agent_runtime --secrets BIFROST_API_TOKEN=bifrost-vk-ir-agent:latest --update-env-vars GCP_MCP_ENABLED=false`
- **Latest agents-cli is 1.1.0**; project migrated to it (commit `5472b8e`). v1.0.0 direct-deploy fails (needs Dockerfile); `scaffold upgrade` is broken (can't fetch 0.4.0 baseline).
- **Local Python is 3.13.3** (`C:\python313\python.exe`). The `.venv` was rebuilt on 3.13; `plain python3`/`python` on PATH is a broken WindowsApps shim — use `/c/python313/python` or `.venv/Scripts/python.exe`.
- **litellm 1.91.2** (latest) — requires `<3.14`; container Dockerfile is python:3.12-slim (compatible). Do NOT let the venv drift to 3.14.
- **Deploy packaging gotchas (fixed):** `.gcloudignore` keeps upload < 8MB (was failing on ~4MB untracked scratch + 2.3MB `.adk/session.db`). Never let a `pip freeze` land at `app/app_utils/.requirements.txt` — it poisons the managed build (it's gitignored now).
- **Bifrost is healthy** — verified live (initialize + tools/list 200). The transient 500 seen earlier was not a persistent issue. Real Bifrost tool names were fixed in `cd046b8` (fictional names caused discovery bloat).
- **`GOOGLE_CLOUD_AGENT_ENGINE_ID` is a RESERVED env prefix** — cannot be set via `--update-env-vars` (platform injects it). Do not try.
- **Firestore fix (`b9b583e`):** `firestore_tools.py` now lazily creates the client via `_get_db()` (was import-time `firestore.Client()` which blocked app-load). This fixed the *initial* single-session hang; the concurrency wedge is separate.

## Do NOT redo (dead ends already ruled out)
- Bifrost is not the cause. Missing `AGENT_ENGINE_ID` env is not settable and not the cause. Dependency/py-version conflicts are resolved. `.requirements.txt` poisoning is resolved. Payload-size is resolved.

## Repo state
- Branch `ado-200001`, HEAD `126489f`. Not pushed. All fixes committed.
- Untracked scratch present (bifrost_catalog.txt, rc-stream.jsonl, docs demo html, tests/channel-batch) — gcloudignored; ignore.
- Eval harness: `tests/eval/run_agent_eval.sh` (captures perf + quality + per-agent tool histograms). Ground truth: `tests/eval/ground-truth/`. Scenarios: `tests/eval/scenarios.md`.
- Deferred work (was the ORIGINAL task before deploy plumbing consumed the session): the **efficiency loop** to reduce tool-call bloat (workflow `efficiency_loop.workflow.js`, left behind in the retired POC repo — it hardcoded that repo's paths) and category decision-trees. Do these AFTER reliability is met.
