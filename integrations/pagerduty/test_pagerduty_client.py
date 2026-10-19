"""Unit tests for the pure logic (signature verify + message building). No network."""

import hashlib
import hmac

import pagerduty_client as pd  # run from within integrations/pagerduty/


def _sign(body: bytes, secret: str) -> str:
    return "v1=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def test_verify_signature_valid():
    body = b'{"event":{"event_type":"incident.triggered"}}'
    secret = "shh"
    assert pd.verify_signature(body, _sign(body, secret), secret) is True


def test_verify_signature_multiple_candidates_rotation():
    body = b"payload"
    secret = "current"
    header = "v1=deadbeef, " + _sign(body, secret)  # old + current during rotation
    assert pd.verify_signature(body, header, secret) is True


def test_verify_signature_bad():
    body = b"payload"
    assert pd.verify_signature(body, "v1=deadbeef", "secret") is False


def test_verify_signature_missing_header():
    assert pd.verify_signature(b"x", None, "secret") is False


def test_verify_signature_tampered_body():
    secret = "s"
    sig = _sign(b"original", secret)
    assert pd.verify_signature(b"tampered", sig, secret) is False


_GCP = (
    "https://console.cloud.google.com/logs/query;query=resource.type%3D%22cloud_function%22"
    "%20AND%20timestamp%3E%3D%222026-08-19T03%3A24%3A00Z%22?project=platform-prod-project"
)


def test_flatten_renders_a_single_link_as_label_colon_url():
    # PagerDuty notes are plain text: [label](url) would render literally and its
    # linkifier eats the trailing ')'. 'label: url' is the form PD linkifies cleanly.
    out = pd._flatten_links_for_pagerduty(f"- [GCP function logs]({_GCP})")
    assert out == f"- GCP function logs: {_GCP}"
    assert "](" not in out


def test_flatten_prints_a_repeated_url_only_on_its_last_occurrence():
    md = (
        f"- Zero function app logs 03:24-03:27Z - [GCP logs]({_GCP})\n"
        "\n**Links:**\n"
        f"- [GCP function logs 03:24-03:27Z]({_GCP})\n"
    )
    out = pd._flatten_links_for_pagerduty(md)
    assert out.count(_GCP) == 1
    # inline citation keeps its label, loses the duplicate URL
    assert "- Zero function app logs 03:24-03:27Z - GCP logs\n" in out
    # the Links trail is last, so it is the copy that keeps the URL
    assert out.endswith(f"- GCP function logs 03:24-03:27Z: {_GCP}\n")


def test_flatten_keeps_distinct_urls_that_share_a_label():
    a, b = "https://us5.datadoghq.com/monitors/1", "https://us5.datadoghq.com/monitors/2"
    out = pd._flatten_links_for_pagerduty(f"[Monitor]({a}) [Monitor]({b})")
    assert out == f"Monitor: {a} Monitor: {b}"


def test_build_alert_message_flattens_and_includes_details():
    data = {
        "id": "QEXAMPLE000002",
        "number": 1001,
        "title": "CoreSvc PROD - Error Rate High",
        "service": {"summary": "Platform - Datadog - High"},
        "urgency": "high",
        "priority": {"summary": "P2"},
        "status": "triggered",
        "created_at": "2026-07-21T02:56:50Z",
        "html_url": "https://acme.pagerduty.com/incidents/QEXAMPLE000002",
    }
    msg = pd.build_alert_message(data, "resource_name POST /api/admin/login\nerror rate > 150")
    assert "QEXAMPLE000002" in msg
    assert "Platform - Datadog - High" in msg
    assert "P2" in msg
    assert "api/admin/login" in msg
    assert "\n" not in msg  # flattened to one line


def test_build_alert_message_no_details():
    data = {"id": "X", "number": 1, "title": "t", "service": {}, "urgency": "low",
            "priority": None, "status": "triggered", "created_at": "", "html_url": ""}
    msg = pd.build_alert_message(data, "")
    assert "Alert details" not in msg


def test_encode_semicolons_only_inside_urls():
    # PD's linkifier stops at a literal ';', which truncates every GCP Logs Explorer
    # link at '/logs/query'. Verified in the Teams card on incident QEXAMPLE000004.
    out = pd._encode_semicolons_in_urls(f"Timeline; then: {_GCP}")
    assert out.startswith("Timeline; then: ")  # prose semicolon untouched
    assert "/logs/query%3Bquery=" in out
    assert ";" not in out.split(": ", 1)[1]


def test_encode_semicolons_is_idempotent():
    once = pd._encode_semicolons_in_urls(_GCP)
    assert pd._encode_semicolons_in_urls(once) == once


def test_encode_semicolons_leaves_urls_without_one_alone():
    url = "https://us5.datadoghq.com/monitors/10000001"
    assert pd._encode_semicolons_in_urls(url) == url
