# Google SRE AI Principles — Analysis & Impact on IR Agent Platform

**Sources:**
- [How Google SREs Use Gemini CLI to Solve Real-World Outages](https://cloud.google.com/blog/topics/developers-practitioners/how-google-sres-use-gemini-cli-to-solve-real-world-outages)
- [AI Engineering for Reliable Operations](https://sre.google/resources/practices-and-processes/ai-engineering-reliable-operations/)

**Date:** 2026-06-01

---

## Core Principles Extracted

### From Article 1: Gemini CLI for Outages

| # | Principle | Google's Implementation |
|---|-----------|------------------------|
| 1 | **Mitigation-First** | MTTM > MTTR. Mitigate user impact first, fix code later. 5-minute SLO to acknowledge a page. |
| 2 | **Bounded Action Space** | Classify symptoms against a *closed set* of Generic Mitigations (drain, rollback, restart, scale). This constraint makes AI safe. |
| 3 | **5-Layer Safety** | Deterministic tools → Risk assessment → Policy enforcement → Human-in-the-loop → Audit trails. |
| 4 | **Context Preservation** | When mitigation fails, AI maintains full context — no operator context switch. This is the real MTTM win. |
| 5 | **Speed of Insight > Speed of Execution** | Surfacing the right signal in seconds avoids minutes chasing red herrings. |
| 6 | **Differential Diagnosis** | When first theory fails, pivot based on evidence. Infrastructure healthy? → Must be application. |
| 7 | **Virtuous Loop** | Postmortems become training data. Every resolved incident makes the system smarter. |
| 8 | **Strictly Typed Tools via MCP** | Not arbitrary commands. Each tool has typed inputs, risk metadata, rollback instructions. |
| 9 | **Custom Commands** | Reusable workflow templates (postmortem generation, playbook execution). |
| 10 | **Progressive Trust** | Copilot → Supervised Autopilot → Full Autopilot, gated by demonstrated reliability. |

### From Article 2: AI Engineering for Reliable Operations

| # | Principle | Google's Implementation |
|---|-----------|------------------------|
| 11 | **Safety Trifecta** | Transparency + Real-time Risk Evaluation + Progressive Authorization. |
| 12 | **No Ambient Access** | Agent identities are distinct from developer credentials. Least privilege, on-demand access. |
| 13 | **Agentic Circuit Breakers** | Per-agent rate limits. Actions must be "highly interruptible." |
| 14 | **Mandatory Dry-Run** | Every system/API supports `dry_run=true` to predict outcome and blast radius before mutation. |
| 15 | **Decoupled Reasoning & Execution** | AI cognitive layer separated from actuation layer by deterministic safety boundary. |
| 16 | **Five-Level Autonomy** | L0 Manual → L1 Assisted → L2 Partial → L3 High → L4 Full. Different scenarios warrant different levels. |
| 17 | **Post-Actuation Verification** | After mitigation, wait → verify alert cleared → if not, re-investigate with new strategy. |
| 18 | **Token Budget Management** | Strict per-step token minimization prevents hallucination in long-horizon incidents. |
| 19 | **Global Emergency Stop** | "Red Button" endpoints for instant fleet-wide agent suspension. |
| 20 | **Investigation Worthiness** | 4-level hierarchy: Anomaly Detection → Change Correlation → Investigation Worthiness → Root Cause. |
| 21 | **Tiered Evaluation Data** | Bronze (auto-generated) → Silver (calibrated) → Gold (human-verified). |
| 22 | **Embedded Golden Data Collection** | When oncaller declares mitigation, system generates structured suggestions for them to accept/modify/reject. |
| 23 | **Hybrid Evaluation** | LLM-as-Judge for reasoning quality + deterministic scoring for mitigation accuracy. |
| 24 | **Dynamic Autonomy Downgrade** | Auto-downgrade from L3→L2 when elevated risk detected (peak traffic, active incident, etc.). |
| 25 | **Enrichment Time Budget** | AI Alert executes within ~2 minute budget with massive parallelism. |

---

## Alignment Analysis: Our Plan vs. Google's Principles

### Already Aligned

| Our Feature | Google Principle | Status |
|-------------|-----------------|--------|
| Read-only Phase 1 | #2 Bounded Action Space, #15 Decoupled Reasoning | ✅ Implemented |
| Human-in-the-loop via Teams | #3 Layer 4 HITL, #16 L1/L2 Autonomy | ✅ Matches L1 Assisted |
| Deterministic agent checklists | #2 Bounded Action Space, #8 Typed Tools | ✅ Agents have numbered steps |
| Bifrost response truncation | #13 Agentic Circuit Breakers | ⚠️ Partial — shared singleton with 4000-char response truncation; full circuit breaker (30s failover) not yet implemented |
| Payload sanitizer | Security vector defense | ✅ Implemented |
| 15 investigation rules | #3 Policy Enforcement layer | ✅ Baked into every prompt |
| Knowledge base (Firestore + GCS) | #7 Virtuous Loop | ✅ Implemented |
| MCP via Bifrost | #8 Strictly Typed Tools | ✅ Per-server allowlists |
| Self-exclusion filter | Preventing feedback loops | ✅ Rule 15 |
| Investigation checkpointing | #4 Context Preservation | ✅ Agent Engine sessions |
| Verification agent (Stage 7) | #17 Post-Actuation Verification | ✅ Implemented |
| Human feedback collection | #22 Embedded Golden Data Collection | ✅ F1-F7 questions |
| Chain-of-thought in incidents | #2 Transparent Reasoning | ✅ Stored in Firestore |
| Per-server tool allowlists | #12 No Ambient Access, Least Privilege | ✅ Read-only enforcement |

### Gaps — Recommended Changes

#### GAP 1: Investigation Worthiness Gate (NEW)

**Google says:** 4-level investigation hierarchy includes assessing whether an anomaly *merits* deeper investigation before dispatching agents.

**Our plan:** Goes straight from Classification → dispatch all 8 Tier 1 agents. No gate to suppress noise or known-transient patterns.

**Recommendation:** Add an **Investigation Worthiness** step between Classification and Tier 1 dispatch:
- Check: Is this a known-transient alert pattern? (query KB for auto-resolved patterns)
- Check: Is this alert already covered by an active investigation? (enhanced dedup)
- Check: Does error budget allow ignoring this? (if error budget is >90%, low-severity alerts can queue)
- If worthless: Log, notify, but don't spawn full investigation

**Impact on decision trees:** Add a pre-dispatch decision node after Classification.

---

#### GAP 2: Chain-of-Thought Audit Trail (PARTIALLY ADDRESSED)

**Google says:** Every execution trace stored for "rigorous debugging and continuous improvement." AI Operator presents full chain-of-thought including signals used, hypotheses considered, rationale.

**Current status:** 
- ✅ Incident records store investigation findings with root cause and evidence
- ✅ Session tool outputs captured via `get_session_tool_outputs` tool
- ✅ Verification agent scores capture evidence citations and reasoning quality
- ⏳ Not yet implemented: Detailed per-step hypothesis tracking with evidence for/against

**Recommendation for future enhancement:** Add `agent_execution_traces` Firestore collection:

> **Note:** The IR Agent uses Firestore Native mode. This would be a Firestore
> `agent_execution_traces` collection, not a SQL table.

```
Firestore collection: agent_execution_traces
Fields:
  incident_id, agent_name, step_number, tool_called (string),
  tool_input (map), tool_output_summary, hypothesis,
  evidence_assessment ("supports"|"refutes"|"inconclusive"),
  tokens_used, duration_ms, created_at (timestamp)
```

---

#### GAP 3: Resolution Verification Loop (PARTIALLY ADDRESSED)

**Google says:** Post-actuation verification — after mitigation, verify alert actually cleared. If not, re-investigate.

**Current status:**
- ✅ Verification agent (Stage 7) scores investigation quality before persistence
- ✅ Human feedback collection captures root cause accuracy (F1) and time impact (F2)
- ⏳ Not yet implemented: Automated re-check of alert source after a time delay

**Recommendation for future enhancement:** Add a **Resolution Verification Agent** that:
1. Waits a configurable period after investigation posts (default: 15 minutes)
2. Re-checks the original alert source — did the alert clear?
3. If cleared: Mark incident as `resolved`, log resolution time
4. If still firing: Post update to Teams: "Alert still active. Re-running stale agents with fresh data."
5. Re-dispatch Tier 1 agents that returned inconclusive findings

**Impact:** New agent in orchestrator graph. Updates incident status automatically.

---

#### GAP 4: Per-Agent Token Budget (NEW)

**Google says:** "Strict token management prevents the LLM from losing context or hallucinating over time."

**Our plan:** No explicit token budgets. Long incidents with many agents could exhaust context.

**Recommendation:** Add per-agent token budgets in agent config:
```python
AGENT_TOKEN_BUDGETS = {
    "cloud_run_health": 4000,
    "gcp_logging": 6000,
    "sql_dba": 8000,  # more complex
    "codebase": 10000,  # token-heavy by design
}
```
Agents that hit 80% of budget must summarize and checkpoint. At 100%, force-stop and report partial findings.

**Impact:** Add token tracking to `investigation_findings.tokens_used` (already exists) + enforce budget in agent runner.

---

#### GAP 5: Global Emergency Stop — "Red Button" (NEW)

**Google says:** Emergency endpoints to "instantly pause all in-flight agentic actions, block new actions, or globally revoke permissions."

**Our plan:** No kill switch. If agents go haywire, the only recourse is killing Cloud Run containers.

**Recommendation:** Add:
1. A `system_state` document in Firestore with fields: `{paused: bool, paused_at: timestamp, paused_by: string}`
2. A `/pause` endpoint on the API surface (authenticated to SRE team)
3. Every agent checks `system_state.paused` before each tool call
4. When paused: All in-flight agents checkpoint and stop. New webhooks are queued but not processed.
5. A `/resume` endpoint to unpause

**Impact:** New table, new endpoints, agent middleware change.

---

#### GAP 6: Explicit Autonomy Level Model (ENHANCE)

**Google says:** Five-level model with clear progression gates.

**Our plan:** Has phases (1-3) but doesn't use autonomy language or define progression criteria.

**Recommendation:** Adopt Google's model explicitly:

| Phase | Autonomy Level | Capability | Gate to Next Level |
|-------|---------------|------------|-------------------|
| 1 | **L1 Assisted** | Auto-investigate, human reads findings | 50+ incidents processed, <5% false positive rate |
| 2 | **L2 Partial** | Auto-investigate + suggest mitigations, human approves | Suggestion accuracy >80% on 100+ incidents |
| 3 | **L3 High** | Auto-investigate + execute approved mitigations | Mitigation success rate >95% with safety controls |

---

#### GAP 7: Investigation Time Budget (NEW)

**Google says:** AI Alert executes within ~2 minute budget.

**Our plan:** No time constraints on investigation. Agents could run indefinitely.

**Recommendation:**
- Tier 1 parallel: 3-minute budget (all 8 agents)
- Tier 2 conditional: 2-minute budget per agent
- Total investigation: 8-minute cap (3 Tier 1 + 2 synthesis + 3 Tier 2)
- Agents that exceed budget report partial findings

**Impact:** Add timeout enforcement in orchestrator agent runner. Add `timed_out: bool` to findings.

---

#### GAP 8: Baseline Comparison Step (ENHANCE)

**Google says:** Investigation Dashboard starts with anomaly DETECTION — ML flags deviations from baseline before investigation.

**Our plan:** Agents query current state but don't explicitly compare against normal baseline.

**Recommendation:** Add "Step 0: Establish baseline" to each agent's decision tree:
- Cloud Run Health: What are the normal instance counts, CPU, memory for this time of day?
- GCP Logging: What's the normal error rate for this service?
- SQL DBA: What's the normal connection count, CPU for this time?
- Compare current values against baseline to determine if findings are actually anomalous

**Impact:** Decision tree update for all agents. May require baseline data storage.

---

#### GAP 9: Suggested Mitigation Field (ENHANCE)

**Google says:** Mitigation-first philosophy. Classify symptoms → select from bounded set of Generic Mitigations.

**Our plan:** `investigation_findings.recommended_actions` exists but is "always empty" in Phase 1.

**Recommendation:** Populate `recommended_actions` in Phase 1 as **read-only suggestions** (not executed):
- Agent finds high CPU: Suggest "Scale Cloud Run service to max instances"
- Agent finds blocking chain: Suggest "Kill blocking SPID after human review"
- Agent finds failed deploy: Suggest "Rollback to previous revision"

Generic Mitigation taxonomy (borrowed from Google):
1. **Drain** — Remove traffic from affected instance/cell
2. **Rollback** — Revert to previous known-good deployment
3. **Restart** — Restart affected service/container
4. **Scale** — Add capacity (instances, CPU, memory)
5. **Isolate** — Quarantine affected component
6. **Escalate** — Route to human specialist

**Impact:** Update agent prompts to suggest mitigations. Populate `recommended_actions` field. Trains the system for Phase 2 actuation.

---

#### GAP 10: Embedded Golden Data Collection (IMPLEMENTED)

**Google says:** When oncaller declares mitigation, system generates structured suggestions for accept/modify/reject. This is how you build evaluation data without extra effort.

**Current status:** ✅ Implemented via human feedback system (F1-F7 questions)
- ✅ F1: Root cause accuracy (CORRECT / PARTIALLY_CORRECT / WRONG / INCONCLUSIVE)
- ✅ F4: Which agent findings were useful (multi-select from key_findings)
- ✅ F5: Which investigation steps were wasteful (multi-select from investigation_path)
- ✅ F6: What investigation was missing (checklist of capabilities)
- ✅ F7: What type of fix was applied (solution type categories)
- ✅ Stored in `human_feedback` subdocument within incidents collection

**Already implemented:**

```
Firestore incidents collection:
  human_feedback subdocument:
    - root_cause_accuracy: string (F1)
    - time_impact: string (F2)
    - useful_findings: array (F4 — agent names)
    - wasteful_steps: array (F5 — agent names)
    - missing_investigation: array (F6 — capability gaps)
    - solution_type: string (F7)
    - feedback_by: string (email)
    - feedback_at: timestamp
    - notes: map (optional free text per question)
```

**Impact:** Creates Bronze-tier evaluation data automatically after each incident resolution.

---

#### GAP 11: Hypothesis-Driven Synthesis (ENHANCE)

**Google says:** Differential diagnosis — when first theory fails, pivot based on evidence.

**Our plan:** Synthesizer merges findings and decides Tier 2 dispatch. But doesn't explicitly model hypothesis testing.

**Recommendation:** Synthesis agent should:
1. State top 3 hypotheses based on Tier 1 findings
2. For each hypothesis, cite supporting and refuting evidence
3. Rank hypotheses by evidence strength
4. Dispatch Tier 2 agents specifically to TEST the top hypothesis (not just "category-based dispatch")
5. After Tier 2: Re-evaluate hypotheses. State which are supported/refuted.

**Impact:** Synthesis agent prompt update. More targeted Tier 2 dispatch.

---

## Updated Investigation Rules

Based on Google's principles, add these to the existing 15 rules:

| # | New Rule | Source |
|---|----------|--------|
| 16 | **Establish baseline before flagging anomalies** — Every metric must be compared against time-of-day normal. A value that looks high might be normal for Monday morning. | Google: Anomaly Detection level |
| 17 | **Suggest mitigation, don't just diagnose** — Every finding should include at minimum a Generic Mitigation category: drain, rollback, restart, scale, isolate, or escalate. | Google: Mitigation-First |
| 18 | **Budget your tokens** — If you've used 80% of your token budget, summarize and stop. Partial findings > hallucinated findings. | Google: Token Management |
| 19 | **Speed of insight > completeness** — Surface the most important signal first. Don't wait for all 5 steps to complete before reporting a CRITICAL finding. | Google: Insight Acceleration |
| 20 | **Test hypotheses, don't collect data** — Every tool call should be testing a specific hypothesis. If you can't state what you're testing, you're exploring aimlessly. | Google: Differential Diagnosis |

---

## Decision Tree Updates Summary

Every agent decision tree should be updated with:

1. **Step 0 (NEW):** "Establish baseline — what's normal for this service at this time?"
2. **Each finding node:** Add `→ Suggest Mitigation: [drain/rollback/restart/scale/isolate/escalate]`
3. **Terminal nodes:** Add `→ Confidence: [STRONG/MODERATE/WEAK/UNKNOWN]` (already exists) + `→ Hypothesis: [statement]`
4. **Time budget:** Add `⏱ Budget: X minutes` header to each tree
5. **Token budget:** Add `🔤 Token limit: Xk` header to each tree

### Orchestrator Decision Tree (NEW — replaces simple sequential flow)

```
Alert Received
    → Sanitize payload
    → Dedup (Pass 1: time+service, Pass 2: semantic)
    → If duplicate: merge into existing incident
    → If new: Classify (11 categories)
    → Investigation Worthiness Check (NEW)
        → Is this a known-transient pattern? (query KB)
        → Is error budget >90% and severity <P2? (queue, don't investigate)
        → Is there an active investigation for this service? (merge)
        → If worthy: Dispatch Tier 1 (parallel, 3-min budget)
        → If not worthy: Log + notify + close
    → Tier 1 agents return findings (or timeout with partials)
    → Synthesis: Form hypotheses, rank by evidence
    → Tier 2 dispatch: Test top hypothesis (not just category-based)
    → Final synthesis + Report → Teams
    → Resolution Verification (15-min wait, then re-check)
    → If resolved: Close + debrief + golden data collection
    → If not resolved: Re-investigate with updated context
```

---

## Architectural Changes — Priority Matrix

| Priority | Change | Effort | Impact |
|----------|--------|--------|--------|
| **P0** | Add Global Emergency Stop ("Red Button") | Small | Safety-critical |
| **P0** | Add per-agent time budgets | Small | Prevents runaway agents |
| **P1** | Add Investigation Worthiness gate | Medium | Reduces noise, saves cost |
| **P1** | Add suggested mitigation to agent outputs | Medium | Trains for Phase 2 actuation |
| **P1** | Add Resolution Verification loop | Medium | Closes feedback loop |
| **P1** | Add chain-of-thought audit trail | Medium | Debugging + evaluation |
| **P2** | Add per-agent token budgets | Small | Prevents hallucination |
| **P2** | Add baseline comparison (Step 0) | Medium | Better anomaly detection |
| **P2** | Add hypothesis-driven synthesis | Medium | More targeted investigation |
| **P2** | Add embedded golden data collection | Medium | Builds eval dataset |
| **P3** | Adopt explicit 5-level autonomy model | Small | Documentation clarity |

---

## Firestore Collection Additions Summary

> **Note:** The IR Agent uses Firestore Native mode (not SQL). The collections below
> describe the proposed Firestore document structures for these future enhancements.

```
Firestore collection: system_state
Document ID: "global" (singleton)
Fields:
  - paused: boolean (default: false)
  - paused_at: timestamp
  - paused_by: string
  - reason: string

Firestore collection: agent_execution_traces
Document ID: auto-generated
Fields:
  - incident_id: string (references incidents collection)
  - agent_name: string
  - step_number: number
  - tool_called: string
  - tool_input: map
  - tool_output_summary: string
  - hypothesis: string
  - evidence_assessment: string ("supports" | "refutes" | "inconclusive")
  - tokens_used: number
  - duration_ms: number
  - created_at: timestamp

Firestore collection: finding_evaluations
Document ID: auto-generated
Fields:
  - finding_id: string (references investigation findings)
  - evaluator_email: string
  - accurate: boolean
  - useful: boolean
  - root_cause_contributor: boolean
  - correction: string
  - evaluated_at: timestamp

Additional fields on existing incidents collection documents:
  - chain_of_thought: map (structured reasoning trace)
  - timed_out: boolean (default: false)
  - suggested_mitigation: string (drain | rollback | restart | scale | isolate | escalate)
```
