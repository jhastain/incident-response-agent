"""Azure DevOps pipeline (Build) investigation tools for the IR Agent.

Direct ADO REST client (like the direct GCP tools in gcp_tools.py) — the Bifrost
`azure_devops_token` server exposes only work-item/repo tools, NOT pipelines/build
logs, so mechanism-level CI-pipeline root cause (WHY a build failed) is unreachable
through Bifrost. These functions close that gap.

Auth: read-only ADO PAT from env IR_AGENT_ADO_READONLY (mounted from the GCP secret
ir-agent-ado-readonly in test/prod, IR_agent_ado_readonly in dev, via the deploy
--secrets flag). Read-only — never mutates ADO.

All functions return JSON-serialized strings (ADK requirement) and are capped.
"""

import base64
import json
import os

import httpx

_ORG = "https://dev.azure.com/acme"
_API = "api-version=7.1"
_MAX_OUTPUT_CHARS = 4_000
_TIMEOUT = 25.0


def _cap(s: str) -> str:
    if len(s) <= _MAX_OUTPUT_CHARS:
        return s
    return s[:_MAX_OUTPUT_CHARS] + f"\n... [TRUNCATED — {len(s):,} chars]"


def _auth_header() -> dict | None:
    pat = os.environ.get("IR_AGENT_ADO_READONLY", "")
    if not pat:
        return None
    token = base64.b64encode(f":{pat}".encode()).decode()
    return {"Authorization": f"Basic {token}"}


def _get(client: httpx.Client, url: str):
    r = client.get(url)
    r.raise_for_status()
    return r


_PROJECTS = ["AcmeProjects", "AcmeInfrastructure"]


def get_failed_pipeline_builds(
    project: str = "",
    pipeline_name: str = "",
    alert_time: str = "",
    window_hours: int = 1,
    top: int = 8,
) -> str:
    """List FAILED Azure DevOps pipeline builds around the alert time.

    Use this to find failing CI/CD pipeline runs (the mechanism lives in the build
    timeline + logs, fetched by get_pipeline_failure_detail). ALWAYS pass alert_time so
    the search is anchored to when the incident happened — a build that failed hours ago
    will be missed otherwise. Resolved/historical alerts are still investigable this way
    because ADO retains failed builds.

    Args:
        project: ADO project name. Leave EMPTY to search both AcmeProjects and
            Infrastructure (recommended when unsure — most CI pipelines like
            Teardown-Env / Ephemeral-Smoke-Tests live in AcmeProjects).
        pipeline_name: Optional build-definition name to filter (e.g. "Teardown-Env").
        alert_time: ISO8601 alert timestamp (e.g. "2026-07-15T11:06:48Z"). The search
            window is [alert_time - window_hours, alert_time + 15min]. If empty, falls
            back to the most recent failed builds (only correct for currently-active
            incidents).
        window_hours: Hours to look back before alert_time. Default 1.
        top: Max builds to return. Default 8.

    Returns:
        JSON string: list of {id, project, definition, result, finishTime, sourceBranch}.
        Feed an `id`+`project` to get_pipeline_failure_detail for the root cause.
    """
    headers = _auth_header()
    if not headers:
        return json.dumps({"error": "IR_AGENT_ADO_READONLY not set"})

    time_filter = ""
    if alert_time:
        try:
            import datetime as _dt
            t = _dt.datetime.fromisoformat(alert_time.replace("Z", "+00:00"))
            lo = (t - _dt.timedelta(hours=window_hours)).strftime("%Y-%m-%dT%H:%M:%SZ")
            hi = (t + _dt.timedelta(minutes=15)).strftime("%Y-%m-%dT%H:%M:%SZ")
            time_filter = f"&minTime={lo}&maxTime={hi}"
        except (ValueError, TypeError):
            time_filter = ""

    projects = [project] if project else _PROJECTS

    def _pack(b, proj):
        bid = b.get("id")
        return {
            "id": bid,
            "project": proj,
            "definition": b.get("definition", {}).get("name"),
            "result": b.get("result"),
            "finishTime": b.get("finishTime"),
            "sourceBranch": b.get("sourceBranch"),
            # Clickable build results page (cite this as the source in the report).
            "url": f"{_ORG}/{proj.replace(' ', '%20')}/_build/results?buildId={bid}&view=results" if bid else None,
        }

    try:
        with httpx.Client(headers=headers, timeout=_TIMEOUT) as client:
            all_failed = []  # every failed build in the window, across projects
            for proj in projects:
                url = (f"{_ORG}/{proj.replace(' ', '%20')}/_apis/build/builds"
                       f"?resultFilter=failed&$top={top}&{_API}"
                       f"&queryOrder=finishTimeDescending{time_filter}")
                try:
                    builds = _get(client, url).json().get("value", [])
                except httpx.HTTPStatusError:
                    continue
                all_failed.extend(_pack(b, proj) for b in builds)

            matched = all_failed
            fallback_used = False
            if pipeline_name:
                # A Datadog CI monitor fires on a pipeline/stage name (e.g.
                # "Ephemeral-Deploy") but the ADO Build API records child builds under
                # different DEFINITION names (Ephemeral-Smoke-Tests, Ephemeral-Solution-Tests,
                # Ephemeral-Jobs, ...). Match on any shared token/stem, not exact/substring.
                pn = pipeline_name.lower()
                stems = [s for s in pn.replace("_", "-").split("-") if len(s) >= 3]
                def _hit(name):
                    n = (name or "").lower()
                    if pn in n or n in pn:
                        return True
                    return any(s in n for s in stems)  # shared stem (e.g. "ephemeral")
                matched = [b for b in all_failed if _hit(b["definition"])]
                # FALLBACK: name matched nothing, but builds DID fail in the window ->
                # return them all so synthesis has real evidence to reason over instead
                # of concluding "no failures / aged out" (which was scoring 32-42).
                if not matched and all_failed:
                    matched = all_failed
                    fallback_used = True

            out = matched[:top]
            if fallback_used and out:
                out[0] = {**out[0], "_note": (
                    f"No build named ~'{pipeline_name}' in window; returning ALL "
                    f"failed builds in the window (Datadog CI pipeline name often "
                    f"differs from the ADO build-definition name).")}
            return _cap(json.dumps(out, default=str))
    except Exception as e:
        return json.dumps({"error": f"get_failed_pipeline_builds {type(e).__name__}: {e}"})


