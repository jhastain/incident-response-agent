"""Two properties, both easy to break silently: the sanitiser stays
byte-compatible with CodeAgent's, and nothing here raises.
"""

from __future__ import annotations

from app.app_utils import dd_tags


class FakeSpan:
    def __init__(self):
        self.tags = {}

    def set_tag(self, key, value):
        self.tags[key] = value


class TestSanitize:
    def test_lowercases_and_replaces_unsafe_chars(self):
        assert dd_tags.sanitize("Cloud Run / us-central1") == "cloud_run_/_us-central1"

    def test_none_and_blank_become_unknown(self):
        assert dd_tags.sanitize(None) == "unknown"
        assert dd_tags.sanitize("   ") == "unknown"

    def test_caps_at_200_chars(self):
        assert len(dd_tags.sanitize("x" * 500)) == 200


class TestAnnotateSpan:
    def test_prefixes_every_tag(self):
        span = FakeSpan()
        dd_tags.annotate_span(span, category="deploy-release", severity="sev2")
        assert span.tags == {
            "iragent.category": "deploy-release",
            "iragent.severity": "sev2",
        }

    def test_always_tagged_dimensions_default_to_unknown(self):
        """A missing tag drops the span out of a group-by entirely."""
        span = FakeSpan()
        dd_tags.annotate_span(span, category=None, severity="")
        assert span.tags["iragent.category"] == "unknown"
        assert span.tags["iragent.severity"] == "unknown"

    def test_optional_dimensions_are_skipped_when_empty(self):
        span = FakeSpan()
        dd_tags.annotate_span(span, incident_id=None)
        assert span.tags == {}

    def test_no_span_is_a_no_op(self):
        dd_tags.annotate_span(None, category="x")

    def test_a_raising_span_does_not_propagate(self):
        class Exploding:
            def set_tag(self, key, value):
                raise RuntimeError("boom")

        dd_tags.annotate_span(Exploding(), category="x")
