"""Octopus Deploy investigation tools for the IR Agent.

Direct Octopus REST client — the deploy-hang root cause (which step is stuck and why)
lives in Octopus task details, which no other wired source reaches.

Auth: Octopus API key from env IR_AGENT_OCTOPUS_TASK_WATCHER (mounted from the GCP
secret ir-agent-octopus-task-watcher in test/prod, IR_agent_octopus_task_watcher in
dev, via the deploy --secrets flag). Read-only usage — never triggers or cancels
deployments.

All functions return JSON-serialized strings (ADK requirement) and are capped.
"""

import json
import os

import httpx

_BASE = "https://acme.octopus.app"
_MAX_OUTPUT_CHARS = 4_000
_TIMEOUT = 25.0


def _cap(s: str) -> str:
    if len(s) <= _MAX_OUTPUT_CHARS:
        return s
    return s[:_MAX_OUTPUT_CHARS] + f"\n... [TRUNCATED — {len(s):,} chars]"


def _headers() -> dict | None:
    key = os.environ.get("IR_AGENT_OCTOPUS_TASK_WATCHER", "")
    return {"X-Octopus-ApiKey": key} if key else None


def find_deploy_tasks(name_filter: str = "Deploy", take: int = 5) -> str:
    """Find recent Octopus deployment tasks (optionally filtered by name).

    Use this to locate the ServerTasks-NNNN id for a deploy referenced by an alert
    (e.g. an Octopus 'Incomplete tasks over 10 minutes' card), then pass the id to
    get_deploy_task_detail for the stuck/failed step.

    Args:
        name_filter: Substring to match task descriptions (default "Deploy").
        take: Max tasks to return. Default 5.

    Returns:
        JSON string: list of {id, state, description, duration, hasPendingInterruptions}.
    """
    headers = _headers()
    if not headers:
        return json.dumps({"error": "IR_AGENT_OCTOPUS_TASK_WATCHER not set"})
    try:
        with httpx.Client(headers=headers, timeout=_TIMEOUT) as client:
            r = client.get(f"{_BASE}/api/tasks", params={"take": take, "name": name_filter})
            r.raise_for_status()
            items = r.json().get("Items", [])
            out = [
                {
                    "id": t.get("Id"),
                    "state": t.get("State"),
                    "description": (t.get("Description") or "")[:90],
                    "duration": t.get("Duration"),
                    "hasPendingInterruptions": t.get("HasPendingInterruptions"),
                    # Octopus returns the browsable path in Links.Web, already
                    # carrying the server-derived space (e.g. /app#/Spaces-1/...).
                    # Copy it; never assemble one — the space is per-task.
                    "task_url": (
                        f"{_BASE}{(t.get('Links') or {}).get('Web')}"
                        if (t.get("Links") or {}).get("Web")
                        else None
                    ),
                }
                for t in items[:take]
            ]
            return _cap(json.dumps(out, default=str))
    except httpx.HTTPStatusError as e:
        return json.dumps({"error": f"find_deploy_tasks HTTP {e.response.status_code}"})
    except Exception as e:
        return json.dumps({"error": f"find_deploy_tasks {type(e).__name__}: {e}"})


def get_deploy_task_detail(task_id: str) -> str:
    """Get the ROOT-CAUSE detail for one Octopus deploy task: per-step state and
    duration, surfacing the step that is stuck (Running/long) or Failed.

    This is the mechanism-level evidence for deploy-hang alerts — it identifies WHICH
    step is blocking the deployment and for how long, plus any failure/interruption.

    Args:
        task_id: Octopus task id, e.g. "ServerTasks-1000002" (from the alert or
            find_deploy_tasks).

    Returns:
        JSON string: {task_id, state, problem_steps:[{name,status,duration,errors}],
        interruptions}. problem_steps lists Failed/Running steps (the hang point) WITH
        their actual error messages (the mechanism, e.g. "polling endpoint did not
        collect the request" = Tentacle unreachable), pulled from verbose logs.
    """
    headers = _headers()
    if not headers:
        return json.dumps({"error": "IR_AGENT_OCTOPUS_TASK_WATCHER not set"})
    try:
        with httpx.Client(headers=headers, timeout=_TIMEOUT) as client:
            tr = client.get(f"{_BASE}/api/tasks/{task_id}")
            tr.raise_for_status()
            task = tr.json()
            # verbose=true returns LogElements per node -> the actual error text.
            dr = client.get(f"{_BASE}/api/tasks/{task_id}/details", params={"verbose": "true"})
            dr.raise_for_status()
            detail = dr.json()

            problem = []

            def walk(node):
                st = node.get("Status")
                if st in ("Failed", "Running"):
                    errs = [
                        (l.get("MessageText") or "")[:160]
                        for l in (node.get("LogElements") or [])
                        if l.get("Category") in ("Error", "Fatal")
                    ][:3]
                    problem.append({
                        "name": (node.get("Name") or "")[:60],
                        "status": st,
                        "duration": node.get("Duration"),
                        "errors": errs,
                    })
                for c in node.get("Children") or []:
                    walk(c)

            for a in detail.get("ActivityLogs", []) or []:
                walk(a)

            out = {
                "task_id": task_id,
                "state": task.get("State"),
                "description": (task.get("Description") or "")[:90],
                "duration": task.get("Duration"),
                # The browsable task page. Octopus builds Links.Web itself and embeds
                # the space it resolved for THIS task, so copying it is always
                # space-correct; assembling `/app#/Spaces-1/...` by hand is a guess.
                # Links.Self / Links.Raw are API endpoints — never ship those.
                "task_url": (
                    f"{_BASE}{(task.get('Links') or {}).get('Web')}"
                    if (task.get("Links") or {}).get("Web")
                    else None
                ),
                "problem_steps": problem[:10],
                "interruptions": task.get("HasPendingInterruptions"),
            }
            return _cap(json.dumps(out, default=str))
    except httpx.HTTPStatusError as e:
        return json.dumps({"error": f"get_deploy_task_detail HTTP {e.response.status_code}"})
    except Exception as e:
        return json.dumps({"error": f"get_deploy_task_detail {type(e).__name__}: {e}"})
