# Plan 1: Multi-Layer Memory Architecture — IR Agent Learning System

## Objective

Build a 5-layer memory system for the IR agent that stores incident records, learned triage procedures, semantic similarity for cross-category pattern matching, live session state, and long-term organizational patterns. Each layer uses a purpose-built GCP service instead of forcing everything into a single store.

---

## Architecture Overview

```
┌─────────────────────────────────────────────────────────────────────┐
│                     IR Agent Memory Layers                         │
├──────────────┬──────────────────────────────────────────────────────┤
│ Layer        │ Service               │ Purpose                     │
├──────────────┼───────────────────────┼─────────────────────────────┤
│ 1. Episodic  │ Firestore (incidents) │ What happened + how we      │
│              │                       │ investigated (per incident) │
├──────────────┼───────────────────────┼─────────────────────────────┤
│ 2. Procedural│ Firestore             │ Learned triage steps per    │
│              │ (decision_trees)      │ category, auto-optimized    │
├──────────────┼───────────────────────┼─────────────────────────────┤
│ 3. Semantic  │ Vertex AI             │ "Find me similar past       │
│              │ Vector Search         │ incidents" via embeddings   │
├──────────────┼───────────────────────┼─────────────────────────────┤
│ 4. Session   │ Agent Engine          │ Live triage working memory  │
│              │ Sessions              │ (already in use)            │
├──────────────┼───────────────────────┼─────────────────────────────┤
│ 5. Patterns  │ Agent Engine          │ Org-level cross-incident    │
│ (future)     │ Memory Bank           │ patterns (auto-extracted)   │
└──────────────┴───────────────────────┴─────────────────────────────┘
```

### Why Multi-Layer vs Single Store

The previous plan proposed `VertexAiMemoryBankService` for everything. The multi-layer approach is better because:

| Concern | Single Store (Memory Bank) | Multi-Layer |
|---------|---------------------------|-------------|
| Structured queries | Memory Bank only supports semantic search — no query by `category = 'sql-database' AND severity = 'sev1'` | Firestore supports compound queries on any field |
| Investigation methodology | Would need to serialize methodology into memory text blob | Firestore stores methodology as structured subdocument |
| Decision tree optimization | No way to atomically update step hit rates | Firestore transactions guarantee safe concurrent updates |
| Semantic similarity | Memory Bank's LLM-extracted facts may miss symptom patterns | Vector Search with embeddings finds symptom similarity regardless of category naming |
| Cost at scale | $0.25/1K memories — but what counts as a "memory" is opaque | Firestore: $0.06/100K reads, Vector Search: $0.045/hr endpoint (can be serverless) |
| Operational control | Managed/opaque — can't inspect or fix bad data easily | Full control over schema, indexes, data lifecycle |

---

## Layer 1: Episodic Memory — `incidents` Collection (Firestore)

### What Gets Stored

Each completed incident investigation becomes a Firestore document with 5 data sections:

```python
# incidents/{incident_id}
{
    # --- Incident Details ---
    "incident_id": "2026-06-10_sev2_sql-database_webcore_preferences-blocking",
    "timestamp": "2026-06-10T14:32:00Z",  # Firestore Timestamp
    "category": "sql-database",
    "severity": "sev2",
    "service": "WebCore",
    "environment": "production",
    "gcp_project": "acme-production-100000",
    "alert_source": "pagerduty",
    "alert_text": "SQL blocking chains in CoreSvc DB...",

    # --- Triage Discoveries ---
    "discoveries": {
        "root_cause": "Feature flag 'PC - Preferences Redesign' triggered CostCentreItemsEngine batch UPDATE holding exclusive locks for 20-31s",
        "evidence_sources": ["cloud_logging", "datadog", "sentry"],
        "key_findings": [
            {"agent": "gcp_logging_agent", "finding": "584+ AppSqlException events", "confidence": "STRONG"},
            {"agent": "datadog_agent", "finding": "Query timeout on cteChangeOrders (30s)", "confidence": "STRONG"},
            {"agent": "sentry_agent", "finding": "Hangfire job errors correlated", "confidence": "MODERATE"}
        ],
        "dead_ends": [
            {"agent": "sql_dba_agent", "reason": "No direct DB access — ADO MCP tools unavailable"},
            {"agent": "deploy_correlation_agent", "reason": "No recent deploys matched timeline"}
        ],
        "investigation_path": [
            "1. alert_classifier → sql-database/sev2/WebCore (high confidence)",
            "2. gcp_logging_agent → AppSqlException spike found",
            "3. datadog_agent → Query timeout pattern identified",
            "4. sentry_agent → Hangfire errors correlated",
            "5. synthesis_agent → Feature flag timeline matches error onset",
            "6. sql_dba_agent → DEAD END (no DB access)",
            "7. Key Decision: MEDIUM confidence (lock mechanism inferred, not observed)"
        ]
    },

    # --- Proposed Solutions ---
    "solutions": {
        "immediate": "Disable feature flag, kill blocking SPIDs",
        "long_term": "Add lock timeout to CostCentreItemsEngine batch UPDATE",
        "actions": [
            {"action": "Disable PC - Preferences Redesign flag", "status": "recommended", "owner": null},
            {"action": "Kill blocking SPIDs on sqlprod-c1", "status": "recommended", "owner": null}
        ]
    },

    # --- Key Logs & Telemetry ---
    "telemetry": {
        "tool_calls": 39,
        "agents_dispatched": 14,
        "investigation_duration_seconds": 606,
        "total_tokens": 155336,
        "estimated_cost_usd": 0.85,
        "tier2_agents": ["sql_dba_agent"]
    },

    # --- Verification Status ---
    "verification": {
        "confidence": "MEDIUM",
        "root_cause_verified": false,
        "verified_by": null,
        "verified_at": null,
        "human_feedback": null,
        "quality_score": null  # 1-5, set after human review
    },

    # --- Metadata ---
    "created_at": "2026-06-10T14:42:06Z",  # Firestore server timestamp
    "updated_at": "2026-06-10T14:42:06Z",
    "schema_version": 3
}
```