def get_pipeline_failure_detail(project: str, build_id: int) -> str:
    """Get the ROOT-CAUSE detail for one failed ADO build: which task/step failed and
    the error text from that step's log.

    This is the mechanism-level evidence for CI/CD pipeline alerts — it returns the
    actual failure message (e.g. "Packer build failed: googleapi Error 503"), not just
    that the pipeline failed.

    Args:
        project: ADO project name (e.g. "Infrastructure").
        build_id: The build id from get_failed_pipeline_builds.

    Returns:
        JSON string: {build_id, error_issues:[...], failed_steps:[{type,name}],
        error_log_tail}. error_issues are the ACTUAL error messages ADO recorded
        (issues[].type==error) across every timeline record — this is where the real
        mechanism lives (e.g. "job ran longer than the maximum time of 60 minutes",
        "Bash exited with code 1", "googleapi Error 503"). error_log_tail is the tail
        of the most relevant failed step's log for extra context.
    """
    headers = _auth_header()
    if not headers:
        return json.dumps({"error": "IR_AGENT_ADO_READONLY not set"})
    base = f"{_ORG}/{project.replace(' ', '%20')}/_apis/build/builds/{build_id}"
    try:
        with httpx.Client(headers=headers, timeout=_TIMEOUT) as client:
            records = _get(client, f"{base}/timeline?{_API}").json().get("records", [])

            # 1) The real error text: issues[].type=='error' on ANY record (Task/Job/
            #    Phase/Stage). ADO puts the mechanism here — dedup, preserve order.
            error_issues, seen = [], set()
            for r in records:
                for i in (r.get("issues") or []):
                    if i.get("type") == "error":
                        msg = f"[{r.get('name','')[:45]}] {i.get('message','')}"
                        key = i.get("message", "")[:80]
                        if key not in seen:
                            seen.add(key)
                            error_issues.append(msg[:300])
            error_issues = error_issues[:12]

            # 2) Failed steps (any type) for structure.
            failed_steps = [
                {"type": r.get("type"), "name": r.get("name")}
                for r in records if r.get("result") == "failed"
            ][:12]

            # 3) Log tail: prefer a failed record (any type) that HAS a log; fall back
            #    to any record with a log among the failed set.
            log_tail = ""
            failed_with_log = [
                r for r in records
                if r.get("result") == "failed" and (r.get("log") or {}).get("id")
            ]
            # Prefer the leaf Task log (real step output / exception) over Phase/Job/Stage
            # container logs, whose tail is only the "Prepare job" section — not the failing
            # command's error. ADO returns the Phase record before the Task record, so a
            # naive [0] grabs the wrong log and looks "truncated at Prepare job".
            failed_with_log.sort(key=lambda r: 0 if r.get("type") == "Task" else 1)
            log_id = failed_with_log[0]["log"]["id"] if failed_with_log else None
            if log_id is not None:
                raw = _get(client, f"{base}/logs/{log_id}?{_API}").text
                log_tail = "\n".join(raw.splitlines()[-30:])
                if len(log_tail) > _MAX_OUTPUT_CHARS - 800:
                    log_tail = "... [earlier log truncated]\n" + log_tail[-(_MAX_OUTPUT_CHARS - 800):]

            return json.dumps(
                {
                    "build_id": build_id,
                    "url": f"{_ORG}/{project.replace(' ', '%20')}/_build/results?buildId={build_id}&view=logs",
                    "error_issues": error_issues,
                    "failed_steps": failed_steps,
                    "error_log_tail": log_tail,
                },
                default=str,
            )
    except httpx.HTTPStatusError as e:
        return json.dumps({"error": f"get_pipeline_failure_detail HTTP {e.response.status_code}"})
    except Exception as e:
        return json.dumps({"error": f"get_pipeline_failure_detail {type(e).__name__}: {e}"})
