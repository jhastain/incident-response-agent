# Root-Cause Accuracy Rubric (FROZEN)

Established 2026-07-15 for the accuracy loop. This rubric is **fixed**. Accuracy
scores may NOT be improved by weakening this rubric — only by improving the agent's
actual root-cause identification. Any change to this file invalidates the loop and
must be explicitly approved.

## What is scored

**Root cause only.** The score answers one question: *Did the agent's report
correctly identify the true root cause of the incident, backed by valid evidence?*

Solution quality, efficiency, and report formatting are tracked for context but are
NOT part of the 95 bar.

## Who scores

An **independent judge model = Claude Sonnet** (`claude-sonnet-5` via Vertex).
The judge is NOT the model that ran the investigation (Haiku). The judge reads:
- the original alert text (the incident),
- the agent's final `report`,
- the agent's raw `findings` (all tier1 domains) + `synthesis` + `tier2_findings`
  (so it can check whether the stated root cause is actually supported by evidence).

The judge does NOT see the agent's own `verification` block (no anchoring).

## Scoring scale (0–100), per alert

| Band | Meaning |
|---|---|
| 95–100 | Correct root cause, specific and mechanism-level, fully supported by cited evidence in the findings. A responding engineer could act on it directly. |
| 80–94  | Correct root cause but under-specified (right system, vague mechanism) OR one supporting citation is weak/unverifiable. |
| 60–79  | Partially correct: identifies the right symptom/area but not the actual cause, or names a plausible cause not backed by the gathered evidence. |
| 40–59  | Wrong or speculative root cause; evidence does not support the conclusion; or hedged with no committed cause. |
| 0–39   | No root cause, or a root cause contradicted by the evidence. |

## Hard rules for the judge

1. **Evidence-grounded.** A confident root cause that the findings do not actually
   support scores in the 40–59 band, not higher. Fabricated/invalid citations cap
   the score at 59.
2. **Mechanism over category.** "Application errors on the login endpoint" is a
   restatement of the alert, not a root cause. The cause must explain *why*.
3. **No credit for breadth.** Listing many possibilities without committing to the
   actual cause is band 40–59.
4. **The judge outputs**: `root_cause_score` (int 0–100), `identified_cause` (str),
   `true_cause_assessment` (str), `evidence_supported` (bool), `why` (str).

## Pass bar for the loop

- **Every** alert in a live 5-alert run must score **≥95**.
- Mean server-side duration must stay **≤300s**.
- Both measured server-side (Firestore), independent of client network.