### Firestore Setup

**Database**: `(default)` database in `aiops-dev-project`, Native mode, `us-central1` region

**Composite indexes** (for common query patterns):
```
# Find past incidents by category + severity, ordered by time
incidents: category ASC, severity ASC, timestamp DESC

# Find incidents by service, ordered by time
incidents: service ASC, timestamp DESC

# Find unverified incidents
incidents: verification.root_cause_verified ASC, timestamp DESC
```

### Python Client

```python
from google.cloud import firestore

db = firestore.AsyncClient(project="aiops-dev-project")

# Write incident
async def write_incident(incident_data: dict) -> str:
    doc_ref = db.collection("incidents").document(incident_data["incident_id"])
    incident_data["created_at"] = firestore.SERVER_TIMESTAMP
    incident_data["updated_at"] = firestore.SERVER_TIMESTAMP
    await doc_ref.set(incident_data)
    return doc_ref.id

# Query by category + severity
async def find_incidents(category: str, severity: str = None, limit: int = 5) -> list:
    query = db.collection("incidents").where("category", "==", category)
    if severity:
        query = query.where("severity", "==", severity)
    query = query.order_by("timestamp", direction=firestore.Query.DESCENDING).limit(limit)
    return [doc.to_dict() async for doc in query.stream()]
```

### Pricing Estimate

| Operation | Volume/Month | Unit Cost | Monthly Cost |
|-----------|-------------|-----------|-------------|
| Writes (new incidents) | ~100 | $0.18/100K | ~$0.01 |
| Reads (historical lookup) | ~1,000 (10 reads/incident) | $0.06/100K | ~$0.01 |
| Storage | ~100KB (1KB/doc × 100) | $0.18/GB/month | ~$0.01 |
| **Total** | | | **~$0.03/month** |

---

## Layer 2: Procedural Memory — `decision_trees` Collection (Firestore)

### What Gets Stored

One document per incident category containing the learned triage steps:

```python
# decision_trees/{category}
{
    "category": "sql-database",
    "version": 14,  # Incremented on each optimization
    "last_optimized": "2026-06-10T15:00:00Z",
    
    "triage_steps": [
        {
            "order": 1,
            "agent": "gcp_logging_agent",
            "action": "Search for AppSqlException and deadlock events in Cloud Logging",
            "hit_rate": 0.87,  # 87% of sql-database incidents found useful evidence here
            "avg_time_seconds": 45,
            "skip_rules": []
        },
        {
            "order": 2,
            "agent": "datadog_agent",
            "action": "Check query timeout metrics and blocking duration",
            "hit_rate": 0.72,
            "avg_time_seconds": 38,
            "skip_rules": [
                {"condition": "environment == 'development'", "reason": "Dev DBs don't have Datadog monitoring"}
            ]
        },
        {
            "order": 3,
            "agent": "sentry_agent",
            "action": "Look for correlated application errors",
            "hit_rate": 0.54,
            "avg_time_seconds": 30,
            "skip_rules": []
        },
        {
            "order": 4,
            "agent": "sql_dba_agent",
            "action": "Query sys.dm_exec_requests for active blocking chains",
            "hit_rate": 0.15,  # Low because tool access is limited
            "avg_time_seconds": 120,
            "skip_rules": [
                {"condition": "severity == 'sev3'", "reason": "Not worth Tier 2 cost for low severity"}
            ]
        }
    ],

    "outcome_patterns": [
        {
            "pattern": "AppSqlException + feature flag change",
            "frequency": 3,
            "typical_resolution": "Disable feature flag, add lock timeout",
            "last_seen": "2026-06-10T14:32:00Z"
        }
    ],

    "optimization_log": [
        {
            "version": 14,
            "timestamp": "2026-06-10T15:00:00Z",
            "changes": "Promoted gcp_logging_agent from order 2 to order 1 (hit rate increased to 0.87)",
            "incidents_analyzed": 23
        }
    ]
}
```

### Background Optimization Job

After each verified incident resolution, a Cloud Function (or scheduled job) reads the incident document, updates the relevant decision tree's step hit rates, and re-orders steps by effectiveness:

```python
from google.cloud import firestore

db = firestore.AsyncClient(project="aiops-dev-project")

async def optimize_decision_tree(category: str):
    """Re-score triage steps based on resolved incidents."""
    
    # Get all verified incidents for this category
    incidents = db.collection("incidents") \
        .where("category", "==", category) \
        .where("verification.root_cause_verified", "==", True) \
        .order_by("timestamp", direction=firestore.Query.DESCENDING) \
        .limit(50)
    
    incident_docs = [doc.to_dict() async for doc in incidents.stream()]
    
    # Calculate hit rates per agent
    agent_hits = {}
    for inc in incident_docs:
        for finding in inc.get("discoveries", {}).get("key_findings", []):
            agent = finding["agent"]
            if agent not in agent_hits:
                agent_hits[agent] = {"hits": 0, "total": 0}
            agent_hits[agent]["total"] += 1
            if finding["confidence"] in ("STRONG", "MODERATE"):
                agent_hits[agent]["hits"] += 1
    
    # Transactional update with version increment
    tree_ref = db.collection("decision_trees").document(category)
    
    @firestore.async_transactional
    async def update_in_transaction(transaction):
        tree_doc = await tree_ref.get(transaction=transaction)
        tree = tree_doc.to_dict()
        
        # Update hit rates
        for step in tree["triage_steps"]:
            agent = step["agent"]
            if agent in agent_hits:
                step["hit_rate"] = agent_hits[agent]["hits"] / agent_hits[agent]["total"]
        
        # Re-sort by hit rate (highest first)
        tree["triage_steps"].sort(key=lambda s: s["hit_rate"], reverse=True)
        for i, step in enumerate(tree["triage_steps"]):
            step["order"] = i + 1
        
        tree["version"] += 1
        tree["last_optimized"] = firestore.SERVER_TIMESTAMP
        
        transaction.update(tree_ref, tree)
    
    transaction = db.transaction()
    await update_in_transaction(transaction)
```

