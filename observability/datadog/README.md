# IR agent Datadog config

Three JSON files and one PowerShell script. This replaces the Terragrunt units
that used to live under
`infrastructure/test/global/incident_response_agent/datadog/`.
Those are deleted. There is no Terraform for IR agent Datadog config any more.

| File | Datadog object | Endpoint |
|---|---|---|
| `dashboard_ir_agent.json` | dashboard `yyy-040-zzz` | `POST /api/v1/dashboard`, `PUT /api/v1/dashboard/{id}` |
| `monitors_ir_agent.json` | 5 monitors, 21633848-21633852 | `POST /api/v1/monitor`, `PUT /api/v1/monitor/{id}` |
| `spans_metrics_ir_agent.json` | 9 span-based metrics | `POST /api/v2/apm/config/metrics`, `PATCH .../{metric_id}` |
| `Apply-DatadogConfig.ps1` | applies all three, idempotently | |

API references:
[dashboards](https://docs.datadoghq.com/api/latest/dashboards/),
[monitors](https://docs.datadoghq.com/api/latest/monitors/),
[spans metrics](https://docs.datadoghq.com/api/latest/spans-metrics/).

## Why not Terraform

`InfrastructureAsCode/datadog_root.hcl` pins the datadog provider org-wide at
`~>3.23.0`, which has neither a `datadog_spans_metric` resource nor an
`llm_observability` data source. The only Terraform route was a per-unit
override to `~>4.19` -- two provider versions in one repo. Rejected. The REST
API has every endpoint at one version.

## Running it

Credentials come from environment variables and are never in these files. The
keys that work are `mcpgw-datadog-{api,app}-key` in the target project; the app
key resolves to `aiops-mcp-registry-app-key` (FULL scope). `aiops-readonly`
has read scopes only and every write fails.

```powershell
$env:DD_API_KEY = gcloud secrets versions access latest `
    --secret ir-agent-datadog-api-key --project aiops-test-project
$env:DD_APP_KEY = gcloud secrets versions access latest `
    --secret mcpgw-datadog-app-key --project aiops-test-project

.\Apply-DatadogConfig.ps1 -WhatIf                 # dry run, all three units
.\Apply-DatadogConfig.ps1 -WhatIf -Only monitors  # one unit
.\Apply-DatadogConfig.ps1                         # apply
```

`-WhatIf` sends no write request. It still issues GETs, because that is what the
field-level diff is computed from.

## Idempotence and identity

Every object is read before it is written; an existing one is updated in place.
Each JSON entry carries a `_datadog_id` naming the live object it owns, because
**names are not unique in this org** -- dev and test each have a dashboard and a
monitor set for the IR agent under near-identical names. Without the pinned id a
name lookup would be ambiguous, and the script refuses an ambiguous match rather
than guessing. On a create it prints the new id and tells you to write it back.

Comparison is subset semantics for objects: Datadog fills in its own defaults
(`include_tags`, `new_host_delay`, `require_full_window`, ...), and an exact
match would report drift forever. Arrays and scalars compare exactly.

## Things that will bite you

**`_`-prefixed keys are comments.** JSON has no comment syntax, and these files
carry the reasoning the `.hcl` files used to. The script strips every `_*` key
before anything goes on the wire.

**Span-metric PATCH is much narrower than POST.** Per the
[v2 spec](https://docs.datadoghq.com/resources/json/full_spec_v2.json)
(`SpansMetricUpdateAttributes`), an update accepts only
`compute.include_percentiles`, `filter` and `group_by`. `aggregation_type` and
`compute.path` are immutable -- changing either needs a delete and recreate,
which discards the metric's history. The script detects that and refuses.

**`renotify_interval: 0` cannot be declared.** Datadog treats 0 as "never" and
drops the key entirely, so declaring it guarantees permanent phantom drift. Two
monitors omit it and say so.

**Datadog reclassifies some `metric alert` monitors as `query alert`** and
reports the stored type back. The script treats them as equivalent.

## Data sources -- the thing to get right

A query against the wrong store returns an **empty widget, not an error**.

- `bifrost.*` -- `env`, `virtual_key_name`. Real billed money. Nothing about
  incidents or sub-agents. The tag key is `virtual_key_name`; `virtual_key`
  exists but is unpopulated.
- `ml_obs.*` -- `env`, `error`, `ml_app`, `model_name`, `model_provider`,
  `service`, `span_kind`. No per-run or per-sub-agent dimension. Cost is in
  **nanodollars**.
- APM spans -- `service`, `env`, `operation_name`, `resource_name`, and the
  custom `iragent.*` tags. Not `ml_app`, not `span_kind`, not `@evaluation.*`.
- `ir_agent.*` -- the span-based metrics in this directory. Only the tags their
  own `group_by` declares, plus `env`. No `ml_app`.

Tag keys are lowercase `iragent.` because
[Datadog normalizes them that way](https://docs.datadoghq.com/getting_started/tagging/).
The `@` prefix is span-attribute search syntax, not part of the key.

## Known gaps

**Evaluations are unreachable.** `verification_bridge.py` submits four
trace-scoped labels. `ml_obs.evaluation` returns zero series org-wide and the
spans index has no `@evaluation.*` facet, so no widget is built for them -- an
empty correctness panel reads as "nothing failed". Fixing it means emitting them
as a custom metric. The dashboard carries a note saying exactly this.

**Two sub-agent columns depend on code that does not exist yet.** The `Tool
calls` column and `ir_agent.sub_agent.tool_calls` are written against a span
field `iragent.tool_calls` that `dd_run_plugin.py` does not emit. Separately,
`iragent.incident_id` and `iragent.invocation_id` are tagged on the **root span
only**, and span tags do not inherit to children, so the per-run sub-agent table
stays empty until they are also tagged on the `ir.sub_agent` span. If either
name ships differently, fix it in the JSON rather than substituting another
source -- in particular do not use `ml_obs.span{span_kind:tool}`, which has no
`sub_agent` dimension.

**Dev is still on Terraform.** `infrastructure/dev/global/datadog/`
still manages the dev dashboard `abc-050-def` and monitors 21577973-21577978.
Different objects, left alone.

## Retired Terraform

There is no Terraform for the IR agent's Datadog config any more. Do not add any
back. Three Terragrunt units used to live in
`infrastructure/test/global/incident_response_agent/datadog/`:
`dashboard_ir_agent`, `monitors_ir_agent`, and `spans_metrics_ir_agent`. All three
are replaced by the JSON files in this directory.

The whole directory is deleted rather than left with a marker file: the IaC PR
build treats any changed path under it as a live module and runs
`terragrunt plan` there, which fails when no `terragrunt.hcl` exists.
