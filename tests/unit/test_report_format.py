"""Report-prompt format guards.

Both regressions here were observed live on 2026-07-30 in the SRE - Alerts Teams
channel: the report agent copied the prompt's own ```markdown fence into its
output (Teams rendered the whole post as one code block), and there was no TL;DR
line for a reader skimming the thread.
"""

from app.sub_agents.report import REPORT_INSTRUCTION


class TestNoCodeFence:
    """The template must not contain a fence the model can copy into its output."""

    def test_instruction_has_no_triple_backtick_fence(self):
        # Inline `backticks` are fine; ``` is what Teams renders as a code block.
        assert "```" not in REPORT_INSTRUCTION, (
            "REPORT_INSTRUCTION contains a ``` fence. The report agent copies it "
            "verbatim, and Teams then renders the entire post as a code block."
        )

    def test_instruction_forbids_fencing_the_report(self):
        assert "NEVER WRAP THE REPORT IN A CODE FENCE" in REPORT_INSTRUCTION


class TestTldr:
    """The report opens with a hedged one-liner keyed to the confidence label."""

    def test_template_opens_with_tldr(self):
        assert "**TL;DR:**" in REPORT_INSTRUCTION

    def test_tldr_rules_present(self):
        assert "TL;DR RULES" in REPORT_INSTRUCTION

    def test_hedge_ladder_covers_every_confidence_label(self):
        # One phrasing per label so the hedge is chosen by evidence, not by tone.
        for phrase in (
            "failed because",
            "most likely failed because",
            "may have failed because",
            "cause undetermined",
        ):
            assert phrase in REPORT_INSTRUCTION, f"missing hedge phrasing: {phrase}"

    def test_output_format_requires_tldr_first(self):
        assert "Start with `**TL;DR:**`" in REPORT_INSTRUCTION


class TestNoAgentDiagnostics:
    """ADO-200013: the report carries incident findings only.

    The telemetry table and the Process footer described how the *agent* ran
    (tokens, tool calls, elapsed time, agent count, incident_id) — not what was
    found. They had no consumer outside the eval harness's report-format score,
    and most cells emitted "not instrumented" anyway.
    """

    def test_no_telemetry_table(self):
        assert "Investigation Telemetry" not in REPORT_INSTRUCTION
        assert "| Agent Summary | Tokens | Tool Calls | Status |" not in REPORT_INSTRUCTION

    def test_no_process_footer(self):
        assert "**Process:**" not in REPORT_INSTRUCTION
        assert "incident_id: `<incident_id>`" not in REPORT_INSTRUCTION
        assert "<elapsed>s wall time" not in REPORT_INSTRUCTION

    def test_closing_line_reference_is_not_the_process_line(self):
        # The fence rule names the report's last line; it must not still point
        # at the removed Process footer.
        assert "ends with the Process line" not in REPORT_INSTRUCTION
        assert "Your output starts with `**TL;DR:**`" in REPORT_INSTRUCTION

    def test_rule_forbids_agent_self_diagnostics(self):
        assert "No agent self-diagnostics" in REPORT_INSTRUCTION


class TestRemovedSections:
    """ADO-200018 cut the report to four sections.

    ADO-200013 argued What Was Checked should be retained: it is the ✅/⚠️
    coverage list, a factual finding about the investigation's coverage, and it
    was never one of the harness's scored sections. ADO-200018 supersedes that
    argument — the PagerDuty report is now TL;DR, Incident Summary, What
    Happened, and Key Findings (STRONG tier only), and nothing else. The
    coverage information still exists upstream in each sub-agent's findings
    (shared_prompts.OUTPUT_FORMAT_TEMPLATE) and in synthesis; it just is not
    rendered to the responder any more.
    """

    def test_the_cut_sections_are_gone(self):
        # ADO-200018. Every string here was asserted PRESENT before this change
        # (test_scored_sections_retained / test_what_was_checked_retained).
        # ADO-200020 restored "**Links:**" — see TestLinksSection for why that is
        # not a reversal: it returns as a bare URL list, not a findings section.
        for section in (
            "**Root Cause (",
            "**Timeline (Central):**",
            "**Recommended Actions (Human Decision Required):**",
            "**What Was Checked:**",
            "**Historical Precedent:**",
        ):
            assert section not in REPORT_INSTRUCTION, f"section not removed: {section}"