### How the Agent Uses Decision Trees

In stage 2 (`context_parallel`), the `historical_context` agent queries the decision tree for the classified category:

```python
async def get_triage_guidance(category: str, severity: str, environment: str) -> str:
    """Fetch learned triage steps for this incident type."""
    doc = await db.collection("decision_trees").document(category).get()
    if not doc.exists:
        return "No learned triage guidance for this category yet."
    
    tree = doc.to_dict()
    steps = []
    for step in tree["triage_steps"]:
        # Apply skip rules
        skip = False
        for rule in step.get("skip_rules", []):
            if eval_condition(rule["condition"], severity=severity, environment=environment):
                skip = True
                break
        if not skip:
            steps.append(
                f"{step['order']}. [{step['agent']}] {step['action']} "
                f"(hit rate: {step['hit_rate']:.0%}, avg {step['avg_time_seconds']}s)"
            )
    
    return f"Learned triage steps for {category} (v{tree['version']}):\n" + "\n".join(steps)
```

---

## Layer 3: Semantic Retrieval — Vertex AI Vector Search

### Purpose

Find semantically similar past incidents regardless of category naming. A Cloud Run OOM and a GKE OOM share symptoms even though they're in different categories. Vector Search finds these connections.

### How It Works

```
Incident closes → persistence_agent writes to Firestore
                → Embed (title + discovered_facts) via Vertex AI Embeddings API
                → Upsert vector + metadata to Vector Search index

New incident    → alert_classifier produces category
                → Embed alert text
                → Query Vector Search for top 3 similar incidents (with metadata filters)
                → Inject similar incidents into agent context
```

### Embedding Model

**`text-embedding-005`** (Vertex AI, 768 dimensions)

```python
from vertexai.language_models import TextEmbeddingModel

embedding_model = TextEmbeddingModel.from_pretrained("text-embedding-005")

def embed_incident(title: str, discovered_facts: str) -> list[float]:
    text = f"{title}\n\n{discovered_facts}"
    embeddings = embedding_model.get_embeddings([text])
    return embeddings[0].values  # 768-dim vector
```

### Metadata for Filtering

Each vector includes metadata so we can pre-filter before similarity search:

```python
{
    "id": "2026-06-10_sev2_sql-database_webcore",
    "embedding": [0.012, -0.034, ...],  # 768 dims
    "restricts": [
        {"namespace": "category", "allow_list": ["sql-database"]},
        {"namespace": "severity", "allow_list": ["sev2"]},
        {"namespace": "service", "allow_list": ["WebCore"]},
        {"namespace": "environment", "allow_list": ["production"]},
        {"namespace": "verified", "allow_list": ["false"]}
    ],
    "numeric_restricts": [
        {"namespace": "timestamp_epoch", "value_int": 1718028720}
    ]
}
```

**Query example**: "Find incidents similar to this alert, in production only":
```python
from google.cloud import aiplatform

index_endpoint = aiplatform.MatchingEngineIndexEndpoint(
    index_endpoint_name="projects/aiops-dev-project/locations/us-central1/indexEndpoints/{ID}"
)

response = index_endpoint.find_neighbors(
    deployed_index_id="ir-incidents-index",
    queries=[alert_embedding],
    num_neighbors=3,
    filter=[
        Namespace(name="environment", allow_tokens=["production"])
    ]
)
```

### Index Configuration

**Type**: Stream update index (real-time upserts for ~100 incidents/month)

```python
index = aiplatform.MatchingEngineIndex.create_tree_ah_index(
    display_name="ir-incidents-index",
    dimensions=768,
    approximate_neighbors_count=10,
    distance_measure_type="DOT_PRODUCT_DISTANCE",
    index_update_method="STREAM_UPDATE",
    description="IR agent incident embeddings for semantic similarity"
)
```

### Cost Considerations

| Component | Cost | Notes |
|-----------|------|-------|
| Embedding API | $0.00002/1K chars | ~$0.002/month for 100 incidents |
| Index endpoint (standard) | $0.045/hr = **~$32/month** | Always-on minimum |
| Index endpoint (autoscaling, min 0) | $0/hr when idle | **Only if available for streaming indexes** |
| Per-query | Included in endpoint cost | Negligible at 300 queries/month |

**The $32/month always-on endpoint is the main cost**. At our volume (~100 incidents/month), alternatives to consider:

| Alternative | Cost | Trade-off |
|-------------|------|-----------|
| Vector Search endpoint | ~$32/month | Full semantic search, millisecond latency |
| Firestore + keyword search | ~$0.03/month | No cross-category similarity, but covers 80% of lookups |
| Memory Bank semantic search | ~$0.03/month | Managed, but less control over embeddings and filtering |

**Recommendation**: Start with **Firestore keyword queries** (Layer 1) + **Memory Bank** (Layer 5) for semantic needs. Add Vector Search only when the corpus exceeds ~500 incidents and cross-category matching becomes critical. This saves $32/month while the system is being validated.

---

## Layer 4: Session State — Agent Engine Sessions (Already In Use)

### Current Setup

The IR agent already uses `VertexAiSessionService`:

```python
from google.adk.sessions import VertexAiSessionService

session_service = VertexAiSessionService(
    project="aiops-dev-project",
    location="us-central1"
)
```

### What Changes

