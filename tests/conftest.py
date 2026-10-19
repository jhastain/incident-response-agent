"""Test-suite environment defaults.

ADO-200022 made `BIFROST_MCP_URL` / `BIFROST_LLM_URL` required rather than
defaulting to the prod gateway (app/config.py). Importing almost any `app.*`
module builds a model via `config.create_*_model()`, which reads those settings
at import time, so the unit suite needs them present before collection.

They are set to an obviously-fake host, not to any real environment's URL. No
unit test makes a network call — if one ever starts to, it must fail with an
unresolvable name rather than quietly reaching a live gateway. This is also why
the values are NOT read from a developer's `.env`: a test run must not depend on
which environment the machine happens to be pointed at.

Set at conftest import — i.e. before pytest collects any test module — because a
fixture would run too late: the failure happens while importing the module under
test. `setdefault`, so an explicit value in the ambient environment still wins
and an intentional integration run can point at a real gateway.
"""
import os

_TEST_ENV_DEFAULTS = {
    "BIFROST_MCP_URL": "https://bifrost.invalid/mcp",
    "BIFROST_LLM_URL": "https://bifrost.invalid/v1",
    # Same treatment for the PagerDuty receiver's engine URL, which ADO-200022 also made
    # required with no default (it used to fall back to the live dev engine). Any test
    # importing integrations.pagerduty.main pulls in agent_client, which reads this at
    # import. Points at a project/engine that cannot exist rather than a real one.
    "IR_AGENT_ENGINE_URL": (
        "https://us-central1-aiplatform.googleapis.com/v1beta1/"
        "projects/0/locations/us-central1/reasoningEngines/0"
    ),
}

for _name, _value in _TEST_ENV_DEFAULTS.items():
    os.environ.setdefault(_name, _value)
