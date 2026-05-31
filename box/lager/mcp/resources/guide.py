# Copyright 2024-2026 Lager Data
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
            "chip, and `uart1` might be the DUT's debug console.  Each net "
            "exposes a one-line `purpose` (*what does this wire do on the "
            "DUT?*) and optional `notes`.  Nets are grouped into **subsystems** "
            "(*Power tree*, *Flash subsystem*, *Debug*, ...) on the DUT.\n"
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
            "Scripts run **directly on the box** via `lager python`, so hardware "
            "calls are local with sub-millisecond latency — no network round "
            "trips between steps.\n"
        )

    @mcp.resource("lager://guide/workflow")
    def guide_workflow() -> str:
        """Recommended workflow: orient → discover → plan → write → run → analyse."""
        return (
            "# Recommended Test Workflow\n"
            "\n"
            "## 0. Orient (read this first)\n"
            "Read `lager://dut/overview.md` for a narrative briefing on the "
            "DUT: what this box tests, the MCU/peripherals, the subsystems, "
            "and which documents (schematics, datasheets) to fetch. If you "
            "need the structured shape instead, read `lager://dut/context` "
            "or call `discover_dut()`.\n"
            "\n"
            "**Schematics and datasheets are NOT stored on the box.** The "
            "DUT context advertises references with either a `url` or a "
            "`repo_path` (relative to your project, which is synced to the "
            "box when you run `lager python ... --box <box-ip>`). Open them "
            "with your own file tools — vision models analyse per-sheet "
            "PNG exports faster than full PDFs. Call "
            "`cite_schematic(net_name)` to get just the doc refs and page "
            "hints relevant to one net.\n"
            "\n"
            "## 1. Discover\n"
            "Call `discover_bench()` to see what hardware is available: nets, "
            "instruments, and capabilities.  For details on a specific net, call "
            "`discover_bench(net_name)` — the response now includes the "
            "subsystem the net belongs to and any relevant doc refs.\n"
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
            "script.  For anything not covered here — full method signatures, "
            "every net type, error handling — read the hosted docs listed in "
            "`lager://guide/docs` (start at "
            "https://docs.lagerdata.com/llms.txt).\n"
            "\n"
            "## 4. Write a Test File\n"
            "Author a Python test file locally using `from lager import Net, "
            "NetType`.  Write it like a normal Python program.  Use try/finally "
            "blocks to clean up hardware state.  Your full project is available "
            "on-box (dtest, SerialDevice, custom modules, etc.).\n"
            "\n"
            "## 5. Run\n"
            "Execution does not happen over MCP. Identify the box by the **IP "
            "address** you connected to this MCP server on "
            "(`http://<box-ip>:8100/mcp`) — local box names are arbitrary "
            "client-side aliases, so the IP is the only identifier you can rely "
            "on. `--box` accepts a raw IP directly, so no registration is "
            "needed:\n"
            "```\n"
            "# Run a single test file\n"
            "lager python path/to/test.py --box <box-ip>\n"
            "\n"
            "# ...or run a whole folder (its entrypoint must be main.py).\n"
            "# Everything in the folder is synced and importable, so you can\n"
            "# ship reusable helper modules alongside the test.\n"
            "lager python path/to/test_dir --box <box-ip>\n"
            "```\n"
            "This syncs your local project to the box and runs the script with "
            "full project context.  Output streams back to your terminal. "
            "(Optionally, `lager boxes add --name <name> --ip <box-ip>` "
            "registers a friendly alias — only useful for a stable name or a "
            "non-default SSH user.)\n"
            "\n"
            "## 6. Analyse & Iterate\n"
            "Review the CLI output.  If assertions fail or results are "
            "unexpected, adjust the script and re-run it with `lager python`.\n"
        )

    @mcp.resource("lager://guide/docs")
    def guide_docs() -> str:
        """Where to find the full, authoritative Lager documentation online."""
        return (
            "# Lager Documentation\n"
            "\n"
            "The resources and tools on this server are a curated subset for "
            "fast orientation. For complete API details, every net type, edge "
            "cases, and anything not covered here, read the hosted docs with "
            "your own web-fetch tool:\n"
            "\n"
            "- **Docs home:** https://docs.lagerdata.com\n"
            "- **Python SDK (the `lager.Net` API you write tests against):** "
            "https://docs.lagerdata.com/source/reference/python/overview\n"
            "- **CLI reference (`lager python`, `lager boxes`, `lager nets`, "
            "...):** https://docs.lagerdata.com/source/reference/cli\n"
            "- **Full page index for agents:** "
            "https://docs.lagerdata.com/llms.txt\n"
            "\n"
            "Fetch `llms.txt` first — it lists every page so you can discover "
            "what exists before opening specific pages.\n"
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