**No changes to session management**. Sessions continue to store the live conversation during each triage run. The key addition is: after a session completes, the `persistence_agent` reads session events to extract investigation methodology before writing to Firestore.

### Session → Episodic Memory Pipeline

```python
async def extract_methodology_from_session(session_id: str) -> dict:
    """Parse completed session events to build investigation methodology."""
    session = await session_service.get_session(
        app_name="ir-agent",
        user_id="webhook-trigger@acme.example",
        session_id=session_id
    )
    
    methodology = {
        "investigation_path": [],
        "key_findings": [],
        "dead_ends": [],
        "tool_calls": 0,
        "agents_dispatched": set()
    }
    
    for event in session.events:
        if event.author:
            methodology["agents_dispatched"].add(event.author)
        for part in (event.content.parts if event.content else []):
            if hasattr(part, 'function_call'):
                methodology["tool_calls"] += 1
    
    return methodology
```

### Session Retention

Sessions persist in Agent Engine's managed storage. No explicit TTL — sessions remain queryable. For cost management, consider deleting sessions older than 90 days via the API.

---

## Layer 5: Cross-Incident Patterns — Memory Bank (Future)

### Purpose

After human-in-the-loop verification is built, Memory Bank stores org-level patterns the agent should remember across ALL incidents:

- "This team's deployments always cause 503s on the first revision"
- "This service has never had a database issue"
- "When you see AppSqlException on JobCostingBudget, check feature flags first"

### How Memory Bank Works

Memory Bank uses Gemini to automatically extract structured facts from session history. When `VertexAiMemoryBankService` is configured:

1. After a session ends, Memory Bank's LLM reviews the session transcript
2. It extracts key facts as individual "memories" (short text snippets)
3. These memories are stored and made searchable across all future sessions
4. At the start of a new session, relevant memories are injected into agent context

### ADK Integration

```python
from google.adk.memory import VertexAiMemoryBankService
from google.adk.tools import LoadMemoryTool

memory_service = VertexAiMemoryBankService()

# Add to historical_context agent for on-demand memory retrieval
historical_context_agent = LlmAgent(
    name="historical_context",
    tools=[LoadMemoryTool(), *existing_tools],
    instruction="""
    Use load_memory to search for organizational patterns related to this incident.
    Query examples: "sql blocking WebCore production", "Cloud Run startup probe timeout"
    Focus on patterns that inform which investigation steps to prioritize.
    """
)
```

### Why Future / Phase 2

- Requires human verification loop (Phase 3 of implementation) to avoid storing bad patterns
- Memory Bank's auto-extraction quality needs validation with real incidents
- Firestore + keyword queries cover 80% of the value immediately
- Memory Bank is additive — it can be layered on later without changing Layers 1-4

### Pricing

| Component | Cost | Notes |
|-----------|------|-------|
| Memory writes | $0.25/1K events/memories | ~100 sessions/month = ~$0.025/month |
| Memory reads | Included in Agent Engine compute | ~300 retrievals/month |

---

## Pipeline Integration

### Updated 8-Stage Pipeline

```
Stage 1: alert_classifier [COMPLETE]

Stage 2: context_parallel [COMPLETE]
  ├── infra_context [COMPLETE — GCS context files]
  ├── historical_context [COMPLETE]
  │     ├── Query Firestore incidents collection (by category + service)
  │     │   └── Includes human-corrected incidents (corrected=true, uses human root cause)
  │     ├── Query Firestore decision_trees collection (by category)
  │     └── [PLANNED] Query Memory Bank for org-level patterns
  └── [PLANNED] semantic_retrieval (NEW — Vector Search for similar incidents)

Stage 3: tier1_parallel [COMPLETE — 9 agents]
  └── [PLANNED] Agent ordering influenced by decision_tree step priorities

Stage 4: synthesis_agent [COMPLETE]
Stage 5: tier2_dispatcher [COMPLETE]
Stage 6: report_agent [COMPLETE]

Stage 7: verification_agent [COMPLETE]
  ├── Reads completed report + raw session tool outputs
  ├── Scores: diagnosis / solution / investigation efficiency
  ├── Assigns automated tier (MEDIUM / LOW / UNRELIABLE)
  ├── Uses different model than investigating agents (Gemini 2.5 Flash)
  └── Passes scores to persistence_agent

Stage 8: persistence_agent [COMPLETE]
  ├── Write incident JSON to GCS (existing, keep for backward compatibility)
  ├── Write incident document to Firestore with verification scores
  ├── [PLANNED] Embed + upsert to Vector Search index
  └── [PLANNED] Session events → Memory Bank (automatic when MemoryBankService configured)

Post-pipeline: Human feedback (via skill or Teams bot) [COMPLETE]
  ├── Required: F1 (root cause accuracy) + F2 (time impact)
  ├── Optional: F3-F7 (detail questions)
  ├── Updates Firestore human_feedback subdocument
  ├── Recalculates combined_tier
  └── If F1=WRONG: marks corrected=true, stores human ground truth
```

### New Tool Functions

