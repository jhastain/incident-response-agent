#!/usr/bin/env python3
"""Run the IR Agent IN-PROCESS (no deploy) for fast accuracy iteration.

Builds root_agent + InMemoryRunner locally, streams one alert, and writes the same
live-alertNN.json shape that judge_from_files.py reads (report_text, etc.). Secrets are
loaded from GCP Secret Manager into env before importing the agent, and findings are
captured from session.state after the run so the judge sees the raw evidence.

Usage: local_run.py <alert_file> <out_json>
Requires: gcloud ADC, secrets IR_agent_*/incident-response-agent-bfvk readable.

Secret STORAGE is aiops-dev-project (SECRET_PROJECT overrides) — verified
2026-07-30, aiops-prod-project holds only Bifrost's own infra secrets and no
IR-agent VK. That is independent of which GATEWAY the VK points at: the agent
talks to prod Bifrost (llm-gateway.internal.example), which is also the only route
to the Opus tier — BOTH GCP projects have zero Vertex quota for claude-opus-5
and claude-opus-4-8 (persistent 429 in us-east5, hard 400 "not servable" in
us-central1).
"""
import asyncio
import json
import os
import subprocess
import sys
import time

SECRET_PROJECT = os.environ.get("SECRET_PROJECT", "aiops-dev-project")

# .env (gitignored) is the preferred source: it survives across processes, unlike
# env vars exported in an interactive shell, and avoids a gcloud call per run.
_ENV_FILE = os.path.join(os.path.dirname(__file__), "..", "..", ".env")
if os.path.exists(_ENV_FILE):
    for _line in open(_ENV_FILE, encoding="utf-8"):
        _line = _line.strip()
        if not _line or _line.startswith("#") or "=" not in _line:
            continue
        _k, _v = _line.split("=", 1)
        os.environ.setdefault(_k.strip(), _v.strip().strip('"').strip("'"))


def _load_secret(name):
    """Read a secret, preferring the API over the CLI.

    The CLI path needs a live `gcloud auth login` session, which expires
    independently of ADC. When it lapses, `gcloud secrets versions access`
    fails and the old code returned "" — so BIFROST_API_TOKEN was silently
    empty and the whole run executed with Datadog/Sentry/ADO unreachable while
    still producing a confident-looking report. That is the worst possible
    failure shape for an accuracy harness: a degraded run that looks normal.
    Observed 2026-07-31: tool calls dropped 33 -> 19 with no error surfaced.

    The Secret Manager API uses ADC, which is what everything else in this
    project already relies on. The CLI stays as a fallback.
    """
    try:
        from google.cloud import secretmanager
        client = secretmanager.SecretManagerServiceClient()
        path = f"projects/{SECRET_PROJECT}/secrets/{name}/versions/latest"
        return client.access_secret_version(request={"name": path}).payload.data.decode().strip()
    except Exception as api_error:  # fall back to the CLI
        import shutil
        exe = shutil.which("gcloud") or shutil.which("gcloud.cmd")
        if not exe:
            raise RuntimeError(
                f"cannot read secret {name}: Secret Manager API failed "
                f"({type(api_error).__name__}: {api_error}) and gcloud is not on PATH"
            ) from api_error
        result = subprocess.run(
            [exe, "secrets", "versions", "access", "latest", "--secret", name,
             "--project", SECRET_PROJECT],
            capture_output=True, text=True,
        )
        value = result.stdout.strip()
        if not value:
            raise RuntimeError(
                f"cannot read secret {name}: API failed ({type(api_error).__name__}) "
                f"and gcloud returned nothing (rc={result.returncode}): "
                f"{result.stderr.strip()[:200]}"
            ) from api_error
        return value


# Secrets MUST be in env before importing the agent (config reads them at import).
# Prefer already-set env (fast); fall back to Secret Manager.
# A missing credential is FATAL, not degraded: a run with no Bifrost token reaches
# no Datadog, Sentry, or ADO tool, and measuring accuracy on that is measuring
# nothing. Fail loudly here rather than 5 minutes later in a plausible report.
# dev's containers predate the naming standard and keep the IR_agent_* names;
# test/prod use lowercase-hyphenated (PR 109300). Pick by whichever project we read.
_dev_names = SECRET_PROJECT == "aiops-dev-project"
for env_name, sec_name in [
    ("BIFROST_API_TOKEN", os.environ.get("BIFROST_VK_SECRET", "incident-response-agent-bfvk")),
    ("IR_AGENT_ADO_READONLY",
     "IR_agent_ado_readonly" if _dev_names else "ir-agent-ado-readonly"),
    ("IR_AGENT_OCTOPUS_TASK_WATCHER",
     "IR_agent_octopus_task_watcher" if _dev_names else "ir-agent-octopus-task-watcher"),
]:
    if not os.environ.get(env_name):
        os.environ[env_name] = _load_secret(sec_name)
    if not os.environ.get(env_name):
        raise RuntimeError(f"{env_name} is empty — refusing to run with degraded tools")
