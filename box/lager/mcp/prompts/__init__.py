# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

"""MCP prompt templates.

Prompts surface as slash-command-style entry points in MCP clients (e.g.
Cursor). They don't do work themselves — each one returns a short, opinionated
instruction that steers the agent through the read-only discover → plan →
write → run-over-CLI workflow this server is built around.
"""

from __future__ import annotations


def register(mcp) -> None:
    """Register prompt templates on the given FastMCP server."""

    @mcp.prompt(
        name="write_lager_test",
        title="Write a Lager hardware test",
        description=(
            "Guide the agent to discover the bench/DUT, plan, write a Python "
            "test, and run it on the box via the lager CLI."
        ),
    )
    def write_lager_test(what_to_test: str) -> str:
        return (
            f"I want to test the following on this Lager bench: {what_to_test}\n\n"
            "Work through it in this order:\n"
            "1. Call discover_bench() to see the hardware and the box address to "
            "use for --box, and discover_dut() to learn what the DUT is.\n"
            "2. Call plan_firmware_test(firmware_description, test_goals) to get "
            "a phased plan with the exact Net API methods for each net.\n"
            "3. Call cite_schematic(<net>) for any net you need to reason about "
            "electrically, and read lager://guide/api-quick-reference (or "
            "get_test_example(<query>)) to confirm the API.\n"
            "4. Write a Python test file locally using `from lager import Net, "
            "NetType`. Respect any advisory_limits from discover_bench().\n"
            "5. Run it from the shell with the box address discover_bench() "
            "reported: `lager python path/to/test.py --box <box-address>` "
            "(a folder with a main.py works too). This server cannot run the "
            "test for you.\n"
            "6. Read the CLI output and iterate."
        )

    @mcp.prompt(
        name="explore_bench",
        title="Explore this Lager bench",
        description="Orient on what this box is, what it tests, and what it can run.",
    )
    def explore_bench() -> str:
        return (
            "Help me understand this Lager bench before I write any tests.\n\n"
            "1. Call discover_dut() for the DUT purpose, MCU, peripherals, and "
            "doc references.\n"
            "2. Call discover_bench() for the nets, instruments, interfaces, and "
            "capability summary.\n"
            "3. Summarize what this bench is for and which kinds of tests it can "
            "support. If something looks unconfigured (no nets, no DUT context), "
            "tell me what to run to fix it."
        )

    @mcp.prompt(
        name="assess_test_feasibility",
        title="Can this bench run a test?",
        description="Check whether the bench has the capabilities for a given test.",
    )
    def assess_test_feasibility(test_description: str) -> str:
        return (
            f"Can this bench run the following test? {test_description}\n\n"
            "Call assess_suitability(<test_description>) and explain the result: "
            "which capabilities matched, what's missing, any substitutions, and "
            "the confidence. If it can't run as-is, tell me what hardware or net "
            "configuration would be needed."
        )