```python
# app/tools/firestore_tools.py

from google.cloud import firestore

db = firestore.AsyncClient(project="aiops-dev-project")

async def write_incident_to_firestore(incident_data: dict) -> str:
    """Write completed incident analysis to Firestore."""
    doc_ref = db.collection("incidents").document(incident_data["incident_id"])
    incident_data["created_at"] = firestore.SERVER_TIMESTAMP
    incident_data["updated_at"] = firestore.SERVER_TIMESTAMP
    await doc_ref.set(incident_data)
    return f"Incident {incident_data['incident_id']} saved to Firestore"

async def find_similar_incidents(category: str, service: str = None, limit: int = 5) -> str:
    """Query past incidents by category and optionally by service."""
    query = db.collection("incidents") \
        .where("category", "==", category) \
        .order_by("timestamp", direction=firestore.Query.DESCENDING) \
        .limit(limit)
    
    if service:
        query = query.where("service", "==", service)
    
    results = []
    async for doc in query.stream():
        inc = doc.to_dict()
        results.append({
            "incident_id": inc["incident_id"],
            "timestamp": str(inc.get("timestamp", "")),
            "root_cause": inc.get("discoveries", {}).get("root_cause", "Unknown"),
            "confidence": inc.get("verification", {}).get("confidence", "Unknown"),
            "investigation_path": inc.get("discoveries", {}).get("investigation_path", []),
        })
    
    if not results:
        return f"No past incidents found for category={category}"
    
    return json.dumps(results, indent=2, default=str)

async def get_triage_guidance(category: str) -> str:
    """Fetch learned triage steps for this incident category."""
    doc = await db.collection("decision_trees").document(category).get()
    if not doc.exists:
        return f"No learned triage guidance for category '{category}' yet."
    
    tree = doc.to_dict()
    steps = []
    for step in tree.get("triage_steps", []):
        steps.append(
            f"{step['order']}. [{step['agent']}] {step['action']} "
            f"(hit rate: {step['hit_rate']:.0%})"
        )
    
    header = f"Learned triage for {category} (v{tree['version']}, "
    header += f"optimized {tree.get('last_optimized', 'never')}):"
    return header + "\n" + "\n".join(steps)

async def update_verification(incident_id: str, verified: bool, feedback: str = None) -> str:
    """Update incident verification status (human-in-the-loop)."""
    doc_ref = db.collection("incidents").document(incident_id)
    update = {
        "verification.root_cause_verified": verified,
        "verification.verified_at": firestore.SERVER_TIMESTAMP,
        "updated_at": firestore.SERVER_TIMESTAMP,
    }
    if feedback:
        update["verification.human_feedback"] = feedback
    await doc_ref.update(update)
    return f"Verification updated for {incident_id}: verified={verified}"
```

---

## Verification & Human Feedback System

### Purpose

Prevent unverified or incorrect investigations from contaminating the memory system. Two scoring tiers:
1. **Automated** — a dedicated `verification_agent` (Stage 6.5) scores every incident before persistence
2. **Human** — structured feedback collected via skill invocation (Phase 1) or Teams bot (future), overrides automated scores

**Structural guarantee**: No incident can reach HIGH tier without human confirmation. Automated scoring caps at MEDIUM.

### Verification Agent (Stage 6.5)

A new pipeline stage between `report_agent` (Stage 6) and `persistence_agent` (Stage 7). The verification agent reads the completed report AND the raw session tool outputs, then scores independently.

**Critical**: The verification agent does NOT see the report agent's self-assessed confidence. It scores from evidence only, to avoid anchoring bias.

**Model**: Uses a different model than the investigating agents (e.g., `gemini-2.5-flash` if pipeline runs on `claude-sonnet-4-6`) to reduce correlated reasoning failures.

#### Three Scoring Dimensions

| Dimension | What it evaluates | Rubric |
|-----------|------------------|--------|
| **Diagnosis** | Does cited evidence logically support the root cause claim? Are evidence citations traceable to actual tool outputs? | STRONG / PARTIAL / WEAK / UNSUPPORTED |
| **Solution** | Is the proposed fix actionable, connected to the diagnosis, and specific enough to execute? | ACTIONABLE / VAGUE / DISCONNECTED / MISSING |
| **Investigation Efficiency** | Right agents dispatched? Dead end ratio? Logical progression? Unnecessary Tier 2 dispatches? | EFFICIENT / ADEQUATE / WASTEFUL |

#### Efficiency Sub-Checks

- **Agent precision**: Were Tier 2 agents justified by the evidence, or speculatively dispatched?
- **Dead end ratio**: `dead_ends / (key_findings + dead_ends)` — high ratio = wasteful
- **Breadth**: Did at least 3 Tier 1 agents produce findings? (too few = incomplete)
- **Token efficiency**: Tokens used relative to findings produced (flags "chatty" agents)

#### Overall Tier (Automated)

| Tier | Criteria | Retrieval behavior |
|------|----------|-------------------|
| **HIGH** | Not achievable by automation alone — requires human F1 = CORRECT | — |
| **MEDIUM** | STRONG diagnosis + ACTIONABLE solution + EFFICIENT/ADEQUATE investigation | Pulled into future context |
| **LOW** | WEAK/UNSUPPORTED diagnosis OR DISCONNECTED solution | Stored but quarantined |
| **UNRELIABLE** | UNSUPPORTED diagnosis AND (DISCONNECTED or MISSING solution) | Stored but quarantined, flagged for review |

#### Correlated Failure Mitigations

1. **Evidence citation check** — for each key finding in the report, the verifier confirms a matching tool call exists in the session events. If the report claims data that no tool call returned, flagged as UNSUPPORTED.
2. **Cross-reference tool outputs** — the verifier uses a `get_session_tool_outputs(session_id)` tool that returns raw tool call results (not narrative summaries). Compares these against report claims.
3. **Different model** — verification agent uses a different LLM than the investigating agents. Different model = different reasoning biases = less correlated failures.

### Human Feedback Schema

#### Required Core (2 questions, single-click each)

| # | Question | Options | System effect |
|---|----------|---------|---------------|
| **F1** | Root cause accuracy | `CORRECT` / `PARTIALLY_CORRECT` / `WRONG` / `INCONCLUSIVE` | Combined tier override, episodic memory validation |
| **F2** | Time impact | `SIGNIFICANT` / `SOME` / `MINIMAL` / `NONE` / `NEGATIVE` | ROI trending, system justification |

#### Optional Detail (5 questions, all select/multi-select)

