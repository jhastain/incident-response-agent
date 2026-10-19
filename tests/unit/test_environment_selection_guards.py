"""Regression tests: values that select an ENVIRONMENT must raise, never default.

Three incidents in one week, all the same shape — a hardcoded value silently chose the
wrong environment, and the run produced a complete, plausible, wrong result instead of an
error:

  ADO-200017  five eval scripts pinned engine 1000000000000000003, deleted and present in
              no project.
  ADO-200021  run_pool.sh pinned the dev project inside a block whose comment claimed it
              resolved live. A 10-alert batch meant to certify a TEST deploy ran entirely
              against a 2026-08-04 DEV build: 10/10 reports, sane timings, scored output,
              nothing visibly wrong.
  ADO-200022  local_run.py defaulted GOOGLE_CLOUD_PROJECT to aiops-prod-project, so a
              local eval with the var unset WROTE incidents to prod; config.py defaulted
              Bifrost to the PROD gateway.

The rule these tests lock in: for a value naming which project / engine / gateway /
datastore we are talking to, there is no safe default. A KeyError costs a minute; a
plausible report against the wrong environment costs a day and can certify a regression.

`app.tools.session_tools._resolve_engine_context` is the reference pattern and is already
covered by test_evidence_accuracy.py::test_raises_rather_than_using_a_stale_engine_id.
"""

import ast
import importlib
import os
import subprocess
import sys
import tokenize
from unittest import mock

import pytest

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))


def executable_code(path):
    """Return the file's code with comments and docstrings removed, string VALUES kept.

    These files deliberately DOCUMENT the literals they used to contain — that prose is
    the point of the fix and must not fail the test. But the literals under test are
    themselves string values (a URL, a project id), so a stripper that drops all strings
    would pass vacuously. So: drop `#` comments via tokenize, drop docstrings via the AST,
    and keep every other string constant.
    """
    source = open(path, encoding="utf-8").read()

    docstrings = set()
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if not isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            continue
        body = getattr(node, "body", None) or []
        if (body and isinstance(body[0], ast.Expr)
                and isinstance(body[0].value, ast.Constant)
                and isinstance(body[0].value.value, str)):
            docstrings.add((body[0].lineno, body[0].col_offset))

    kept = []
    with open(path, "rb") as f:
        for tok in tokenize.tokenize(f.readline):
            if tok.type == tokenize.COMMENT:
                continue
            if tok.type == tokenize.STRING and tok.start in docstrings:
                continue
            kept.append(tok.string)
    return " ".join(kept)


def code_lines(path, comment_prefix="#"):
    """Line-based comment stripper, for non-Python files (shell, markdown)."""
    source = open(path, encoding="utf-8").read()
    return "\n".join(
        line for line in source.splitlines()
        if not line.lstrip().startswith(comment_prefix)
    )


class TestBifrostGatewayRequired:
    """app/config.py — BIFROST_MCP_URL / BIFROST_LLM_URL (ADO-200022, L5).

    These used to default to https://llm-gateway.internal.example — prod. An unset var routed
    every LLM call and every MCP tool call through the prod gateway. Whether that even
    surfaced depended on whether the virtual key in hand happened to be prod-valid, so the
    failure mode was "succeeds against the wrong environment".
    """

    def _fresh_config(self):
        """Reimport config so the lru_cache on get_bifrost_settings does not mask the read."""
        import app.config as config

        config.get_bifrost_settings.cache_clear()
        return config

    @pytest.mark.parametrize("missing", ["BIFROST_MCP_URL", "BIFROST_LLM_URL"])
    def test_unset_gateway_url_raises(self, missing):
        config = self._fresh_config()
        env = dict(os.environ)
        env.pop(missing, None)
        env.setdefault("BIFROST_MCP_URL", "https://bifrost.invalid/mcp")
        env.setdefault("BIFROST_LLM_URL", "https://bifrost.invalid/v1")
        env.pop(missing, None)
        with mock.patch.dict(os.environ, env, clear=True):
            with pytest.raises(RuntimeError, match=missing):
                config.get_bifrost_settings()
        config.get_bifrost_settings.cache_clear()

    @pytest.mark.parametrize("blank", ["BIFROST_MCP_URL", "BIFROST_LLM_URL"])
    def test_blank_gateway_url_raises(self, blank):
        """An empty or whitespace value is as wrong as an absent one — `--update-env-vars`
        setting a var to "" must not read as "use the default"."""
        config = self._fresh_config()
        env = {
            "BIFROST_MCP_URL": "https://bifrost.invalid/mcp",
            "BIFROST_LLM_URL": "https://bifrost.invalid/v1",
            blank: "   ",
        }
        with mock.patch.dict(os.environ, env, clear=True):
            with pytest.raises(RuntimeError, match=blank):
                config.get_bifrost_settings()
        config.get_bifrost_settings.cache_clear()

    def test_no_prod_gateway_literal_remains_in_config(self):
        """The specific value that caused the incident must not come back as a default."""
        code = executable_code(os.path.join(REPO_ROOT, "app", "config.py"))
        assert "llm-gateway.internal.example" not in code

    def test_settings_read_from_env_when_set(self):
        """The guard must not break the normal path."""
        config = self._fresh_config()
        env = {
            "BIFROST_MCP_URL": "https://test.bifrost.example/mcp",
            "BIFROST_LLM_URL": "https://test.bifrost.example/v1",
            "BIFROST_API_TOKEN": "tok",
        }
        with mock.patch.dict(os.environ, env, clear=True):
            settings = config.get_bifrost_settings()
        assert settings.mcp_url == "https://test.bifrost.example/mcp"
        assert settings.llm_url == "https://test.bifrost.example/v1"
        config.get_bifrost_settings.cache_clear()


