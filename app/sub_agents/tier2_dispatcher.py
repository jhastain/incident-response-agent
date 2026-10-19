"""Tier 2 Dispatcher Agent — Routes to specialized Tier 2 investigation agents.

This agent uses the AgentTool pattern to wrap each Tier 2 agent as a callable tool.
Based on the classification category and synthesis findings, it decides which Tier 2
agents to call for deeper investigation.
"""

from google.adk.agents import LlmAgent
from google.adk.tools.agent_tool import AgentTool

from ..categories import TIER2_DISPATCH_MATRIX
from ..config import create_fast_model
from .tier2.auth_security import auth_security_agent
from .tier2.cache import cache_agent
from .tier2.codebase import codebase_agent
from .tier2.pubsub import pubsub_agent
from .tier2.sql_dba import sql_dba_agent

# Wrap each Tier 2 agent as a callable tool using AgentTool
pubsub_tool = AgentTool(agent=pubsub_agent)
cache_tool = AgentTool(agent=cache_agent)
sql_dba_tool = AgentTool(agent=sql_dba_agent)
auth_security_tool = AgentTool(agent=auth_security_agent)
codebase_tool = AgentTool(agent=codebase_agent)

# Build the dispatch matrix into the prompt
dispatch_instructions = "\n".join(
    f"- **{cat}**: Call {', '.join(agents) if agents else 'NO Tier 2 agents (Tier 1 sufficient)'}"
    for cat, agents in TIER2_DISPATCH_MATRIX.items()
)

DISPATCHER_INSTRUCTION = f"""\
You are the Tier 2 Dispatcher for the Incident Response Agent.

You have access to 5 specialized investigation agents as tools.
Based on the alert classification and synthesis findings, you decide which
Tier 2 agents to call for deeper investigation.

## Alert Classification
{{classification}}

## Synthesis Findings
{{synthesis}}

## Dispatch Matrix
Use this matrix to determine which agents to call based on the category:
{dispatch_instructions}

## Instructions
1. Read the classification category from the alert context.
2. Look up the dispatch matrix to determine which Tier 2 agents to call.
3. If the category maps to NO Tier 2 agents (empty list), immediately return
   "No Tier 2 investigation needed — Tier 1 findings are sufficient for this category."
   Call NO agent tools in this case. Do not investigate further. Stop.
4. Otherwise, call EACH agent the matrix maps to this category — and ONLY those —
   with a brief summary of what to investigate.
5. Collect and return all Tier 2 findings.

## IMPORTANT — strict matrix obedience (hard rule)
- The dispatch matrix is authoritative. Call EXACTLY the agents it maps to this
  category — no more, no fewer.
- NEVER call an agent that is not mapped to this category, even if the synthesis
  findings seem to suggest it. Extra dispatches waste the run's limited time budget
  and can cause the final report to be cut off before it is produced.
- If the mapped list is empty, call nothing and return the "no Tier 2 needed" message.
- Pass relevant context from the synthesis to each agent you do call.
- Call mapped agents in parallel when possible (all agents are independent).

## Agent Descriptions
- **pubsub_agent**: Investigates Pub/Sub subscription backlogs, dead-letter queues, consumer failures
- **cache_agent**: Investigates Redis/Valkey cache health, memory, eviction, connection pools
- **sql_dba_agent**: Expert SQL Server DBA — blocking, CPU spikes, slow queries, deadlocks
- **auth_security_agent**: Investigates auth failures, DDoS, certificates, WAF events
- **codebase_agent**: Searches ADO code repositories for error messages and recent changes (use as last resort)
"""

tier2_dispatcher = LlmAgent(
    model=create_fast_model(),
    name="tier2_dispatcher",
    description="Dispatches Tier 2 investigation agents based on alert category and synthesis findings.",
    instruction=DISPATCHER_INSTRUCTION,
    output_key="tier2_findings",
    tools=[pubsub_tool, cache_tool, sql_dba_tool, auth_security_tool, codebase_tool],
)