| # | Question | Options | System effect |
|---|----------|---------|---------------|
| **F3** | Actual root cause category *(shown if F1 != CORRECT)* | Single-select from 11 alert categories | Misclassification tracking, classifier tuning |
| **F4** | Which agent findings helped your investigation? | Multi-select from `key_findings[]` list | Decision tree hit rate **boost** for selected agents |
| **F5** | Which investigation steps were unnecessary? | Multi-select from `investigation_path[]` + `dead_ends[]` | Decision tree hit rate **demotion**, skip rule candidates |
| **F6** | What investigation was missing? | Multi-select from checklist | Decision tree expansion, capability gap tracking |
| **F7** | What type of fix was applied? | Single-select from solution types | Solution pattern library per category |

Every question has an optional `notes: string` field for additional context.

#### F6 — Missing Investigation Checklist

| Option | Meaning |
|--------|---------|
| `DATABASE_QUERIES` | Needed to run/analyze SQL queries directly |
| `DEPLOYMENT_HISTORY` | Needed deeper deploy/rollback analysis |
| `NETWORK_CONNECTIVITY` | Needed network path / firewall / DNS investigation |
| `CODE_REVIEW` | Needed to read source code to understand the bug |
| `CONFIG_CHANGES` | Needed to check config/feature flag changes |
| `SCALING_CAPACITY` | Needed to check resource limits, autoscaling, quotas |
| `DEPENDENCY_HEALTH` | Needed to check upstream/downstream service health |
| `SECURITY_AUTH` | Needed to check auth failures, cert expiry, IAM |
| `DATA_INTEGRITY` | Needed to check data corruption, migration issues |
| `CUSTOMER_IMPACT` | Needed to assess user-facing impact scope |
| `OTHER` | *(triggers notes field)* |

#### F7 — Solution Type Categories

| Option | Examples |
|--------|---------|
| `FEATURE_FLAG_TOGGLE` | Disabled/enabled a feature flag |
| `ROLLBACK_DEPLOYMENT` | Reverted to a previous deployment/revision |
| `CONFIG_CHANGE` | Changed app config, env var, or infrastructure setting |
| `SCALE_RESOURCES` | Increased replicas, CPU, memory, or quotas |
| `RESTART_SERVICE` | Restarted the service or killed stuck processes |
| `DATABASE_FIX` | Killed blocking SPIDs, rebuilt indexes, ran migration |
| `CODE_FIX` | Deployed a code change to fix the issue |
| `INFRASTRUCTURE_CHANGE` | Network, DNS, firewall, IAM, cert change |
| `SELF_RESOLVED` | Issue resolved on its own before intervention |
| `ESCALATED` | Handed off to another team without resolution |
| `OTHER` | *(triggers notes field)* |

### Firestore Document Schema (human_feedback subdocument)

```python
"human_feedback": {
    # Required core
    "root_cause_accuracy": "CORRECT",        # F1
    "time_impact": "SOME",                   # F2
    
    # Optional detail
    "actual_category": "sql-database",       # F3 — from 11 categories
    "useful_findings": [                     # F4 — agent names
        "gcp_logging_agent", 
        "datadog_agent"
    ],
    "wasteful_steps": [                      # F5 — agent names
        "deploy_correlation_agent"
    ],
    "missing_investigation": [               # F6 — from checklist
        "DATABASE_QUERIES"
    ],
    "solution_type": "FEATURE_FLAG_TOGGLE",  # F7
    
    # Metadata
    "feedback_by": "first.last@acme.example",
    "feedback_at": "2026-06-10T15:30:00Z",  # server timestamp
    "notes": {                               # Optional free text per question
        "F1": "Got the feature flag connection right but missed the lock mechanism",
        "F6": "Needed to check sys.dm_exec_requests directly"
    }
}
```

### Automated Verification Schema (verification subdocument — replaces existing verification field)

Update the existing `verification` field in the incidents schema to:

```python
"verification": {
    # Automated scoring (set by verification_agent)
    "automated": {
        "diagnosis": "STRONG",           # STRONG / PARTIAL / WEAK / UNSUPPORTED
        "solution": "ACTIONABLE",        # ACTIONABLE / VAGUE / DISCONNECTED / MISSING
        "efficiency": "ADEQUATE",        # EFFICIENT / ADEQUATE / WASTEFUL
        "automated_tier": "MEDIUM",      # MEDIUM / LOW / UNRELIABLE (never HIGH)
        "evidence_citations_valid": 8,   # Count of verified citations
        "evidence_citations_invalid": 1, # Count of unverifiable claims
        "scored_at": "2026-06-10T14:42:00Z",
        "scorer_model": "gemini-2.5-flash"
    },
    
    # Combined tier (recalculated when human feedback arrives)
    "combined_tier": "HIGH",             # HIGH / MEDIUM / LOW / UNRELIABLE
    "tier_updated_at": "2026-06-10T15:30:00Z",
    
    # Human correction flag
    "corrected": false,                  # true if human provided different root cause
    "corrected_root_cause": null,        # Human-provided root cause (if corrected)
    "corrected_category": null           # Human-provided category (if F3 answered)
}
```

### Combined Tier Calculation

```
IF human_feedback exists:
    IF F1 == WRONG → combined_tier = LOW (hard override)
    IF F1 == CORRECT AND automated_tier >= MEDIUM → combined_tier = HIGH
    IF F1 == PARTIALLY_CORRECT → combined_tier = min(automated_tier, MEDIUM)
    IF F1 == INCONCLUSIVE → combined_tier = automated_tier (no change)
ELSE:
    combined_tier = automated_tier (capped at MEDIUM)
```

### Corrected Incident Retrieval

When F1 = WRONG and the human provides corrections (F3, F6, F7), the incident is marked `corrected: true` with the human-provided ground truth. A background process handles corrected incidents:

