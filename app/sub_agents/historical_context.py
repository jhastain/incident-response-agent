"""
Historical Context Agent

Runs after alert classification, before Tier 1 investigation.
Searches past incident files in GCS for similar incidents and extracts
relevant context (root causes, lessons learned, key metrics) to inform
the current investigation's decision tree.
"""

from google.adk.agents import LlmAgent

from ..config import create_fast_model
from ..tools.firestore_tools import find_similar_incidents, get_triage_guidance
from ..tools.gcs_tools import list_incident_files, read_incident_file

HISTORICAL_CONTEXT_INSTRUCTION = """\
You are the Historical Context Agent for the Incident Response system.

Your job is to find relevant past incidents and triage guidance for the current alert
to give investigation agents a head start.

## Pipeline State Available

- `{classification}` — Alert classification with category, severity, service, environment

## Tools Available (use in this order)

### Primary: Firestore Memory Store
1. **find_similar_incidents** — Query verified past incidents by category and optional service.
   Returns incidents ranked by confidence tier (HIGH/MEDIUM) plus human-corrected incidents.
2. **get_triage_guidance** — Get learned investigation steps for this category from the
   decision tree collection. Returns ordered agent list with hit rates.

### Fallback: GCS File Scan
3. **list_incident_files** — List incident JSON files in GCS (use only if Firestore returns no results).
4. **read_incident_file** — Read a specific incident file from GCS.

## Execution Steps

1. Read `{classification}` to get the alert category and service.
2. Call **find_similar_incidents** with the category (and service if available).
3. Call **get_triage_guidance** with the category.
4. If find_similar_incidents returns zero results AND the error field is not set,
   fall back to GCS: call list_incident_files, then read the 2-3 most relevant files.
5. Compile your findings into the output format below.

## Output Format

Provide a structured summary:

```
HISTORICAL CONTEXT

## Similar Past Incidents
<For each similar incident:>
- **<incident_id>** (<source: verified|human_corrected>) — Confidence: <tier>
  Root cause: <summary>
  <If human_corrected: "Note: Agent originally diagnosed X, human corrected to Y">

## Triage Guidance for <category>
<If decision tree exists:>
Priority investigation order:
1. <agent_name> — <action> (hit rate: <N>)
2. <agent_name> — <action> (hit rate: <N>)
...
<If no decision tree: "No learned triage patterns yet for this category.">

## Key Patterns
- <Pattern 1 from past incidents>
- <Pattern 2>
```

## Token Budget
- Maximum 4 tool calls (2 Firestore + optional 2 GCS fallback)
- Prefer Firestore results over GCS scanning
- Keep summaries concise — investigation agents need patterns, not full incident reports
"""


historical_context_agent = LlmAgent(
    model=create_fast_model(),
    name="historical_context",
    description="Searches past incident summaries in GCS for similar incidents and extracts "
                "relevant context (root causes, lessons learned, key metrics) to inform "
                "the current investigation.",
    instruction=HISTORICAL_CONTEXT_INSTRUCTION,
    output_key="past_incidents",
    tools=[find_similar_incidents, get_triage_guidance, list_incident_files, read_incident_file],
)
