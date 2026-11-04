# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for lager.mcp.engine.fleet -- the engine's views of a stored manifest.

These views read the manifest a host stored, as a plain dict, so the rules
that matter are the reading rules: a key that is absent or ``None`` falls
back while an empty value is kept, an empty object or list is TRUE (a host
may have written the views in JavaScript first, and the views must agree
with it), a role this release does not know is skipped rather than fatal,
and a replaceable sentence comes from ``messages`` when given.
"""

import ast
import copy
import json
from pathlib import Path

import pytest

from lager.mcp.engine import fleet

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "fleet-manifest-v1.json"


@pytest.fixture
def manifest():
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


# --- purity -------------------------------------------------------------------


def test_fleet_imports_nothing_box_side():
    """The module the engine service runs on must not reach the server, tools, resources or live state."""
    tree = ast.parse(Path(fleet.__file__).read_text(encoding="utf-8"))
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            imported.add(("." * node.level) + (node.module or ""))
        elif isinstance(node, ast.Import):
            imported.update(a.name for a in node.names)
    forbidden = ("..server", "..server_state", "..tools", "..resources", ".instruments", ".bench_loader", "mcp")
    assert not [m for m in imported if m.startswith(forbidden)], imported


# --- summary ------------------------------------------------------------------


def test_summary_counts_roles_from_the_stored_bindings(manifest):
    out = fleet.summary(manifest)
    bindings = manifest["bench"]["capability_bindings"]
    assert out["total_capabilities"] == len(bindings)
    assert sum(out["capability_summary"].values()) == len(bindings)
    assert "run_tests" not in out  # how to run a test is the host's call
    assert [n["name"] for n in out["nets"]] == ["vin", "uart0", "reset"]


def test_summary_counts_a_role_this_release_does_not_know(manifest):
    manifest["bench"]["capability_bindings"].append({"role": "teleport", "target": "x"})
    assert fleet.summary(manifest)["capability_summary"]["teleport"] == 1


def test_box_id_falls_back_to_the_manifest_but_keeps_an_empty_bench_value(manifest):
    manifest["bench"].pop("box_id")
    manifest["box_id"] = "from-manifest"
    assert fleet.summary(manifest)["box_id"] == "from-manifest"
    manifest["bench"]["box_id"] = ""
    assert fleet.summary(manifest)["box_id"] == ""


def test_no_nets_warns_and_the_warning_is_replaceable(manifest):
    manifest["bench"]["nets"] = []
    assert fleet.summary(manifest)["warning"] == fleet.MESSAGES["no_nets"]
    assert fleet.summary(manifest, {"no_nets": "custom"})["warning"] == "custom"


def test_advisory_limits_only_when_a_limit_is_set(manifest):
    assert "advisory_limits" not in fleet.summary(manifest)
    manifest["bench"]["constraints"] = {"max_voltage": 5.0}
    limits = fleet.summary(manifest)["advisory_limits"]
    assert limits["max_voltage"] == 5.0 and limits["max_current"] is None


# --- messages -------------------------------------------------------------------


def test_unknown_message_key_is_refused():
    with pytest.raises(ValueError, match="unknown message key"):
        fleet.resolve_messages({"no_such": "x"})


def test_message_must_be_a_short_string():
    with pytest.raises(ValueError):
        fleet.resolve_messages({"no_dut": 5})
    with pytest.raises(ValueError):
        fleet.resolve_messages({"no_dut": "x" * (fleet.MAX_MESSAGE_LEN + 1)})


def test_default_messages_name_no_product():
    """The defaults ship in a public package: they point at lager commands, never a host's screens."""
    for text in fleet.MESSAGES.values():
        assert " tab" not in text and "dashboard" not in text.lower()


# --- one net ----------------------------------------------------------------------


def test_net_detail_drops_empty_fields_and_keeps_the_rest(manifest):
    detail = fleet.net_detail(manifest, "reset")
    assert detail["name"] == "reset" and detail["net_type"] == "gpio"
    assert "purpose" not in detail  # "" is dropped
    assert all(c["target"] == "reset" for c in detail["capabilities"])


def test_net_detail_attaches_the_owning_subsystem(manifest):
    detail = fleet.net_detail(manifest, "vin")
    assert detail["dut"] == "demo-board"
    assert detail["subsystem"]["name"] == "power"
    assert detail["dut_schematic_refs"][0]["external_id"] == "doc-1"
    assert all(v is not None for v in detail["dut_schematic_refs"][0].values())


def test_net_detail_unknown_net_is_none(manifest):
    assert fleet.net_detail(manifest, "nope") is None


# --- DUT ----------------------------------------------------------------------------


def test_dut_context_without_slots_warns(manifest):
    manifest["bench"]["dut_slots"] = []
    out = fleet.dut_context(manifest)
    assert out == {"box_id": "box-fixture", "dut_slots": [], "warning": fleet.MESSAGES["no_dut"]}


