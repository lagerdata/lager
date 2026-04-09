# Copyright 2024-2026 Lager Data LLC
# SPDX-License-Identifier: Apache-2.0

"""MCP tools for test authoring guidance — examples and test planning."""

from __future__ import annotations

import json
import re

from ..audit import audited
from ..server import mcp

# ── Ordered phases for structuring a test plan ────────────────────────────

_PHASE_ORDER = {
    "power-supply": 0, "power-supply-2q": 0, "battery": 0, "eload": 0,
    "debug": 1,
    "uart": 2, "spi": 2, "i2c": 2,
    "gpio": 3, "adc": 3, "dac": 3,
    "watt-meter": 4, "thermocouple": 4, "energy-analyzer": 4,
    "usb": 5, "webcam": 5,
}

_PHASE_LABELS = {
    0: "setup_power",
    1: "flash_and_boot",
    2: "protocol_tests",
    3: "io_tests",
    4: "measurement",
    5: "peripherals",
}

_POWER_SIGNALS = {"power", "power-cycle", "powercycle", "vbus", "vcc", "vdd", "source_power"}


def _infer_phase(net) -> int:
    """Determine the test phase for a net based on type and metadata.

    Nets whose metadata indicates a power role (e.g. a USB hub used for
    DUT power cycling) are promoted to the setup_power phase regardless
    of their net_type.
    """
    metadata_words = set()
    for tag in net.tags:
        metadata_words.add(tag.lower())
    for role in getattr(net, "roles", []):
        metadata_words.add(role.lower())
    if net.description:
        metadata_words.update(net.description.lower().split())

    if metadata_words & _POWER_SIGNALS:
        return 0  # setup_power

    return _PHASE_ORDER.get(net.net_type, 5)


@mcp.tool()
@audited()
def get_test_example(query: str) -> str:
    """Find runnable test script examples by net type, pattern name, or keyword.

    Args:
        query: Net type ("SPI"), pattern ("spi_flash_readback"), or keyword ("power").
    """
    from ..data.test_patterns import find_pattern, get_script_content

    matches = find_pattern(query)
    if not matches:
        return json.dumps({"error": f"No test examples matching '{query}'."})

    results = []
    for m in matches[:3]:
        entry = {
            "pattern": m["pattern_key"],
            "description": m["description"],
            "net_types": m["net_types"],
            "tags": m.get("tags", []),
            "script_path": m["script"],
        }
        content = get_script_content(m["script"])
        if content:
            entry["script_content"] = content
        results.append(entry)

    return json.dumps(results, indent=2)


def _score_net(net, goal_words: set[str]) -> int:
    """Score a net's relevance to the goal using word overlap."""
    score = 0
    for tag in net.tags:
        if tag.lower() in goal_words:
            score += 5
    for hint in net.test_hints:
        for word in hint.lower().split():
            if word in goal_words:
                score += 1
    if net.description:
        for word in net.description.lower().split():
            if word in goal_words:
                score += 2
    net_type_lower = net.net_type.lower().replace("-", "")
    for w in goal_words:
        if w in net_type_lower or net_type_lower in w:
            score += 3
    return score


@mcp.tool()
@audited()
def plan_firmware_test(firmware_description: str, test_goals: str) -> str:
    """Generate a phased test plan with full API references for each net.

    Returns structured phases (power -> flash -> protocols -> IO ->
    measurement -> peripherals), each containing the nets to use, complete
    method signatures, gotchas, and example snippets so the agent can
    write the test script without additional lookups.

    Args:
        firmware_description: What the firmware does.
        test_goals: What to validate.
    """
    from ..data.api_reference import get_reference_for_type
    from ..server_state import get_bench

    bench = get_bench()
    goal_words = set((firmware_description + " " + test_goals).lower().split())

    scored_nets = []
    for n in bench.nets:
        score = _score_net(n, goal_words)
        scored_nets.append((n, score))

    relevant = sorted(
        [(n, s) for n, s in scored_nets if s > 0],
        key=lambda x: x[1],
        reverse=True,
    )
    other = [n for n, s in scored_nets if s == 0]

    phases: dict[int, list] = {}
    for net, _score in relevant:
        phase_idx = _infer_phase(net)
        ref = get_reference_for_type(net.net_type)

        step: dict = {
            "net": net.name,
            "net_type": net.net_type,
            "description": net.description or f"{net.net_type} net",
            "dut_connection": net.dut_connection,
        }
        if net.test_hints:
            step["suggested_tests"] = net.test_hints

        if ref:
            # Replace the first double-quoted placeholder net name in the
            # canonical get_pattern with the real net name. Falls back to
            # the unmodified pattern if it has no quoted placeholder, so
            # a future api_reference entry without that shape can never
            # raise IndexError here.
            step["get_pattern"] = re.sub(
                r'"[^"]*"', f'"{net.name}"', ref["get_pattern"], count=1
            )
            step["methods"] = [
                {"sig": m["sig"], "desc": m["desc"]} for m in ref["methods"]
            ]
            if ref.get("gotchas"):
                step["gotchas"] = ref["gotchas"]
            if ref.get("example_snippet"):
                step["example_snippet"] = ref["example_snippet"]

        phases.setdefault(phase_idx, []).append(step)

    ordered_phases = []
    for idx in sorted(phases):
        ordered_phases.append({
            "phase": _PHASE_LABELS.get(idx, "other"),
            "nets": phases[idx],
        })

    missing = []
    fw_lower = firmware_description.lower()
    type_keywords = {
        "ble": "BLE", "bluetooth": "BLE", "wifi": "WiFi", "wi-fi": "WiFi",
        "ethernet": "Ethernet", "can": "CAN", "lin": "LIN",
    }
    bench_type_set = {n.net_type.lower().replace("-", "") for n in bench.nets}
    for keyword, label in type_keywords.items():
        if keyword in fw_lower and not any(keyword in t for t in bench_type_set):
            missing.append(f"{label} — mentioned in firmware but no matching net on bench")

    plan: dict = {
        "firmware": firmware_description,
        "goals": test_goals,
        "phases": ordered_phases,
        "other_available_nets": [
            {"name": n.name, "net_type": n.net_type} for n in other
        ],
    }
    if missing:
        plan["missing_coverage"] = missing

    return json.dumps(plan, indent=2)