class TestKeptSections:
    """The four surviving sections and the footer."""

    def test_the_four_surviving_sections_are_present(self):
        for section in (
            "**TL;DR:**",
            "Incident Summary — <short_description>",
            "**What Happened:**",
            "**Key Findings:**",
        ):
            assert section in REPORT_INSTRUCTION, f"missing kept section: {section}"

    def test_key_findings_is_strong_tier_only(self):
        assert "STRONG EVIDENCE ONLY" in REPORT_INSTRUCTION
        assert "**MODERATE EVIDENCE:**" not in REPORT_INSTRUCTION
        assert "**WEAK SIGNAL:**" not in REPORT_INSTRUCTION

    def test_tldr_hedge_is_anchored_to_synthesis_not_a_report_section(self):
        assert "Committed Root Cause" in REPORT_INSTRUCTION

    def test_mechanism_bullet_survives_in_key_findings(self):
        # firestore_tools._root_cause_of parses this bullet out of the stored
        # report to build historical context (ADO-200005). It must stay.
        assert "- Mechanism:" in REPORT_INSTRUCTION

    def test_generated_by_footer_retained(self):
        assert "_Generated by the Incident Response Agent — verify before acting._" in (
            REPORT_INSTRUCTION
        )


class TestLinksSection:
    """The trailing Links block is sparse and directly attributable.

    Previously, an unbounded link dump caused 12+ URLs (generic dashboards,
    unrelated error-tracking issues, exploratory queries) to overwhelm Teams notes.
    The Links section is now strictly capped at <=3 primary links directly attributable
    to the incident mechanism and Key Findings, with generic dashboards and unrelated
    background errors strictly excluded.
    """

    def test_links_section_present(self):
        assert "**Links:**" in REPORT_INSTRUCTION
        assert "LINKS SECTION" in REPORT_INSTRUCTION

    def test_links_are_directly_attributable(self):
        assert "DIRECTLY ATTRIBUTABLE" in REPORT_INSTRUCTION
        assert "Strict Exclusions" in REPORT_INSTRUCTION

    def test_dedup_required(self):
        assert "Deduplicate by URL" in REPORT_INSTRUCTION
        assert "produce ONE entry" in REPORT_INSTRUCTION

    def test_empty_case_omits_the_header(self):
        # ADO-200013/285912 trap: a section that cannot be satisfied must be
        # omitted, not emitted empty and not padded with a placeholder.
        assert "omit the `**Links:**` header entirely" in REPORT_INSTRUCTION
        assert "Never emit a bare header" in REPORT_INSTRUCTION

    def test_verified_is_defined_as_retrieved_this_run(self):
        assert "a tool returned the URL itself" in REPORT_INSTRUCTION
        assert "contains the exact identifier the URL addresses" in REPORT_INSTRUCTION

    def test_speculative_urls_are_excluded(self):
        # Acceptance criterion 4: the prompt must say so explicitly.
        assert "assembled it speculatively" in REPORT_INSTRUCTION
        assert "A dead or invented URL" in REPORT_INSTRUCTION
        assert "LEAVE IT OUT" in REPORT_INSTRUCTION

    def test_sources_that_cannot_yield_a_link_are_named(self):
        # Do not mandate links from tools that cannot produce one — that is what
        # drives fabrication. Verified against the tool source 2026-08-12.
        assert "Sources that CANNOT yield a verified link" in REPORT_INSTRUCTION
        for src in ("GCP metrics", "Cloud Run", "Octopus", "Compute Engine"):
            assert src in REPORT_INSTRUCTION, f"unlinkable source not named: {src}"

    def test_verifiable_sources_match_what_the_tools_actually_return(self):
        # These three are code-built by tools and returned as literal dict values.
        for key in ("logs_explorer_url", "permalink"):
            assert key in REPORT_INSTRUCTION, f"missing tool URL key: {key}"

    def test_inline_links_still_mandatory(self):
        assert "SOURCE LINKS ARE MANDATORY" in REPORT_INSTRUCTION
        assert "does NOT replace inline links" in REPORT_INSTRUCTION

    def test_links_entry_cap_is_three(self):
        assert "Maximum 3 links" in REPORT_INSTRUCTION

    def test_strict_exclusions_named(self):
        assert "Generic dashboards" in REPORT_INSTRUCTION
        assert "Unrelated background errors" in REPORT_INSTRUCTION
        assert "Broad exploratory log queries" in REPORT_INSTRUCTION


