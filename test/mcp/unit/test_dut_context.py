# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for the DUT context feature.

Covers:
- New schema types (DocRef, SubSystem, DUTContext).
- Net metadata (purpose/notes/tags) loading in the bench loader.
- DUT-context parsing from bench.json (both ``dut_slots`` and the
  single-DUT ``dut_context`` short-form).
- ``discover_dut`` / ``cite_schematic`` tools.
- Enrichment of ``discover_bench(net_name)`` with subsystem + doc refs.
- DUT context threading into ``plan_firmware_test``.
- ``lager://dut/context`` and ``lager://dut/overview.md`` resources.
"""

import json

import pytest

from lager.mcp.schemas.bench import BenchDefinition, DocRef, DUTContext, SubSystem
from lager.mcp.schemas.net import NetDescriptor
from lager.mcp.engine.bench_loader import (
    _dut_context_from_raw,
    _net_from_raw,
    load_from_dicts,
)


# ---------------------------------------------------------------------------
# Schema basics
# ---------------------------------------------------------------------------

class TestDocRef:
    def test_minimal(self):
        d = DocRef(title="Main schematic")
        assert d.title == "Main schematic"
        assert d.kind == "other"
        assert d.url is None
        assert d.repo_path is None

    def test_full(self):
        d = DocRef(
            title="Power tree",
            kind="schematic",
            repo_path="docs/sch.pdf",
            url="https://example.com/sch.pdf",
            pages="3-5",
            notes="See sheet POWER",
        )
        assert d.kind == "schematic"
        assert d.url and d.repo_path  # both allowed


class TestSubSystem:
    def test_defaults(self):
        s = SubSystem(name="Flash subsystem")
        assert s.name == "Flash subsystem"
        assert s.nets == []
        assert s.doc_refs == []

    def test_with_nets_and_refs(self):
        s = SubSystem(
            name="Power tree",
            summary="PMIC + LDOs",
            nets=["psu1", "psu2"],
            doc_refs=[DocRef(title="Power sheet", kind="schematic", pages="2")],
        )
        assert s.nets == ["psu1", "psu2"]
        assert s.doc_refs[0].pages == "2"


class TestDUTContext:
    def test_defaults(self):
        d = DUTContext(name="main")
        assert d.name == "main"
        assert d.active is True
        assert d.purpose == ""
        assert d.subsystems == []

    def test_all_doc_refs_combines(self):
        d = DUTContext(
            name="main",
            schematic_refs=[DocRef(title="A")],
            datasheet_refs=[DocRef(title="B")],
            firmware_refs=[DocRef(title="C")],
            extra_docs=[DocRef(title="D")],
            subsystems=[
                SubSystem(name="X", doc_refs=[DocRef(title="E")]),
            ],
        )
        titles = [r.title for r in d.all_doc_refs()]
        assert titles == ["A", "B", "C", "D", "E"]

    def test_subsystem_for_net(self):
        d = DUTContext(
            name="main",
            subsystems=[
                SubSystem(name="Flash", nets=["flash_cs", "flash_clk"]),
                SubSystem(name="Power", nets=["psu1"]),
            ],
        )
        assert d.subsystem_for_net("flash_cs").name == "Flash"
        assert d.subsystem_for_net("psu1").name == "Power"
        assert d.subsystem_for_net("nope") is None


class TestBenchDUTSlots:
    def test_dut_slots_accept_dut_context(self):
        bench = BenchDefinition(dut_slots=[DUTContext(name="main", purpose="x")])
        assert bench.dut_slots[0].purpose == "x"

    def test_primary_dut_prefers_active(self):
        bench = BenchDefinition(dut_slots=[
            DUTContext(name="a", active=False, purpose="inactive"),
            DUTContext(name="b", active=True, purpose="active"),
        ])
        assert bench.primary_dut().name == "b"

    def test_primary_dut_falls_back_to_first(self):
        bench = BenchDefinition(dut_slots=[
            DUTContext(name="only", active=False),
        ])
        assert bench.primary_dut().name == "only"

    def test_primary_dut_none_when_empty(self):
        assert BenchDefinition().primary_dut() is None


# ---------------------------------------------------------------------------
# Net metadata loading
# ---------------------------------------------------------------------------

class TestNetFromRaw:
    def test_purpose_and_notes_pass_through(self):
        nd = _net_from_raw({
            "name": "spi1", "role": "spi",
            "purpose": "flash bus",
            "notes": "idle high",
        })
        assert nd.purpose == "flash bus"
        assert nd.notes == "idle high"

    def test_legacy_fields_are_ignored(self):
        """Old description/dut_connection/test_hints keys no longer exist."""
        nd = _net_from_raw({
            "name": "uart1", "role": "uart",
            "description": "DUT debug CLI",
            "dut_connection": "PA9/PA10",
            "test_hints": ["boot banner"],
        })
        assert nd.purpose == ""
        assert nd.notes == ""
        assert not hasattr(nd, "description")
        assert not hasattr(nd, "dut_connection")
        assert not hasattr(nd, "test_hints")

    def test_tags_pass_through(self):
        nd = _net_from_raw({
            "name": "psu1", "role": "power-supply",
            "tags": ["power", "rail"],
        })
        assert nd.tags == ["power", "rail"]


class TestNetOverrides:
    def test_override_sets_purpose_and_notes(self):
        bench = load_from_dicts(
            raw_nets=[{"name": "psu1", "role": "power-supply"}],
            bench_cfg={
                "net_overrides": [
                    {"name": "psu1", "purpose": "canonical", "notes": "n"},
                ],
            },
        )
        assert bench.nets[0].purpose == "canonical"
        assert bench.nets[0].notes == "n"


# ---------------------------------------------------------------------------
# DUT context parsing from bench.json
# ---------------------------------------------------------------------------

class TestDUTContextFromRaw:
    def test_minimal_legacy_shape(self):
        d = _dut_context_from_raw({"name": "slot0", "active": True, "board_profile": "nrf52840"})
        assert d.name == "slot0"
        assert d.board_profile == "nrf52840"
        assert d.purpose == ""

    def test_rich_shape(self):
        d = _dut_context_from_raw({
            "name": "main",
            "purpose": "Power regression",
            "summary": "STM32 box",
            "mcu": "STM32H7",
            "key_peripherals": ["QSPI flash"],
            "schematic_refs": [{"title": "Main", "kind": "schematic", "repo_path": "x.pdf"}],
            "subsystems": [
                {"name": "Flash", "nets": ["flash_cs"],
                 "doc_refs": [{"title": "Sheet", "kind": "schematic", "pages": "3"}]},
            ],
        })
        assert d.purpose == "Power regression"
        assert d.mcu == "STM32H7"
        assert d.schematic_refs[0].title == "Main"
        assert d.subsystems[0].nets == ["flash_cs"]
        assert d.subsystems[0].doc_refs[0].pages == "3"

    def test_skips_doc_refs_without_title(self):
        d = _dut_context_from_raw({
            "name": "main",
            "schematic_refs": [{"kind": "schematic"}],  # missing title
        })
        assert d.schematic_refs == []

    def test_missing_name_raises(self):
        with pytest.raises(ValueError):
            _dut_context_from_raw({"purpose": "no name"})


class TestLoaderDUTContextBlock:
    def test_dut_context_shortform_promotes_to_dut_slots(self):
        bench = load_from_dicts(
            bench_cfg={
                "dut_context": {"name": "only", "purpose": "single DUT", "mcu": "STM32"},
            },
        )
        assert len(bench.dut_slots) == 1
        assert bench.dut_slots[0].purpose == "single DUT"

    def test_dut_slots_list_form(self):
        bench = load_from_dicts(
            bench_cfg={
                "dut_slots": [
                    {"name": "main", "purpose": "primary"},
                    {"name": "spare", "active": False},
                ],
            },
        )
        assert [d.name for d in bench.dut_slots] == ["main", "spare"]
        assert bench.dut_slots[1].active is False

    def test_dut_slots_skips_malformed(self):
        bench = load_from_dicts(
            bench_cfg={
                "dut_slots": [
                    {"name": "good"},
                    {"no_name": "bad"},  # missing name; should be skipped
                    "not a dict",         # also skipped
                ],
            },
        )
        assert [d.name for d in bench.dut_slots] == ["good"]


class TestDanglingNetReferences:
    def test_warns_on_unknown_subsystem_net(self, caplog):
        import logging
        with caplog.at_level(logging.WARNING, logger="lager.mcp.engine.bench_loader"):
            load_from_dicts(
                raw_nets=[{"name": "flash_cs", "role": "gpio"}],
                bench_cfg={
                    "dut_context": {
                        "name": "main",
                        "subsystems": [
                            {"name": "Flash", "nets": ["flash_cs", "typo_net"]},
                        ],
                    },
                },
            )
        assert any(
            "typo_net" in r.message and "unknown net" in r.message
            for r in caplog.records
        )

    def test_no_warning_when_all_nets_resolve(self, caplog):
        import logging
        with caplog.at_level(logging.WARNING, logger="lager.mcp.engine.bench_loader"):
            load_from_dicts(
                raw_nets=[{"name": "flash_cs", "role": "gpio"}],
                bench_cfg={
                    "dut_context": {
                        "name": "main",
                        "subsystems": [{"name": "Flash", "nets": ["flash_cs"]}],
                    },
                },
            )
        assert not any("unknown net" in r.message for r in caplog.records)


# ---------------------------------------------------------------------------
# Resources
# ---------------------------------------------------------------------------

@pytest.fixture
def populated_bench():
    return load_from_dicts(
        raw_nets=[
            {"name": "uart1", "role": "uart",
             "purpose": "DUT debug CLI", "notes": "PA9/PA10"},
            {"name": "flash_cs", "role": "gpio",
             "purpose": "SPI flash chip-select"},
            {"name": "psu1", "role": "power-supply",
             "purpose": "main 3V3 rail"},
        ],
        bench_cfg={
            "box_id": "HW-7",
            "hostname": "hw7",
            "dut_context": {
                "name": "main",
                "purpose": "Power-regression for FeatureA boards",
                "mcu": "STM32H7",
                "key_peripherals": ["QSPI flash"],
                "summary": "STM32H7 DUT under fault injection.",
                "schematic_refs": [{
                    "title": "Main schematic", "kind": "schematic",
                    "repo_path": "docs/sch.pdf",
                }],
                "datasheet_refs": [{
                    "title": "STM32H7 RM", "kind": "datasheet",
                    "url": "https://example.com/rm.pdf", "pages": "150-200",
                }],
                "subsystems": [
                    {"name": "Flash subsystem", "summary": "QSPI",
                     "nets": ["flash_cs"],
                     "doc_refs": [{"title": "Flash sheet", "kind": "schematic",
                                   "repo_path": "docs/sch.pdf", "pages": "3"}]},
                    {"name": "Power tree", "summary": "PMIC",
                     "nets": ["psu1"]},
                ],
            },
        },
    )


class TestDUTResource:
    def test_dut_context_resource_json(self, populated_bench, monkeypatch):
        import lager.mcp.resources.dut as dut_resource
        monkeypatch.setattr(dut_resource, "get_bench", lambda: populated_bench)

        captured = {}

        class _MCP:
            def resource(self, uri):
                def deco(fn):
                    captured[uri] = fn
                    return fn
                return deco

        dut_resource.register(_MCP())
        body = captured["lager://dut/context"]()
        payload = json.loads(body)
        assert payload["box_id"] == "HW-7"
        assert payload["dut_slots"][0]["purpose"].startswith("Power-regression")
        assert payload["dut_slots"][0]["subsystems"][0]["name"] == "Flash subsystem"

    def test_dut_overview_md(self, populated_bench, monkeypatch):
        import lager.mcp.resources.dut as dut_resource
        monkeypatch.setattr(dut_resource, "get_bench", lambda: populated_bench)
        captured = {}

        class _MCP:
            def resource(self, uri):
                def deco(fn):
                    captured[uri] = fn
                    return fn
                return deco

        dut_resource.register(_MCP())
        md = captured["lager://dut/overview.md"]()
        assert "# DUT Overview" in md
        assert "Power-regression for FeatureA" in md
        assert "STM32H7" in md
        assert "Flash subsystem" in md
        assert "flash_cs" in md
        # The orphan section should pick up uart1 which is not in any subsystem
        assert "uart1" in md
        # Doc refs rendered
        assert "Main schematic" in md
        assert "STM32H7 RM" in md

    def test_dut_overview_empty_bench(self, monkeypatch):
        import lager.mcp.resources.dut as dut_resource
        monkeypatch.setattr(dut_resource, "get_bench", lambda: BenchDefinition())
        md = dut_resource._render_overview(BenchDefinition())
        assert "No DUT context has been authored" in md


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------

class TestDiscoverDUTTool:
    def test_returns_warning_when_empty(self, monkeypatch):
        import lager.mcp.tools.dut as dut_tools
        monkeypatch.setattr(dut_tools, "get_bench", lambda: BenchDefinition())
        body = dut_tools.discover_dut.fn() if hasattr(dut_tools.discover_dut, "fn") else dut_tools.discover_dut()
        payload = json.loads(body)
        assert payload["dut_slots"] == []
        assert "No DUT context" in payload["warning"]

    def test_populated(self, populated_bench, monkeypatch):
        import lager.mcp.tools.dut as dut_tools
        monkeypatch.setattr(dut_tools, "get_bench", lambda: populated_bench)
        body = dut_tools.discover_dut.fn() if hasattr(dut_tools.discover_dut, "fn") else dut_tools.discover_dut()
        payload = json.loads(body)
        assert payload["box_id"] == "HW-7"
        slot = payload["dut_slots"][0]
        assert slot["mcu"] == "STM32H7"
        assert slot["schematic_refs"][0]["repo_path"] == "docs/sch.pdf"
        assert slot["subsystems"][0]["name"] == "Flash subsystem"


class TestCiteSchematicTool:
    def _call(self, name):
        from lager.mcp.tools.dut import cite_schematic
        fn = cite_schematic.fn if hasattr(cite_schematic, "fn") else cite_schematic
        return json.loads(fn(name))

    def test_missing_net(self, monkeypatch, populated_bench):
        import lager.mcp.tools.dut as dut_tools
        monkeypatch.setattr(dut_tools, "get_bench", lambda: populated_bench)
        out = self._call("nope")
        assert "error" in out

    def test_net_in_subsystem(self, monkeypatch, populated_bench):
        import lager.mcp.tools.dut as dut_tools
        monkeypatch.setattr(dut_tools, "get_bench", lambda: populated_bench)
        out = self._call("flash_cs")
        assert out["subsystem"] == "Flash subsystem"
        assert out["subsystem_doc_refs"][0]["pages"] == "3"
        assert out["schematic_refs"][0]["title"] == "Main schematic"

    def test_net_without_subsystem(self, monkeypatch, populated_bench):
        import lager.mcp.tools.dut as dut_tools
        monkeypatch.setattr(dut_tools, "get_bench", lambda: populated_bench)
        out = self._call("uart1")
        assert out["subsystem"] is None
        assert out["schematic_refs"][0]["title"] == "Main schematic"

    def test_no_dut_authored(self, monkeypatch):
        import lager.mcp.tools.dut as dut_tools
        bench = load_from_dicts(
            raw_nets=[{"name": "x", "role": "gpio"}],
        )
        monkeypatch.setattr(dut_tools, "get_bench", lambda: bench)
        out = self._call("x")
        assert "No DUT context" in out["warning"]


class TestDiscoverBenchEnrichment:
    def test_net_inspection_includes_subsystem(self, monkeypatch, populated_bench):
        import lager.mcp.tools.discover as discover_tool
        from lager.mcp.engine.capability_graph import build_capability_graph

        graph = build_capability_graph(populated_bench)
        monkeypatch.setattr(discover_tool, "get_bench", lambda: populated_bench)
        monkeypatch.setattr(discover_tool, "get_capability_graph", lambda: graph)

        fn = discover_tool.discover_bench.fn if hasattr(discover_tool.discover_bench, "fn") else discover_tool.discover_bench
        body = fn("flash_cs")
        payload = json.loads(body)
        assert payload["dut"] == "main"
        assert payload["subsystem"]["name"] == "Flash subsystem"
        assert payload["subsystem"]["doc_refs"][0]["pages"] == "3"
        assert payload["dut_schematic_refs"][0]["title"] == "Main schematic"

    def test_summary_includes_dut_purpose(self, monkeypatch, populated_bench):
        import lager.mcp.tools.discover as discover_tool
        from lager.mcp.engine.capability_graph import build_capability_graph

        graph = build_capability_graph(populated_bench)
        monkeypatch.setattr(discover_tool, "get_bench", lambda: populated_bench)
        monkeypatch.setattr(discover_tool, "get_capability_graph", lambda: graph)

        fn = discover_tool.discover_bench.fn if hasattr(discover_tool.discover_bench, "fn") else discover_tool.discover_bench
        body = fn()
        payload = json.loads(body)
        slot = payload["dut_slots"][0]
        assert slot["purpose"].startswith("Power-regression")
        assert slot["mcu"] == "STM32H7"
        # Per-net entries should show ``purpose``.
        names = {n["name"]: n for n in payload["nets"]}
        assert names["uart1"]["purpose"] == "DUT debug CLI"
        assert names["flash_cs"]["purpose"] == "SPI flash chip-select"


class TestPlanFirmwareTestThreading:
    def test_includes_dut_block_and_doc_refs(self, monkeypatch, populated_bench):
        import lager.mcp.tools.authoring as authoring
        monkeypatch.setattr(authoring, "get_bench", lambda: populated_bench, raising=False)
        # plan_firmware_test imports get_bench inside the function -- patch the
        # server_state version it actually imports.
        monkeypatch.setattr("lager.mcp.server_state.get_bench", lambda: populated_bench)

        fn = authoring.plan_firmware_test.fn if hasattr(authoring.plan_firmware_test, "fn") else authoring.plan_firmware_test
        body = fn("flash driver firmware", "exercise QSPI flash and the UART CLI")
        payload = json.loads(body)
        # DUT context threaded through
        assert payload["dut"]["name"] == "main"
        assert payload["dut"]["mcu"] == "STM32H7"
        assert payload["dut"]["schematic_refs"][0]["repo_path"] == "docs/sch.pdf"
        assert payload["dut_overview_resource"] == "lager://dut/overview.md"
        # The flash net step should carry the subsystem + doc refs.
        for phase in payload["phases"]:
            for step in phase["nets"]:
                if step["net"] == "flash_cs":
                    assert step["subsystem"] == "Flash subsystem"
                    assert any(d["pages"] == "3" for d in step.get("doc_refs", []))
                    assert step["purpose"] == "SPI flash chip-select"
                    return
        pytest.fail("flash_cs step not found in plan")


# ---------------------------------------------------------------------------
# bench_identity resource enrichment
# ---------------------------------------------------------------------------

class TestBenchIdentityResource:
    def test_identity_surfaces_purpose(self, populated_bench, monkeypatch):
        import lager.mcp.resources.bench_identity as bench_identity_resource
        monkeypatch.setattr(bench_identity_resource, "get_bench", lambda: populated_bench)
        captured = {}

        class _MCP:
            def resource(self, uri):
                def deco(fn):
                    captured[uri] = fn
                    return fn
                return deco

        bench_identity_resource.register(_MCP())
        payload = json.loads(captured["lager://bench/identity"]())
        slot = payload["dut_slots"][0]
        assert slot["purpose"].startswith("Power-regression")
        assert slot["mcu"] == "STM32H7"
        assert payload["more"]["dut_overview"] == "lager://dut/overview.md"