class TestPagerDutyEngineRequired:
    """integrations/pagerduty/agent_client.py — IR_AGENT_ENGINE_URL (ADO-200022, L6).

    Ran in a subprocess: the guard fires at module import, and the module is not otherwise
    importable from the unit suite (it is packaged separately for the receiver image).

    The old default was the DEV engine — alive, which is worse than dead. A receiver
    missing the var accepted the webhook, passed the HMAC check, returned 202, triaged
    against a stale dev build, and posted the note. Nothing errored anywhere.
    """

    def _import_with(self, env):
        return subprocess.run(
            [sys.executable, "-c",
             "import integrations.pagerduty.agent_client as m; print(m._ENGINE)"],
            cwd=REPO_ROOT, env=env, capture_output=True, text=True,
        )

    def test_unset_engine_url_raises_on_import(self):
        env = {k: v for k, v in os.environ.items() if k != "IR_AGENT_ENGINE_URL"}
        result = self._import_with(env)
        assert result.returncode != 0
        assert "IR_AGENT_ENGINE_URL is not set" in result.stderr

    def test_blank_engine_url_raises_on_import(self):
        env = dict(os.environ, IR_AGENT_ENGINE_URL="  ")
        result = self._import_with(env)
        assert result.returncode != 0
        assert "IR_AGENT_ENGINE_URL is not set" in result.stderr

    def test_set_engine_url_is_used_verbatim(self):
        url = "https://us-central1-aiplatform.googleapis.com/v1beta1/projects/1/locations/us-central1/reasoningEngines/2"
        result = self._import_with(dict(os.environ, IR_AGENT_ENGINE_URL=url))
        assert result.returncode == 0, result.stderr
        assert result.stdout.strip() == url

    def test_no_dev_engine_literal_remains_as_a_default(self):
        code = executable_code(
            os.path.join(REPO_ROOT, "integrations", "pagerduty", "agent_client.py")
        )
        assert "1000000000000000002" not in code
        assert "000000000002" not in code


class TestLocalEvalProjectRequired:
    """tests/eval/local_run.py — GOOGLE_CLOUD_PROJECT (ADO-200022, L4).

    The only landmine on a WRITE path. local_run.py builds root_agent in-process with the
    real persistence_agent; firestore_tools._get_db reads exactly this variable. Defaulted
    to aiops-prod-project, so an accuracy iteration wrote synthetic incidents into the
    prod store — whose `incidents` collection is empty, meaning the first such run would
    not merely pollute the corpus but create it, and find_similar_incidents would then
    serve those back as precedent.

    Subprocess again: the guard fires at import, before the agent is built.
    """

    SCRIPT = os.path.join("tests", "eval", "local_run.py")

    def test_unset_project_raises_before_any_write(self):
        env = {k: v for k, v in os.environ.items() if k != "GOOGLE_CLOUD_PROJECT"}
        # Pre-set the credentials the module loads first, so the failure under test is the
        # project guard and not a Secret Manager call.
        env.update({
            "BIFROST_API_TOKEN": "tok",
            "IR_AGENT_ADO_READONLY": "tok",
            "IR_AGENT_OCTOPUS_TASK_WATCHER": "tok",
        })
        result = subprocess.run(
            [sys.executable, self.SCRIPT, "nonexistent.txt", "out.json"],
            cwd=REPO_ROOT, env=env, capture_output=True, text=True,
        )
        assert result.returncode != 0
        assert "GOOGLE_CLOUD_PROJECT is not set" in result.stderr

    def test_no_prod_project_literal_remains_as_a_default(self):
        code = executable_code(os.path.join(REPO_ROOT, self.SCRIPT))
        assert "aiops-prod-project" not in code