class TestApiEndpointUrlsAreNeverSanctioned:
    """ADO-200020 Defect B: a delivered report shipped a REST endpoint as a link.

    The live provenance audit (5 runs, 48 URLs) found alert01 emitting
    `[ADO work item 36130](https://dev.azure.com/acme/_apis/wit/workItems/36130)`.
    Not fabricated — `fetch_known_issues` returned that exact string in the
    work-item object's `url` field, four times. But `/_apis/` is the REST
    endpoint: verified live 2026-08-12 it answers `content-type: application/json`,
    so a responder clicking it gets a raw payload, not the item.
    """

    def test_no_apis_url_appears_as_a_documented_format(self):
        """Every `_apis/` mention must sit inside a paragraph that forbids it.

        Paragraph-scoped, not line-scoped: the prohibition wraps across lines,
        so a per-line scan would flag its own continuation.
        """
        spec = REPORT_INSTRUCTION[
            REPORT_INSTRUCTION.index("## SOURCE LINKS ARE MANDATORY"):
        ]
        for para in spec.split("\n\n"):
            if "_apis/" not in para:
                continue
            assert "NEVER emit" in para or "never ships" in para or (
                "one sanctioned edit" in para
            ), f"an _apis/ URL appears outside a prohibition: {para!r}"

    def test_the_can_yield_list_offers_no_apis_url(self):
        # The bullet list of emittable formats must be free of endpoints.
        can = REPORT_INSTRUCTION[
            REPORT_INSTRUCTION.index("Sources that CAN yield a verified link"):
            REPORT_INSTRUCTION.index("**NEVER emit an `_apis/` URL.**")
        ]
        assert "_apis/" not in can, f"API endpoint listed as an emittable format: {can!r}"

    def test_the_api_endpoint_form_is_explicitly_banned(self):
        assert "**NEVER emit an `_apis/` URL.**" in REPORT_INSTRUCTION
        assert "REST API endpoint, not a page" in REPORT_INSTRUCTION
        # The lesson is general, not a work-item special case.
        assert "A URL containing `/_apis/`" in REPORT_INSTRUCTION

    def test_a_returned_url_field_does_not_license_an_api_endpoint(self):
        # The precise collision that produced the defect: the model followed
        # "a tool returned the URL itself" over the prohibition.
        assert (
            '"A tool returned\nit" does NOT make an API endpoint a usable link.'
            in REPORT_INSTRUCTION
        )

    def test_the_conversion_is_the_only_sanctioned_edit_to_a_returned_url(self):
        assert "convert it to" in REPORT_INSTRUCTION
        assert "or cite the source by name with no link" in REPORT_INSTRUCTION


class TestAdoWorkItemLinkFormat:
    """ADO-200020: work items stay linkable, but as the browsable page.

    A known-issue work item is real evidence for a responder, so the fix is to
    correct the URL form rather than ban the source. Format verified live
    2026-08-12 against dev.azure.com with a bearer token:
      /acme/_workitems/edit/36130                  -> 302 to the project page
      /acme/AcmeProjects/_workitems/edit/36130 -> 200 text/html
      /acme/_apis/wit/workItems/36130              -> 200 application/json
    """

    def test_work_items_are_no_longer_on_the_cannot_list(self):
        cannot = REPORT_INSTRUCTION[
            REPORT_INSTRUCTION.index("Sources that CANNOT yield a verified link"):
            REPORT_INSTRUCTION.index("## LINKS SECTION")
        ]
        assert "ADO work items" not in cannot
        # The rest of the list is untouched.
        for src in ("Compute Engine", "Firestore", "GCS", "infra-context", "Teams"):
            assert src in cannot, f"unlinkable source dropped from CANNOT list: {src}"

    def test_browsable_work_item_format_is_documented(self):
        can = REPORT_INSTRUCTION[
            REPORT_INSTRUCTION.index("Sources that CAN yield a verified link"):
            REPORT_INSTRUCTION.index("Sources that CANNOT yield a verified link")
        ]
        assert "ADO work item" in can
        assert "https://dev.azure.com/acme/_workitems/edit/<id>" in can
        assert "_apis/wit/workItems" not in can.split("**NEVER emit")[0]

    def test_the_org_level_form_is_the_documented_default(self):
        # Live-verified redirect. The project segment cannot be guessed: the
        # batch tool omits System.TeamProject unless it is explicitly requested,
        # and the project= call argument does not indicate where the item lives.
        assert "Use this ORG-LEVEL form — no project segment" in REPORT_INSTRUCTION
        assert "302-redirects to the item's real project page" in REPORT_INSTRUCTION
        assert "`System.TeamProject` value for THAT item appears in the tool output" in (
            REPORT_INSTRUCTION
        )
        assert "is not evidence of where the item lives" in REPORT_INSTRUCTION


