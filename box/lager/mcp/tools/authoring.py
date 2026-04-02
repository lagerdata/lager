# Copyright 2024-2026 Lager Data LLC
# SPDX-License-Identifier: Apache-2.0

"""MCP tools for test authoring guidance — API docs, examples, test planning."""

from __future__ import annotations

import json

from ..server import mcp


@mcp.tool()
def get_api_reference(net_type: str) -> str:
    """Get the Python API reference for a specific net type.

    Returns method signatures, parameter descriptions, common gotchas,
    and an example snippet.  Use this to learn how to write on-box test
    scripts for a given hardware type.

    Args:
        net_type: Net type name (e.g., "PowerSupply", "UART", "SPI", "GPIO",
                  "I2C", "Debug", "ADC", "DAC", "Battery", "ELoad",
                  "Thermocouple", "WattMeter", "Usb", "EnergyAnalyzer").
                  Also accepts raw net_type strings like "power-supply", "uart".
    """
    from ..data.api_reference import get_reference_for_type, list_supported_types

    ref = get_reference_for_type(net_type)
    if ref is None:
        return json.dumps({
            "error": f"No API reference for net type '{net_type}'.",
            "supported_types": list_supported_types(),
        })
    return json.dumps(ref, indent=2)


@mcp.tool()
def get_test_example(query: str) -> str:
    """Get a real, runnable test script example matching a query.

    Searches the test corpus by net type, pattern name, or keyword.
    Returns the matching pattern info and (when the script file exists
    on this box) the full script source.

    Args:
        query: Search query — a net type ("SPI", "UART"), a pattern name
               ("spi_flash_readback", "uart_loopback"), or a keyword
               ("power", "flash", "temperature").
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


@mcp.tool()
def plan_firmware_test(firmware_description: str, test_goals: str) -> str:
    """Generate a structured test plan for validating firmware on this bench.

    Cross-references the firmware description and test goals against the
    bench's available nets, their metadata (descriptions, test_hints, tags),
    and capability graph to produce actionable test steps.

    Args:
        firmware_description: What the firmware does (e.g., "BLE temperature
            sensor that reads I2C thermometer, transmits over BLE, has a
            UART debug console").
        test_goals: What to validate (e.g., "verify I2C reads correct
            temperature, UART outputs debug logs, power consumption under
            5mA in sleep mode").
    """
    from ..data.api_reference import get_reference_for_type
    from ..server_state import get_bench

    bench = get_bench()

    goal_words = set((firmware_description + " " + test_goals).lower().split())

    net_info = []
    for n in bench.nets:
        relevance_score = 0
        for tag in n.tags:
            if tag.lower() in goal_words:
                relevance_score += 5
        for hint in n.test_hints:
            for word in hint.lower().split():
                if word in goal_words:
                    relevance_score += 1
        if n.description:
            for word in n.description.lower().split():
                if word in goal_words:
                    relevance_score += 2
        net_type_lower = n.net_type.lower().replace("-", "")
        for w in goal_words:
            if w in net_type_lower or net_type_lower in w:
                relevance_score += 3

        net_info.append({
            "name": n.name,
            "net_type": n.net_type,
            "description": n.description,
            "dut_connection": n.dut_connection,
            "test_hints": n.test_hints,
            "tags": n.tags,
            "relevance_score": relevance_score,
        })

    relevant_nets = sorted(
        [n for n in net_info if n["relevance_score"] > 0],
        key=lambda x: x["relevance_score"],
        reverse=True,
    )
    other_nets = [n for n in net_info if n["relevance_score"] == 0]

    test_steps = []
    for net in relevant_nets:
        ref = get_reference_for_type(net["net_type"])
        step = {
            "net": net["name"],
            "net_type": net["net_type"],
            "description": net["description"] or f"{net['net_type']} net",
            "dut_connection": net["dut_connection"],
        }
        if net["test_hints"]:
            step["suggested_tests"] = net["test_hints"]
        if ref:
            step["api_pattern"] = ref["get_pattern"].replace(
                ref["get_pattern"].split('"')[1], net["name"]
            )
            step["key_methods"] = [m["sig"] for m in ref["methods"][:5]]
            if ref.get("gotchas"):
                step["gotchas"] = ref["gotchas"][:2]
        test_steps.append(step)

    missing = []
    fw_lower = firmware_description.lower()
    type_keywords = {
        "ble": "BLE", "bluetooth": "BLE", "wifi": "WiFi", "wi-fi": "WiFi",
        "ethernet": "Ethernet", "can": "CAN", "lin": "LIN",
    }
    bench_type_set = {n.net_type.lower().replace("-", "") for n in bench.nets}
    for keyword, label in type_keywords.items():
        if keyword in fw_lower and not any(keyword in t for t in bench_type_set):
            missing.append(f"{label} — mentioned in firmware description but no matching net on this bench")

    plan = {
        "firmware": firmware_description,
        "goals": test_goals,
        "test_plan": test_steps,
        "other_available_nets": [
            {"name": n["name"], "net_type": n["net_type"]}
            for n in other_nets
        ],
    }
    if missing:
        plan["missing_coverage"] = missing

    return json.dumps(plan, indent=2)
