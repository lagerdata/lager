# Copyright 2024-2026 Lager Data LLC
# SPDX-License-Identifier: Apache-2.0

"""MCP guide resources — context the agent reads at session start."""

from __future__ import annotations

import json


def register(mcp):
    @mcp.resource("lager://guide/overview")
    def guide_overview() -> str:
        """What is Lager, how does it work, and what is a 'net'."""
        return (
            "# Lager — Hardware-in-the-Loop Test Platform\n"
            "\n"
            "A **Lager box** is a small Linux computer that sits next to your "
            "device-under-test (DUT).  It has physical connections to the DUT "
            "via instruments — power supplies, debug probes, logic analysers, "
            "protocol adapters (SPI/I2C/UART), USB hubs, GPIO, and more.\n"
            "\n"
            "Each physical connection is represented as a **net** — a named "
            "handle you use in code.  For example, `supply1` might be a power "
            "supply set to 3.3 V, `spi1` might be an SPI bus wired to a flash "
            "chip, and `uart1` might be the DUT's debug console.\n"
            "\n"
            "You interact with hardware through the **`lager` Python API**:\n"
            "\n"
            "```python\n"
            "from lager import Net, NetType\n"
            "\n"
            "psu = Net.get('supply1', type=NetType.PowerSupply)\n"
            "psu.set_voltage(3.3)\n"
            "psu.enable()\n"
            "```\n"
            "\n"
            "Scripts run **directly on the box** (via `lager python` or through "
            "this MCP server's `run_test_script` tool), so hardware calls are "
            "local with sub-millisecond latency — no network round trips between "
            "steps.\n"
        )

    @mcp.resource("lager://guide/workflow")
    def guide_workflow() -> str:
        """Recommended workflow: discover → plan → write → run → analyse."""
        return (
            "# Recommended Test Workflow\n"
            "\n"
            "## 1. Discover\n"
            "Call `discover_bench()` to see what hardware is available: nets, "
            "instruments, and capabilities.  For details on a specific net, call "
            "`discover_bench(net_name)`.  Nets with metadata (description, "
            "test_hints, tags) will tell you exactly what they're wired to on "
            "the DUT.\n"
            "\n"
            "## 2. Plan\n"
            "Call `plan_firmware_test(firmware_description, test_goals)` with a "
            "description of what the firmware does and what you want to validate. "
            "It cross-references the bench's nets and their metadata to produce "
            "a structured test plan.\n"
            "\n"
            "## 3. Learn the API\n"
            "Read `lager://reference/{net_type}` for the Python API of the "
            "relevant net types (PowerSupply, UART, SPI, etc.).  Call "
            "`get_test_example(pattern)` to see a proven, runnable example "
            "script.\n"
            "\n"
            "## 4. Write a Test Script\n"
            "Author a complete Python script using `from lager import Net, "
            "NetType`.  Write it like a normal Python program — the box is "
            "the test runner.  Use try/finally blocks to clean up hardware "
            "state.  Print results to stdout; optionally print a JSON summary "
            "on the last line for structured output.\n"
            "\n"
            "## 5. Run\n"
            "Call `run_test_script(python_code)`.  The script executes on the "
            "box and returns stdout, stderr, exit code, and duration.  If the "
            "last stdout line is JSON, it's parsed into the result.\n"
            "\n"
            "## 6. Analyse & Iterate\n"
            "Review the output.  If assertions fail or results are unexpected, "
            "adjust the script and re-run.  Use `quick_io(net_name)` for "
            "spot-checks between runs.\n"
        )

    @mcp.resource("lager://guide/api-quick-reference")
    def guide_api_quick_reference() -> str:
        """Compact cheat sheet of the lager.Net API by NetType."""
        from ..data.api_reference import API_REFERENCE

        lines = [
            "# lager.Net API Quick Reference\n",
            "```python",
            "from lager import Net, NetType",
            "net = Net.get('name', type=NetType.X)",
            "```\n",
        ]

        for type_name, ref in sorted(API_REFERENCE.items()):
            lines.append(f"## {type_name}  (`{ref['net_type_enum']}`)")
            lines.append(f"```python\n{ref['get_pattern']}\n```")
            for m in ref["methods"]:
                lines.append(f"- `{m['sig']}` — {m['desc']}")
            lines.append("")

        return "\n".join(lines)