1. Sets `corrected: true`, stores `corrected_root_cause` and `corrected_category`
2. The retrieval query for `find_similar_incidents` includes corrected incidents:
   - For corrected incidents, returns the **human-provided** root cause and category, not the agent's original analysis
   - Tags the result as `source: "human_corrected"` so the synthesis agent knows this is ground truth
3. Corrected incidents feed decision tree optimization with **inverted** signal:
   - The agent's investigation path becomes a negative example ("don't do this")
   - The human's actual category + solution type become the positive signal

```python
async def find_similar_incidents(category: str, service: str = None, limit: int = 5) -> str:
    """Query past incidents — includes human-corrected incidents."""
    
    # Standard query: HIGH and MEDIUM tier incidents
    standard_query = db.collection("incidents") \
        .where("category", "==", category) \
        .where("verification.combined_tier", "in", ["HIGH", "MEDIUM"]) \
        .order_by("timestamp", direction=firestore.Query.DESCENDING) \
        .limit(limit)
    
    # Corrected query: incidents where human said actual category matches
    corrected_query = db.collection("incidents") \
        .where("verification.corrected", "==", True) \
        .where("verification.corrected_category", "==", category) \
        .order_by("timestamp", direction=firestore.Query.DESCENDING) \
        .limit(3)
    
    results = []
    
    async for doc in standard_query.stream():
        inc = doc.to_dict()
        results.append({
            "incident_id": inc["incident_id"],
            "source": "verified",
            "root_cause": inc["discoveries"]["root_cause"],
            "investigation_path": inc["discoveries"]["investigation_path"],
            "confidence_tier": inc["verification"]["combined_tier"],
        })
    
    async for doc in corrected_query.stream():
        inc = doc.to_dict()
        results.append({
            "incident_id": inc["incident_id"],
            "source": "human_corrected",
            "root_cause": inc["verification"]["corrected_root_cause"],
            "original_agent_root_cause": inc["discoveries"]["root_cause"],
            "correction_note": "Agent misdiagnosed — human provided correct root cause",
            "solution_type": inc.get("human_feedback", {}).get("solution_type"),
        })
    
    return json.dumps(results, indent=2, default=str)
```

### Feedback Flow → System Improvements

```
F1 (root cause accuracy)
 ├── CORRECT → combined tier promotes to HIGH, incident verified
 ├── PARTIALLY_CORRECT → tier stays ≤ MEDIUM, flagged for decision tree review
 ├── WRONG → tier demotes to LOW, marked corrected with human ground truth
 └── INCONCLUSIVE → tier stays at automated level, no promotion

F2 (time impact)
 └── Aggregated monthly → efficiency trend metric

F3 (actual category)
 └── If different from agent's classification → misclassification logged
     → After N misclassifications for a pattern, classifier prompt updated

F4 (useful findings) 
 └── For each selected agent → hit_rate++ in decision_tree for this category
     → Over time, useful agents rise to top of triage order

F5 (wasteful steps)
 └── For each selected agent → hit_rate-- in decision_tree for this category
     → Repeated waste → skip_rule candidate → auto-proposed to SRE for approval

F6 (missing investigation)
 └── Aggregated per category → capability gap report
     → e.g., "sql-database incidents: 60% of humans needed DATABASE_QUERIES"

F7 (solution type)
 └── Aggregated per category → solution pattern library
     → e.g., "cloud-infrastructure: 45% resolved by ROLLBACK_DEPLOYMENT"
```

---

## Implementation Plan

### Phase 1: Firestore Foundation (Episodic + Procedural) [COMPLETE]

| Story | Points | Status | Description |
|-------|--------|--------|-------------|
| Create Firestore database in aiops-dev-project | 1 | ✅ COMPLETE | Native mode, us-central1, composite indexes |
| Build firestore_tools.py (write, query, verify) | 3 | ✅ COMPLETE | CRUD operations for incidents + decision_trees |
| Update persistence_agent to dual-write (GCS + Firestore) | 3 | ✅ COMPLETE | Existing GCS write stays, add Firestore write |
| Update historical_context to query Firestore | 3 | ✅ COMPLETE | Replace GCS filename-prefix search with Firestore queries |
| Add triage guidance lookup from decision_trees | 2 | ✅ COMPLETE | Query decision_tree doc in context_parallel stage |
| Seed initial decision_trees for 11 categories | 2 | ✅ COMPLETE | Bootstrap with current tier1/tier2 agent ordering |
| Add Firestore permissions to ir-agent-sa | 1 | ✅ COMPLETE | `roles/datastore.user` on aiops-dev-project |
| Build verification_agent (Stage 7) | 3 | ✅ COMPLETE | Scores diagnosis/solution/efficiency from report + raw tool outputs, uses different model |
| Add get_session_tool_outputs tool | 2 | ✅ COMPLETE | Reads raw tool call results from session events for cross-reference |
| Build human feedback collection in skill | 3 | ✅ COMPLETE | Structured F1-F7 questions presented after report, writes to Firestore |
| Build combined tier calculation | 2 | ✅ COMPLETE | Recalculates tier when human feedback arrives, handles corrected incidents |
| Update find_similar_incidents for corrected data | 2 | ✅ COMPLETE | Dual query for verified + human-corrected incidents |
| **Phase 1 Total** | **27** | **COMPLETE** | |

### Phase 2: Semantic Retrieval + Memory Bank [PLANNED]

| Story | Points | Status | Description |
|-------|--------|--------|-------------|
| Add Vector Search index + endpoint (Terraform) | 3 | ⏳ PLANNED | Stream update index, 768 dims, metadata restricts |
| Build embedding pipeline (embed + upsert on incident close) | 3 | ⏳ PLANNED | text-embedding-005, upsert to Vector Search |
| Build semantic query tool (embed alert → find neighbors) | 3 | ⏳ PLANNED | Top 3 similar incidents injected into context |
| Configure VertexAiMemoryBankService in agent.py | 2 | ⏳ PLANNED | Add LoadMemoryTool to historical_context |
| Add session event parsing for methodology extraction | 3 | ⏳ PLANNED | Read completed session events → methodology structure |
| **Phase 2 Total** | **14** | **PLANNED** | |