@pytest.mark.parametrize(
    "calibration, healthy",
    [(None, True), ({}, False), ({"healthy": True}, True), ({"healthy": False}, False)],
)
def test_calibration_healthy_reads_an_empty_object_as_present(manifest, calibration, healthy):
    manifest["bench"]["calibration"] = calibration
    assert fleet.dut_context(manifest)["calibration_healthy"] is healthy


def test_dut_overview_marks_an_inactive_dut_and_lists_documents(manifest):
    text = fleet.dut_overview(manifest)
    assert text.startswith("# DUT Overview\n")
    assert "`doc-1` (document store ID)" in text
    assert "pages: 3-4" in text
    manifest["bench"]["dut_slots"][0]["active"] = False
    assert "## DUT: demo-board  _(inactive)_" in fleet.dut_overview(manifest)


def test_dut_overview_escapes_pipes_in_net_purpose(manifest):
    manifest["bench"]["nets"][0]["purpose"] = "a|b"
    assert "a\\|b" in fleet.dut_overview(manifest)


# --- cite ---------------------------------------------------------------------------


def test_cite_names_subsystem_and_refs(manifest):
    out = fleet.cite(manifest, "vin")
    assert out["dut"] == "demo-board" and out["subsystem"] == "power"
    assert out["refs"] == [{"title": "Main schematic", "kind": "schematic", "externalId": "doc-1", "pages": "3-4"}]
    payload = json.loads(out["raw"])
    assert payload["guidance"] == fleet.MESSAGES["cite_guidance"]
    assert out["raw"] == json.dumps(payload, indent=2, ensure_ascii=False)


def test_cite_unknown_net_and_no_dut(manifest):
    missing = fleet.cite(manifest, "nope")
    assert missing["error"] == "Net 'nope' not found." and missing["refs"] == []
    assert missing["raw"] == '{"box_id":"box-fixture","error":"Net \'nope\' not found."}'
    manifest["bench"]["dut_slots"] = []
    no_dut = fleet.cite(manifest, "uart0")
    assert no_dut["warning"] == fleet.MESSAGES["cite_no_dut"] and no_dut["purpose"] == "console"


# --- reference --------------------------------------------------------------------------


def test_reference_lists_the_bench_types_sorted(manifest):
    out = fleet.reference(manifest)
    assert out["available"] is True
    assert out["keys"] == ["GPIO", "PowerSupply", "UART"]
    assert "## PowerSupply" in out["text"]


def test_reference_absent_differs_from_empty(manifest):
    absent = copy.deepcopy(manifest)
    absent.pop("reference_entries")
    assert fleet.reference(absent) == {
        "available": False,
        "keys": ["GPIO", "PowerSupply", "UART"],
        "text": fleet.MESSAGES["reference_unavailable"],
    }
    manifest["reference_entries"] = {}
    empty = fleet.reference(manifest)
    assert empty["available"] is True
    assert "_No net of a documented type is on this bench._" in empty["text"]


def test_reference_gotchas_and_examples_are_opt_in(manifest):
    plain = fleet.reference(manifest, ["vin"])["text"]
    full = fleet.reference(manifest, ["vin"], include_gotchas=True, include_examples=True)["text"]
    assert "Gotchas:" not in plain and "Example:" not in plain
    assert "Gotchas:" in full and "Example:" in full


# --- suitability -------------------------------------------------------------------------


def test_suitability_runs_the_box_engine_on_the_stored_bindings(manifest):
    out = fleet.suitability(manifest, "uart_loopback")
    assert out["requirement"]["test_type"] == "uart_loopback"
    assert out["report"]["box_id"] == "box-fixture"
    assert "warnings" not in out


def test_suitability_skips_an_unknown_role_and_says_so(manifest):
    manifest["bench"]["capability_bindings"].append({"role": "teleport", "target": "x"})
    baseline = fleet.suitability(json.loads(FIXTURE.read_text(encoding="utf-8")), "uart_loopback")
    out = fleet.suitability(manifest, "uart_loopback")
    assert out["report"] == baseline["report"]
    assert "teleport" in out["warnings"][0]


def test_binding_without_numeric_confidence_counts_as_certain():
    graph, _ = fleet.graph_from_bindings("b", [{"role": "observe", "target": "n", "confidence": True}])
    assert graph.nodes[0].confidence == 1.0


def test_search_reports_each_bench_and_isolates_a_bad_one():
    benches = [
        {"key": "a", "box_id": "a", "capability_bindings": [{"role": "run_local_program", "target": "_box"}]},
        {"key": "b", "box_id": "b", "capability_bindings": "nope"},
        {"box_id": "no-key"},
    ]
    out = fleet.search("check the board responds", benches)
    assert out["requirement"]["test_type"] == "custom"
    assert out["reports"]["a"]["report"]["can_run"] is True
    assert "error" in out["reports"]["b"]
    assert any("no key" in r.get("error", "") for r in out["reports"].values())