os.environ.setdefault("GCP_MCP_ENABLED", "false")
os.environ.setdefault("GOOGLE_CLOUD_LOCATION", "us-central1")

# GOOGLE_CLOUD_PROJECT selects the Firestore/GCS store this run WRITES to
# (firestore_tools._get_db, gcs_tools). It used to default to
# aiops-prod-project (ADO-200022): an accuracy iteration with the var unset
# wrote synthetic incident documents into the PROD `incidents` collection and
# summaries into the prod bucket, and `find_similar_incidents` would then serve
# those back as precedent. Prod's collection is empty, so the first such run
# would not merely pollute the corpus — it would create it.
#
# No default. This is a write path selecting an environment; there is no value
# that is safe to guess. Same shape as the BIFROST_API_TOKEN check above:
# fail here, in one second, rather than five minutes later in a plausible report
# that landed in the wrong store.
if not os.environ.get("GOOGLE_CLOUD_PROJECT"):
    raise RuntimeError(
        "GOOGLE_CLOUD_PROJECT is not set — refusing to run. This run WRITES incidents "
        "to Firestore and GCS in whichever project this names, so guessing is worse "
        "than not starting. Set it explicitly, e.g. "
        "GOOGLE_CLOUD_PROJECT=aiops-dev-project"
    )

print(
    f"local_run target: project={os.environ['GOOGLE_CLOUD_PROJECT']} "
    f"location={os.environ['GOOGLE_CLOUD_LOCATION']} secrets={SECRET_PROJECT} "
    f"bifrost={os.environ.get('BIFROST_LLM_URL', '<unset>')}",
    flush=True,
)

from google.adk.runners import InMemoryRunner  # noqa: E402
from google.genai import types  # noqa: E402

from app.agent import root_agent  # noqa: E402

APP = "ir-local"
USER = "local-eval"


async def run_one(alert_text: str) -> dict:
    runner = InMemoryRunner(agent=root_agent, app_name=APP)
    session = await runner.session_service.create_session(app_name=APP, user_id=USER)
    msg = types.Content(role="user", parts=[types.Part(text=alert_text)])
    report_text = verif_text = classif_text = ""
    tools = 0
    t0 = time.time()
    async for event in runner.run_async(user_id=USER, session_id=session.id, new_message=msg):
        author = getattr(event, "author", "") or ""
        content = getattr(event, "content", None)
        for part in (getattr(content, "parts", None) or []):
            if getattr(part, "function_call", None):
                tools += 1
            t = getattr(part, "text", None)
            if t:
                if author == "report_agent" and len(t) > len(report_text):
                    report_text = t
                elif author == "verification_agent" and len(t) > len(verif_text):
                    verif_text = t
                elif author == "alert_classifier":
                    classif_text = t
    wall = round(time.time() - t0, 1)

    # pull findings from final session state so the judge sees raw evidence
    sess = await runner.session_service.get_session(app_name=APP, user_id=USER, session_id=session.id)
    state = dict(sess.state or {})
    findings = {k: state[k] for k in state if k.startswith("findings_")}
    # The classifier's STREAMED text is emitted before its after_agent_callback
    # runs, so it never shows the epoch-param timestamp override (or any other
    # post-hoc annotation). Reading state is the only way to see what the rest of
    # the pipeline actually consumed. Keeping both makes the difference visible
    # instead of silently reporting the pre-callback value as final.
    classification_state = state.get("classification")
    if hasattr(classification_state, "model_dump"):
        classification_state = classification_state.model_dump()

    return {
        "wall_seconds": wall,
        "tools": tools,
        "report_present": bool(report_text),
        "report_text": report_text[:8000],
        "verification_text": verif_text[:2000],
        "classification": classification_state,
        "classification_text": classif_text[:1500],
        "synthesis": str(state.get("synthesis", ""))[:6000],
        "tier2_findings": str(state.get("tier2_findings", ""))[:4000],
        "findings": {k: str(v)[:6000] for k, v in findings.items()},
    }


def main():
    alert_file, out_json = sys.argv[1], sys.argv[2]
    alert = open(alert_file, encoding="utf-8").read().replace("\r", " ").strip()
    res = asyncio.run(run_one(alert))
    res["alert_file"] = alert_file
    with open(out_json, "w", encoding="utf-8") as f:
        json.dump(res, f, indent=2, default=str)
    print(f"LOCAL {os.path.basename(alert_file)} wall={res['wall_seconds']}s "
          f"report={res['report_present']} tools={res['tools']} -> {out_json}")


if __name__ == "__main__":
    main()