### Phase 3: Decision Tree Optimization + Verification Loop [PLANNED]

| Story | Points | Status | Description |
|-------|--------|--------|-------------|
| Build background optimization Cloud Function | 5 | ⏳ PLANNED | Reads verified incidents, re-scores steps, transactional update |
| Build human verification endpoint (Cloud Run or ADO webhook) | 3 | ⏳ PLANNED | SRE confirms/corrects root cause → updates verification status |
| Connect verification to optimization pipeline | 2 | ⏳ PLANNED | Only verified incidents feed optimization |
| Add decision tree influence to tier1 agent ordering | 3 | ⏳ PLANNED | Synthesis agent uses step priorities from decision_tree |
| **Phase 3 Total** | **13** | **PLANNED** | |

### Phase 4: Testing & Validation [PLANNED]

| Story | Points | Status | Description |
|-------|--------|--------|-------------|
| E2E test: incident → Firestore → retrieval in next incident | 3 | ⏳ PLANNED | Validate round-trip |
| Backfill existing GCS incidents to Firestore | 2 | ⏳ PLANNED | Tag as `backfilled: true`, `has_methodology: false` |
| Add Firestore metrics to E2E test harness | 1 | ⏳ PLANNED | Assert Firestore write occurred, query returns results |
| **Phase 4 Total** | **6** | **PLANNED** | |

**Grand Total: ~60 story points across 23 stories**

---

## Cost Analysis

### Monthly Cost at Steady State

| Component | Phase | Monthly Cost | Notes |
|-----------|-------|-------------|-------|
| Firestore (reads + writes + storage) | 1 | ~$0.03 | 100 incidents, 1K reads |
| Firestore (decision tree optimization) | 3 | ~$0.01 | 50 reads + 11 writes per optimization run |
| Vector Search endpoint | 2 | ~$32.00 | Always-on minimum (defer if possible) |
| Embedding API | 2 | ~$0.002 | 100 embed calls/month |
| Memory Bank | 2 | ~$0.025 | 100 sessions × $0.25/1K |
| Agent Engine Sessions | — | Included | Already paying for Agent Engine |
| **Total (Phase 1 only)** | | **~$0.04/month** | |
| **Total (All phases)** | | **~$32.07/month** | Vector Search dominates |

### Cost Optimization: Skip Vector Search Initially

If Vector Search's $32/month isn't justified at low volume:
- Use Firestore compound queries (category + service + severity) for structured lookup
- Use Memory Bank's built-in semantic search for cross-category pattern matching
- Add Vector Search later when corpus exceeds ~500 incidents and cross-category matches become critical

**Phase 1 + Memory Bank (no Vector Search): ~$0.07/month**

---

## Migration Strategy

### GCS → Firestore Transition

1. **Phase 1**: Dual-write (GCS + Firestore). Historical context queries both.
2. **Phase 2**: Historical context queries Firestore first, falls back to GCS for pre-migration incidents.
3. **Phase 3**: After backfill, historical context uses Firestore only. GCS writes continue as durable backup.
4. **Keep GCS writes permanently** — they're the archival format and may have other consumers.

### Backfilling Existing Incidents

Parse existing GCS incident JSON files and load into Firestore. Tag backfilled entries:
```python
{
    "backfilled": True,
    "has_methodology": False,  # Pre-migration incidents lack investigation path data
    "original_gcs_path": "gs://incident-summaries/2026-06-10_sev2_..."
}
```

---

## Risks & Mitigations

| Risk | Impact | Mitigation |
|------|--------|------------|
| Firestore not accessible from Agent Engine | Blocks Layers 1-2 | Verify `ir-agent-sa` can write to Firestore before starting (same pattern as GCS access) |
| Decision tree optimization produces bad orderings | Wrong triage priorities | Version field + optimization log enable rollback; require minimum 10 verified incidents before optimization |
| Vector Search endpoint cost ($32/month) disproportionate to value | Budget pressure | Defer to Phase 2; use Firestore queries + Memory Bank semantic search first |
| Session event parsing breaks on ADK updates | Methodology extraction fails | Pin ADK version; add schema version to parsed methodology |
| Memory Bank extracts wrong facts | Bad org-level patterns | Gate on human verification (Phase 3); start with LoadMemoryTool (on-demand) not PreloadMemoryTool (automatic) |
| Firestore document size limit (1 MiB) | Large incident reports truncated | Cap investigation_path to 20 steps, key_findings to 30 entries; store full report in GCS |
| Concurrent decision_tree updates | Version conflicts | Firestore transactions guarantee atomic read-modify-write |

---

## Service-to-Docs Reference

| Data | Service | Official Docs |
|------|---------|--------------|
| Incident records + triage findings | Firestore | https://docs.cloud.google.com/firestore/native/docs |
| Decision trees per category | Firestore | https://docs.cloud.google.com/firestore/native/docs/transactions |
| Similar incident retrieval | Vertex AI Vector Search | https://docs.cloud.google.com/vertex-ai/docs/vector-search/overview |
| Live triage session state | Agent Engine Sessions | https://cloud.google.com/vertex-ai/generative-ai/docs/agent-engine/sessions/overview |
| Cross-incident org-level patterns | Agent Engine Memory Bank | https://cloud.google.com/agent-builder/agent-engine/memory-bank/overview |
| Agent runtime | Agent Engine + ADK | https://docs.cloud.google.com/vertex-ai/generative-ai/docs/agent-engine/overview |
