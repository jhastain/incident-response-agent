import os
from dataclasses import dataclass
from functools import lru_cache

import litellm
from google.adk.models.lite_llm import LiteLlm

from .app_utils.bf_headers import AttributingLiteLLMClient

# When a Bifrost/MCP toolset transiently returns an EMPTY tool list under the
# Tier-1 parallel fan-out, the affected agent issues a tool call with no tools
# declared. Anthropic (via Vertex/LiteLLM) rejects that:
#   "Anthropic doesn't support tool calling without `tools=` param specified."
# which crashed the whole run mid-Tier-1 (confirmed live 2026-06-25, #1002).
# modify_params lets LiteLLM inject a dummy tool so the request stays valid and
# the run continues; the on_tool_error_callback then handles the empty result.
litellm.modify_params = True


@dataclass(frozen=True, slots=True)
class AgentSettings:
    gcp_project: str
    gcp_location: str
    model_id: str


@dataclass(frozen=True, slots=True)
class BifrostSettings:
    mcp_url: str
    llm_url: str
    api_token: str


def _required_gateway_url(name: str) -> str:
    """Read a Bifrost gateway URL from env, or raise.

    ADO-200022. These used to default to https://llm-gateway.internal.example — the
    PROD gateway. There is no safe default for a value that selects WHICH
    environment's gateway every LLM call and every MCP tool call is routed
    through: an unset var sent test/dev traffic through prod, billing prod and
    drawing test tool traffic across the boundary. Whether that surfaced at all
    depended on whether the virtual key in hand happened to be prod-valid, so
    the failure mode was "succeeds against the wrong environment", not "errors".

    Both deployed engines set these explicitly (scripts/deploy-test.ps1), and
    `--update-env-vars` REPLACES the whole env map, so a deploy that drops the
    flag now fails to start instead of silently re-pointing at prod. A dead
    engine is recoverable in minutes; a week of prod-routed test traffic is not.
    """
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(
            f"{name} is not set. It selects which Bifrost gateway (and therefore which "
            "environment) every LLM and MCP call is routed through, so there is no safe "
            "default — a wrong value succeeds against the wrong environment. Set it from "
            ".env.example (dev/test/prod URLs differ) or from the deploy's --update-env-vars."
        )
    return value


@lru_cache
def get_agent_settings() -> AgentSettings:
    return AgentSettings(
        gcp_project=os.environ.get("GOOGLE_CLOUD_PROJECT", ""),
        gcp_location=os.environ.get("GOOGLE_CLOUD_LOCATION", "us-central1"),
        model_id=os.environ.get("AGENT_MODEL_ID", "claude-sonnet-4-6"),
    )


@lru_cache
def get_bifrost_settings() -> BifrostSettings:
    return BifrostSettings(
        mcp_url=_required_gateway_url("BIFROST_MCP_URL"),
        llm_url=_required_gateway_url("BIFROST_LLM_URL"),
        api_token=os.environ.get("BIFROST_API_TOKEN", ""),
    )


def _make(model_id: str) -> LiteLlm:
    """Build a model routed through the Bifrost gateway.

    Every LLM call goes through Bifrost rather than direct Vertex. Bifrost owns
    provider selection, region pinning and failover: its normalize-*-model rules
    pin each Claude model to the env's `aiops-<env>-project-global` Vertex key
    with an `anthropic/` fallback and 3 retries with backoff. That replaces the
    in-process region fallback this module used to carry — a single gateway
    handles a regional 503 without every agent reimplementing the retry.

    The `openai/vertex/<model>` prefix is what the sibling agents use: LiteLLM
    treats Bifrost as an OpenAI-compatible endpoint, and the `vertex/` segment
    names the upstream provider for Bifrost's routing rules to resolve.
    """
    bifrost = get_bifrost_settings()
    return LiteLlm(
        model=f"openai/vertex/{model_id}",
        api_base=bifrost.llm_url,
        api_key=bifrost.api_token,
        # Stamps this run's invocation_id onto the Bifrost log record so
        # app_utils/bifrost_usage.py can read back THIS run's billed cost.
        llm_client=AttributingLiteLLMClient(),
    )


def create_model() -> LiteLlm:
    """Full model for agents, routed through Bifrost."""
    return _make(get_agent_settings().model_id)


def create_fast_model() -> LiteLlm:
    """Faster model (Haiku) for investigation agents, routed through Bifrost.

    Override via FAST_MODEL_ID; set to "same" to use the full model. The model
    id is the bare name (claude-haiku-4-5), NOT the -YYYYMMDD suffix — that is
    what both Bifrost's allowed_models list and the Vertex publisher expect.
    """
    fast_id = os.environ.get("FAST_MODEL_ID", "claude-haiku-4-5")
    if fast_id == "same":
        return create_model()
    return _make(fast_id)


def create_report_model() -> LiteLlm:
    """Stronger model for the report agent, routed through the Bifrost gateway.

    The report agent owns the TL;DR and the whole prose/format discipline of
    the Teams post. On Haiku it ignored the brevity rules outright (a live run
    on 2026-07-30 produced 6,856 chars against an 1,800-char cap, with 21 of
    ~30 bullets over the 12-word limit) and copied the prompt's own markdown
    fence into its output, so Teams rendered the report as a code block.
    It has no tools, so the cost delta is one generation per investigation.

    Unlike the other factories this one does NOT pin `vertex/`: no GCP project
    has Vertex quota for the Opus tier — verified live 2026-07-30 against
    aiops-dev-project AND aiops-prod-project, where claude-opus-5 and
    claude-opus-4-8 both return a persistent 429
    (`online_prediction_input_tokens_per_minute_per_base_model`) in us-east5 and
    a hard 400 "not servable in region" in us-central1. Bifrost holds the
    Anthropic capacity, so the bare `openai/<model>` form lets its routing rules
    pick the provider that can actually serve Opus instead of forcing a Vertex
    attempt that always fails over.

    Override the model with REPORT_MODEL_ID.
    """
    model_id = os.environ.get("REPORT_MODEL_ID", "claude-opus-5")
    bifrost = get_bifrost_settings()
    return LiteLlm(
        model=f"openai/{model_id}",
        api_base=bifrost.llm_url,
        api_key=bifrost.api_token,
        llm_client=AttributingLiteLLMClient(),
    )


def create_verification_model() -> LiteLlm:
    """Create the LLM model for the verification agent.

    Uses claude-sonnet-4-6 (settings.model_id) for reliability. gemini-2.5-flash
    was the original choice for cross-model independence, but via the LiteLlm/
    Vertex path it intermittently returns zero output tokens (~1/3 of runs
    produced no verification score; non-fatal since {verification_result?} no
    longer crashes persistence, but the score was missing). The Claude path is
    reliable across all 15 investigation agents. Trade-off: verification shares
    the investigation model family, but it remains a separate agent that does not
    see the report agent's self-assessment (no anchoring). Override with
    VERIFICATION_MODEL_ID if a reliable diverse model is identified later.
    """
    # Haiku-only build: verification defaults to the fast model (Haiku) too.
    # Override with VERIFICATION_MODEL_ID to restore a diverse/stronger judge.
    # Routed through Bifrost like the other models.
    fast_id = os.environ.get("FAST_MODEL_ID", "claude-haiku-4-5")
    verification_model = os.environ.get("VERIFICATION_MODEL_ID", fast_id)
    return _make(verification_model)