class TestCannotListPrecedence:
    """ADO-200020: the CANNOT list must beat "a tool returned the URL itself".

    Without this the same collision recurs with the next tool that emits a URL
    field for an unlinkable source.
    """

    def test_cannot_list_explicitly_wins(self):
        assert "**The CANNOT list WINS over every other link rule.**" in REPORT_INSTRUCTION
        assert (
            "even when a tool returns a `url`, `href`, `link`, or `permalink` field for it"
            in REPORT_INSTRUCTION
        )

    def test_the_verified_hard_rule_defers_to_the_prohibitions(self):
        assert "Both bullets are subordinate to the two prohibitions above" in (
            REPORT_INSTRUCTION
        )
        assert '"A tool returned it" is the weaker rule and loses.' in REPORT_INSTRUCTION

    def test_precedence_does_not_weaken_the_verified_rule_itself(self):
        # Guard the constraint: the hard rule stays hard.
        assert '**"Verified" is a hard rule, not a preference.**' in REPORT_INSTRUCTION
        assert "a tool returned the URL itself" in REPORT_INSTRUCTION


class TestVerbatimUrlCopying:
    """ADO-200020 Defect A: two links were rewritten when a verbatim one existed.

    alert04 #1 reordered the params of a returned `traces_explorer_url` and
    dropped `historicalData=true&paused=true`; #9 composed a logs URL when a
    real `logs_explorer_url` was available. Same destination, but the emitted
    string no longer matches any tool output, so provenance is unauditable.
    """

    def test_verbatim_copying_is_required_not_preferred(self):
        assert "**Copy the link a tool returned BYTE-FOR-BYTE**" in REPORT_INSTRUCTION
        assert 'Copying is not "preferred", it is required' in REPORT_INSTRUCTION

    def test_the_specific_rewrites_the_audit_caught_are_named(self):
        assert "do not\n  reorder query params" in REPORT_INSTRUCTION
        assert "historicalData" in REPORT_INSTRUCTION

    def test_format_assembly_is_the_fallback_not_the_default(self):
        assert "Only build from a format when NO tool returned a" in REPORT_INSTRUCTION


class TestGoldExample:
    """The gold example is what the pre-merge gate check is run against."""

    @staticmethod
    def _gold() -> str:
        start = REPORT_INSTRUCTION.index("**TL;DR:** Ephemeral-SQL builds failed")
        end = REPORT_INSTRUCTION.index("## Severity Emojis")
        return REPORT_INSTRUCTION[start:end]

    def test_gold_example_carries_a_links_section(self):
        assert "**Links:**" in self._gold()

    def test_gold_example_links_have_real_urls_not_placeholders(self):
        import re

        gold = self._gold()
        links = gold[gold.index("**Links:**"):]
        urls = re.findall(r"\]\(([^)]+)\)", links)
        assert len(urls) >= 3, f"gold Links section too thin: {urls}"
        for u in urls:
            assert u.startswith("https://"), f"placeholder URL in gold example: {u}"

    def test_gold_example_has_no_placeholder_url_anywhere(self):
        # The pre-285912 gold example used a literal "(url)" placeholder, which
        # taught the model that an unresolved link was acceptable output.
        assert "](url)" not in self._gold()

    def test_gold_example_demonstrates_sparse_links(self):
        # Gold links section is sparse (<=3 links directly attributable to Key Findings).
        # Generic dashboards and exploratory queries are excluded.
        gold = self._gold()
        links_block = gold[gold.index("**Links:**"):]
        assert "dashboard/aaa-111-bbb" not in links_block

    def test_gold_example_scores_five_of_five_on_the_eval_gate(self):
        """Mirror of tests/eval/run_agent_eval.sh's five-key section scorer.

        ADO-200020 adds no sixth key — see the work item's constraint 4. This
        asserts the restored section does not disturb the existing five.
        """
        import re

        report_text = self._gold()
        rt = report_text.lower()
        sections = {
            "has_tldr": "**tl;dr:**" in rt,
            "has_severity_header": bool(re.search(r"sev|🟢|🟠|🔴", report_text)),
            "has_what_happened": "what happened" in rt,
            "has_key_findings": "key findings" in rt,
            "has_mechanism": "mechanism:" in rt,
        }
        assert sum(sections.values()) == 5, f"gate not 5/5: {sections}"


class TestEvidenceBackedClaims:
    """Every remaining claim must be evidence-backed or stated as unknown.

    Guards the conventions the template already relies on rather than adding
    new ones: the no-fabrication rule, the shared anti-fabrication injection,
    and the two explicit "we don't know" markers.
    """

    def test_no_fabrication_rule_present(self):
        assert "**No fabrication**" in REPORT_INSTRUCTION

    def test_uncited_gap_marker_present(self):
        assert "[UNCITED — gap]" in REPORT_INSTRUCTION

    def test_undetermined_marker_present(self):
        assert "UNDETERMINED" in REPORT_INSTRUCTION

    def test_anti_fabrication_rule_injected(self):
        from app.shared_prompts import ANTI_FABRICATION_RULE

        assert ANTI_FABRICATION_RULE.strip() in REPORT_INSTRUCTION
