# IR Agent: SequentialAgent today vs ADK Graph Workflow

Companion to `docs/ir-agent-graph-comparison.drawio`. 2-minute talk track.

Verified 2026-08-26 against `app/agent.py`, `app/tier1_routing.py`,
`app/sub_agents/tier1/parallel.py`, `app/sub_agents/tier2_dispatcher.py`,
`google.adk.workflow` (installed ADK 2.5.0), and adk.dev.

## One-slide summary

| | Today | Graph workflow |
|---|---|---|
| Shape | `SequentialAgent` of 9 steps, one `ParallelAgent` barrier | `Workflow` of nodes + declared edges |
| Tier 1 routing | `before_agent_callback` returns `Content` to skip; skipped agent writes a `NOT RUN --` sentinel | `Event(route=[...])` + edge dict; unrouted node is not on the graph |
| Tier 2 routing | an LLM reads the dispatch matrix from its prompt | a `FunctionNode` returns routes from code |
| Per-node retry | none | `retry_config` (max_attempts, backoff, jitter, exceptions) |
| Per-node timeout | none | `timeout` on every node |
| Failure blast radius | whole run lost | that node retries |
| Resume after deadline | none | dynamic workflows skip completed nodes |
| Loops / re-work | impossible in `SequentialAgent` | back-edge is legal |
| Data handoff | untyped session-state string keys | `input_schema` / `output_schema` Pydantic |

## Feature-by-feature

| Feature | Today | Graph | Delta |
|---|---|---|---|
| Fan-out Tier 1 | `ParallelAgent`, all 9 instantiated | edge fan-out, only routed nodes | none functionally; cleaner |
| Fan-in | implicit barrier | explicit `JoinNode` | equal, plus a new wedge risk |
| Skip semantics | sentinel string every downstream prompt must parse | node absent | fewer prompt rules |
| Decision tree | Firestore -> `tier1_plan` dict -> callbacks | Firestore -> `Event(route=...)` | same data, declared instead of imperative |
| Learning loop | persistence -> hit_rate -> tree | unchanged | none |
| Human-in-the-loop | not present | `RequestInput` pause/resume node | new capability |
| Observability | `DatadogRunPlugin`, per-sub-agent spans | per-node events | unverified whether the plugin binds to nodes |

## Speed

| | Today | Graph |
|---|---|---|
| LLM calls per run | same set | same set |
| Tier 1 wall time | slowest routed agent | slowest routed agent |
| Router cost | ~0.39s Firestore read | ~0.39s Firestore read |
| Net | baseline | **no measurable change** |

Speed is not the reason to do this. Skipped agents already cost only a scheduling
slot. The only real latency win would be from `resume` avoiding a full re-run
after a deadline, and that is a recovery win, not a throughput win.

## Accuracy

| Failure class | Today | Graph | Helps? |
|---|---|---|---|
| Malformed handoff between steps | untyped strings | `output_schema` enforced | yes, small |
| Tier 2 dispatcher over-calling agents | prompt says "strict matrix obedience" | matrix is code | yes |
| Sentinel misread as "checked, found nothing" | mitigated by a long warning string | node simply absent | yes |
| Teams-context red herring | open defect | unchanged | **no** - prompt/evidence problem |
| Hallucinated root cause | verification agent scores it | unchanged | no |

## Architecture

| | Today | Graph |
|---|---|---|
| Where routing lives | Python callbacks + prompt text, 3 files | one `edges=[...]` list |
| Readability | must read `parallel.py` + `tier1_routing.py` + prompts | read the edge map |
| Testability | mock the callback context | assert on routes |
| Coupling to ADK internals | relies on `before_agent_callback` returning `Content` to set `end_invocation` (pinned to google-adk 2.5.0) | supported public API |
| Root object | `App(root_agent=SequentialAgent)` | `Workflow` is a `BaseNode`, **not** a `BaseAgent` |

## Open risks (call these out)

- `google.adk.workflow.Workflow` is not a `BaseAgent` subclass in the installed build.
  `App(root_agent=...)` and the Agent Engine deploy path assume an agent. Compatibility unverified.
- `JoinNode` wedges if any upstream branch emits no Event. Every Tier 1 node needs a failsafe output.
- adk.dev lists graph workflows as incompatible with some third-party integrations; the IR agent
  runs Bifrost MCP and GCP MCP toolsets. Not checked.

## Recommendation

Not a rewrite. Adopt in this order, each independently valuable:

1. `retry_config` + `timeout` per node - biggest reliability win, smallest change.
2. `tier2_dispatcher` LLM -> `FunctionNode` route - removes a model from a decision that is a lookup table.
3. Typed `output_schema` on classifier and synthesis handoffs.
4. Resume / checkpointing, only after the Agent Engine compatibility question is answered.
