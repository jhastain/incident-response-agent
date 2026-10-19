# IR Agent End-to-End Testing

Reproducible E2E evaluation framework for the Incident Response Agent deployed on Vertex AI Agent Engine.

## What This Tests

The E2E framework sends fixed alert texts to the **deployed** Agent Engine (not a local runner) and evaluates:

- **Classification accuracy** — Does the agent correctly categorize the incident (category, severity, service)?
- **Root cause alignment** — Does the agent identify the same root cause as the human-verified ground truth?
- **Tier 2 dispatch correctness** — Does the agent dispatch the right conditional agents (sql_dba_agent, codebase_agent, etc.)?
- **Report completeness** — Does the final report contain all required sections?
- **Performance metrics** — Token usage, execution time, estimated cost, tool calls

## How to Run

From any Claude Code session with access to the `incident-response-agent` repo:

```
/ir-agent-e2e-test
```

The skill will:
1. Read the 3 fixed scenarios from `scenarios.md`
2. Send each to the deployed Agent Engine sequentially (~2-5 min each)
3. Parse the response stream for classification, report, and telemetry
4. Compare against ground truth in `ground-truth/`
5. Score each scenario on accuracy and performance metrics
6. Save results to `results/<YYYY-MM-DD_HHMMSS>.json`
7. Display a comparison table (with deltas against the previous run if available)

**Total runtime:** ~10-15 minutes for all 3 scenarios.

## Test Scenarios

| # | ID | Category | Tier 2 Path | Source File |
|---|---|----------|-------------|-------------|
| 1 | `cloud-infrastructure-startup-probe` | cloud-infrastructure | None (T1 only) | GCS: `2026-04-09_sev3_cloud-infrastructure_...` |
| 2 | `sql-database-preferences-blocking` | sql-database | sql_dba_agent | GCS: `2026-03-31_sev2_sql-database_...` |
| 3 | `application-errors-apigateway-mobile` | application-errors | codebase_agent | GCS: `2026-05-13_sev2_application-errors_...` |

Scenarios are defined in `scenarios.md` with verbatim alert text inputs. Ground truth is in `ground-truth/`.

## Evaluation Metrics

### Accuracy (per scenario)

| Metric | Scoring |
|--------|---------|
| Classification Accuracy | Boolean match per field (category, severity, service) |
| Root Cause Alignment | LLM-as-judge: HIGH / MEDIUM / LOW / MISS |
| Tier 2 Dispatch | Boolean — expected vs actual agents dispatched |
| Report Completeness | N/5 format checks over the four-section report (ADO-200018) |
| Telemetry Captured | Non-zero token count and >=1 tool call on the event stream |
| Confidence Calibration | Match / Over / Under vs ground truth |

### Performance (per scenario)

| Metric | Source |
|--------|--------|
| Total Tokens | `usage_metadata` from stream events |
| Wall Clock Time | Start to last event timestamp |
| Estimated Cost | Tokens x Claude Sonnet Vertex AI rate |
| Agents Dispatched | Unique agent authors in stream |
| Tool Calls | `function_call` events in stream |

## Adding New Scenarios

1. Run the agent against a new incident using `/investigate`
2. Save the resulting JSON to `gs://incident-summaries/`
3. Extract ground truth fields into a new `ground-truth/scenario-N-<category>.json`
4. Add the scenario to `scenarios.md` with the verbatim alert text
5. Update the scenario count in this README and in the SKILL.md

### Ground Truth JSON Schema

```json
{
  "scenario_id": "string",
  "category": "string",
  "severity": "string",
  "service": "string",
  "environment": "string",
  "gcp_project": "string",
  "root_cause_summary": "string",
  "root_cause_confidence": "possible|likely|confirmed",
  "tier2_agents_expected": ["string"],
  "key_findings": ["string"],
  "tags": ["string"],
  "telemetry_baseline": {
    "total_tokens": "number|null",
    "total_tool_uses": "number|null",
    "agents_dispatched": "number",
    "agents_completed": "number",
    "total_duration_ms": "number"
  }
}
```

## Results Storage

Results are saved as timestamped JSON files in `results/`:

```
results/
├── 2026-06-15_143200.json
├── 2026-06-16_091500.json
└── ...
```

Each file contains per-scenario accuracy scores, performance metrics, and a run summary. The skill automatically compares against the most recent previous result to show deltas.

## Directory Structure

```
tests/e2e/
├── README.md              # This file
├── scenarios.md           # Fixed alert text inputs (3 scenarios)
├── ground-truth/          # Expected outputs for evaluation
│   ├── scenario-1-cloud-infrastructure.json
│   ├── scenario-2-sql-database.json
│   └── scenario-3-application-errors.json
└── results/               # Historical run results (timestamped JSON)
    └── .gitkeep
```

## Prerequisites

- `gcloud` CLI authenticated with access to the project recorded in
  `deployment_metadata.json` (currently `aiops-test-project`) — the harnesses derive the
  target from that file, so authenticate against whichever environment it names
- Network access to `us-central1-aiplatform.googleapis.com`
- An active Agent Engine deployment. **No engine ID is listed here on purpose**: the ID
  changes on every redeploy, and the ID this line used to name (`1000000000000000003`) had
  been deleted from all three projects while still being documented as the active
  deployment. Every harness resolves the engine from `deployment_metadata.json` and prints
  the target it resolved; override with `IR_AGENT_ENGINE_URL` / `ENGINE_ID`.
- Claude Code with the `/ir-agent-e2e-test` skill installed