class TestHarnessTargetsAreDerivedNotPinned:
    """The ADO-200017 idiom, extended to the Python harnesses that sweep missed.

    Each of these previously pinned an engine id or a project. Asserting on the SOURCE
    rather than on a resolved value keeps the test offline and hermetic — the point is
    that no literal is available to go stale, which is a property of the file.
    """

    DEAD_ENGINE = "1000000000000000003"   # verified absent from all three projects
    DEV_ENGINE = "1000000000000000002"    # alive in dev — a stale default that ANSWERS
    DEV_PROJECT_NUMBER = "000000000002"
    STALE_WORKTREE = "AIOps-agent-framework"

    FILES = [
        os.path.join("tests", "eval", "deployed_run.py"),
        os.path.join("tests", "eval", "judge_from_files.py"),
        os.path.join("tests", "eval", "judge_root_cause.py"),
        os.path.join("tests", "eval", "deep_timing.py"),
        os.path.join("tests", "eval", "timing_probe.py"),
        os.path.join("scripts", "backfill_verification_scores.py"),
    ]

    @pytest.mark.parametrize("relpath", FILES)
    def test_no_stale_environment_literal_in_code(self, relpath):
        code = executable_code(os.path.join(REPO_ROOT, relpath))
        for literal in (self.DEAD_ENGINE, self.DEV_ENGINE, self.DEV_PROJECT_NUMBER,
                        self.STALE_WORKTREE):
            assert literal not in code, f"{relpath} still pins {literal}"

    @pytest.mark.parametrize("relpath", FILES)
    def test_derives_from_deployment_metadata(self, relpath):
        """Every one of these resolves its target from the deploy artifact.

        judge_root_cause.py derives its Firestore project by importing judge_from_files'
        resolver rather than reading the file itself, so accept either the artifact name
        or that delegation — both mean "not pinned"."""
        code = executable_code(os.path.join(REPO_ROOT, relpath))
        derived = "deployment_metadata.json" in code or "resolve_project" in code
        assert derived, f"{relpath} does not derive its target"

    def test_judge_root_cause_repo_resolves_to_this_checkout(self):
        """L9: the pinned path was the stale worktree, which still exists on disk — so the
        judge read plausible older fixtures and wrote scores where nobody looked, and both
        ALERTS_DIR and RESULTS moved together so the output stayed self-consistent."""
        sys.path.insert(0, os.path.join(REPO_ROOT, "tests", "eval"))
        try:
            module = importlib.import_module("judge_root_cause")
            importlib.reload(module)
            assert os.path.normcase(os.path.abspath(module.REPO)) == os.path.normcase(REPO_ROOT)
            assert os.path.isdir(module.ALERTS_DIR)
        finally:
            sys.path.remove(os.path.join(REPO_ROOT, "tests", "eval"))


class TestRunPoolResolvesProject:
    """tests/channel-batch/assets/run_pool.sh — ADO-200021 (L1).

    The resolver's freshness logic was real (paginate, filter by displayName, sort by
    updateTime, take newest) but the search space was pinned to dev, so it could not go
    stale within dev and could never leave it — while the comment above it asserted the
    safety property the code did not have.
    """

    PATH = os.path.join("tests", "channel-batch", "assets", "run_pool.sh")

    @property
    def _code(self):
        return code_lines(os.path.join(REPO_ROOT, self.PATH))

    def test_no_dev_project_pinned(self):
        assert "000000000002" not in self._code

    def test_project_is_resolved_and_required(self):
        code = self._code
        assert "deployment_metadata.json" in code
        assert "${PROJECT}" in code or "$PROJECT" in code

    def test_echoes_the_resolved_target(self):
        """Both incidents were silent because nothing printed what was being measured."""
        code = self._code
        assert "project:" in code
        assert "target.txt" in code

    def test_sourced_retry_does_not_clobber_an_exported_workdir(self):
        """The documented single-alert retry exports WORKDIR and passes no positional arg.
        `${1:-$PWD}` overwrote it with $PWD, so run_one looked for an alert file that was
        not there — and still printed FINISHED."""
        assert "${1:-${WORKDIR:-$PWD}}" in self._code

    def test_failed_run_one_does_not_report_finished(self):
        """A retry that cannot work must not report success: every early return prints
        FAILED and returns non-zero, and an empty stream is a failure, not a fast pass."""
        code = self._code
        assert code.count("return 1") >= 4
        assert "EMPTY_STREAM" in code
        assert "NO_ALERT_FILE" in code


