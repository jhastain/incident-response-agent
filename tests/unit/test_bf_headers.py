"""Bifrost attribution headers.

Without these, every log record for every concurrent investigation looks
identical and bifrost_usage cannot attribute cost to a run.
"""

from __future__ import annotations

import asyncio

from app.app_utils import bf_headers


class TestDimensions:
    def test_dimensions_become_x_bf_lh_headers(self):
        bf_headers.set_dimensions({"invocation_id": "inv1", "agent": "ir"})
        assert bf_headers.headers() == {
            "x-bf-lh-invocation_id": "inv1",
            "x-bf-lh-agent": "ir",
        }

    def test_empty_values_are_dropped_not_sent_blank(self):
        """A blank bucket reads like real attribution on a cost report."""
        bf_headers.set_dimensions({"invocation_id": "inv1", "agent": None,
                                   "extra": "  "})
        assert bf_headers.headers() == {"x-bf-lh-invocation_id": "inv1"}

    def test_crlf_cannot_forge_a_second_header(self):
        bf_headers.set_dimensions({"invocation_id": "a\r\nX-Evil: 1"})
        value = bf_headers.headers()["x-bf-lh-invocation_id"]
        assert "\r" not in value and "\n" not in value

    def test_concurrent_runs_do_not_share_dimensions(self):
        """Agent Engine serves sessions concurrently on one agent object."""
        seen = {}

        async def run(name):
            bf_headers.set_dimensions({"invocation_id": name})
            await asyncio.sleep(0)
            seen[name] = bf_headers.headers()["x-bf-lh-invocation_id"]

        async def main():
            await asyncio.gather(run("inv1"), run("inv2"))

        asyncio.run(main())
        assert seen == {"inv1": "inv1", "inv2": "inv2"}
