#!/usr/bin/env python3
# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

"""Write test/mcp/fixtures/engine-views-golden.json: every fleet view over fixed manifests.

The golden is the engine's contract with a host that reimplemented these
views before the engine service existed, or that wants to prove a deployed
service answers like this release: it records, for a fixed set of manifests
and arguments, exactly what ``lager.mcp.engine.fleet`` returns.

Replaceable sentences (``fleet.MESSAGES``) are recorded as the placeholder
``{{message:<key>}}``, so a host that words them its own way can substitute
its wording and compare. ``test_engine_golden`` fails when the file is stale.

Usage:
    python tools/gen_engine_golden.py          # rewrite the fixture
    python tools/gen_engine_golden.py --check  # exit 1 if it would change
"""

from __future__ import annotations

import copy
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FIXTURES = os.path.join(ROOT, "test", "mcp", "fixtures")
MANIFEST = os.path.join(FIXTURES, "fleet-manifest-v1.json")
GOLDEN = os.path.join(FIXTURES, "engine-views-golden.json")

#: Built-in test type keys, free text that maps onto them, and free text that maps onto nothing.
DESCRIPTIONS = [
    "gpio_validation",
    "gpio_button_validation",
    "firmware_flash_and_boot",
    "power_consumption",
    "spi_slave_validation",
    "uart_loopback",
    "i2c_validation",
    "battery_discharge",
    "adc_measurement",
    "waveform_capture",
    "UART LOOPBACK",
    "flash the firmware and verify boot banner over UART",
    "capture the DAC output on the oscilloscope",
    "measure the boot current of the board",
    "check the board responds",
]

#: Benches that exist only as capability bindings, for suitability and search.
BINDING_BENCHES = {
    "empty": [],
    "psu_uart_debug": [
        {"role": "source_power", "target": "psu1", "confidence": 1.0},
        {"role": "measure", "target": "psu1", "confidence": 0.9},
        {"role": "protocol_master", "target": "uart0", "confidence": 1.0},
        {"role": "capture_protocol", "target": "uart0", "confidence": 0.8},
        {"role": "flash_firmware", "target": "debug1", "confidence": 1.0},
        {"role": "control_state", "target": "debug1", "confidence": 1.0},
        {"role": "run_local_program", "target": "_box", "confidence": 1.0},
    ],
    "logic_only": [
        {"role": "capture_logic", "target": "logic0", "confidence": 1.0},
        {"role": "run_local_program", "target": "_box", "confidence": 1.0},
    ],
    "unknown_roles_only": [
        {"role": "teleport", "target": "x0", "confidence": 1.0},
        {"role": "levitate", "target": "x1"},
    ],
}


#: Manifest variants whose bindings or box id differ, so suitability can differ. The
#: other variants change only what the renderings read.
SUITABILITY_CASES = {"manifest-v1", "no-nets", "box-id-from-manifest", "empty-bench-box-id"}


def _cases(base: dict) -> list[tuple[str, dict]]:
    def variant(name, change):
        m = copy.deepcopy(base)
        change(m)
        return name, m

    cases = [("manifest-v1", copy.deepcopy(base))]
    cases.append(variant("no-dut", lambda m: m["bench"].__setitem__("dut_slots", [])))
    cases.append(variant("no-reference-entries", lambda m: m.pop("reference_entries", None)))
    cases.append(variant("empty-reference-entries", lambda m: m.__setitem__("reference_entries", {})))
    cases.append(variant("no-nets", lambda m: m["bench"].update(nets=[], capability_bindings=[])))
    cases.append(variant("box-id-from-manifest", lambda m: m["bench"].pop("box_id", None)))
    cases.append(variant("empty-bench-box-id", lambda m: m["bench"].__setitem__("box_id", "")))
    cases.append(variant("inactive-dut", lambda m: [d.__setitem__("active", False) for d in m["bench"]["dut_slots"]]))
    cases.append(variant("calibration-empty-object", lambda m: m["bench"].__setitem__("calibration", {})))
    cases.append(variant("calibration-null", lambda m: m["bench"].__setitem__("calibration", None)))
    cases.append(variant(
        "advisory-limits",
        lambda m: m["bench"].__setitem__("constraints", {"max_voltage": 5.0, "max_current": None}),
    ))
    for name, bindings in BINDING_BENCHES.items():
        cases.append((f"bindings:{name}", {
            "schema_version": 1,
            "content_hash": "0" * 64,
            "box_id": name,
            "bench": {"box_id": name, "capability_bindings": copy.deepcopy(bindings)},
        }))
    return cases


def build() -> dict:
    sys.path.insert(0, os.path.join(ROOT, "box"))
    from lager.mcp.engine import fleet  # pylint: disable=import-outside-toplevel

    messages = {key: "{{message:%s}}" % key for key in fleet.MESSAGES}
    with open(MANIFEST, encoding="utf-8") as fh:
        base = json.load(fh)

    out_cases = []
    for name, m in _cases(base):
        nets = [n["name"] for n in m["bench"].get("nets", [])]
        views: dict = {
            "summary": fleet.summary(m, messages),
            "dut": fleet.dut_context(m, messages),
            "dut_overview": fleet.dut_overview(m, messages),
        }
        for net in nets + ["no_such_net"]:
            views[f"net:{net}"] = fleet.net_detail(m, net)
            views[f"cite:{net}"] = fleet.cite(m, net, messages)
        for label, sel, gotchas, examples in (
            ("all", None, False, False),
            ("all+gx", None, True, True),
            ("first+g", nets[:1], True, False),
            ("unknown", ["no_such_net"], False, True),
        ):
            views[f"reference:{label}"] = fleet.reference(m, sel, gotchas, examples, messages)
        if name in SUITABILITY_CASES or name.startswith("bindings:"):
            for description in DESCRIPTIONS:
                views[f"suitability:{description}"] = fleet.suitability(m, description)
        out_cases.append({"name": name, "manifest": m, "views": views})

    search_benches = [
        {"key": name, "box_id": name, "capability_bindings": copy.deepcopy(b)} for name, b in BINDING_BENCHES.items()
    ] + [{"key": "broken", "box_id": "broken", "capability_bindings": "not a list"}]
    return {
        "generated_by": "tools/gen_engine_golden.py",
        "message_placeholder": "{{message:<key>}}",
        "message_keys": sorted(fleet.MESSAGES),
        "cases": out_cases,
        "search": [
            {"description": d, "benches": search_benches, "result": fleet.search(d, search_benches)}
            for d in ("uart_loopback", "waveform_capture", "check the board responds")
        ],
    }


def render(doc: dict) -> str:
    return json.dumps(doc, indent=1, sort_keys=True, ensure_ascii=False) + "\n"


def main(argv: list[str]) -> int:
    text = render(build())
    if "--check" in argv:
        with open(GOLDEN, encoding="utf-8") as fh:
            if fh.read() != text:
                print(f"{GOLDEN} is stale; run: python tools/gen_engine_golden.py", file=sys.stderr)
                return 1
        print("engine golden is current")
        return 0
    with open(GOLDEN, "w", encoding="utf-8") as fh:
        fh.write(text)
    print(f"wrote {GOLDEN}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
