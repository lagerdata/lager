# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

"""The bench manifest: the versioned document a client stores per box.

Pins the contract a consumer relies on: the hash covers the content and
only the content, building never touches the shared bench, every net names
its API reference, ``metadata_sources`` says which file authored a field,
and the instrument inventory behind it scans at a bounded rate.
"""

import json
import threading

import pytest

from lager.mcp.engine import bench_loader
from lager.mcp.engine.bench_loader import load_from_dicts
from lager.mcp.engine.capability_graph import build_capability_graph
from lager.mcp.engine.instruments import InstrumentCache, instrument_from_record
from lager.mcp.engine.manifest import build_manifest
from lager.mcp.schemas.manifest import MANIFEST_SCHEMA_VERSION, BenchManifest


def _bench(**overrides):
    raw_nets = overrides.pop("raw_nets", [
        {"name": "psu1", "role": "power-supply", "instrument": "Rigol_DP832",
         "channel": "1", "purpose": "DUT 3V3 rail"},
        {"name": "spi0", "role": "spi", "instrument": "Aardvark", "channel": "0",
         "notes": "CS on FIO4", "dut_connection": "J2 pins 1-4"},
        {"name": "arm1", "role": "rotation"},
    ])
    bench_cfg = overrides.pop("bench_cfg", {})
    hello = overrides.pop("hello_data", {"box_id": "BX-7", "hostname": "bx7", "version": "0.50.0"})
    return load_from_dicts(raw_nets=raw_nets, bench_cfg=bench_cfg, hello_data=hello, **overrides)


def _manifest(bench=None, **kw):
    bench = bench or _bench()
    return build_manifest(bench, build_capability_graph(bench), **kw)


class TestManifestShape:
    def test_carries_version_box_id_and_hash(self):
        m = _manifest()
        assert m.schema_version == MANIFEST_SCHEMA_VERSION == 1
        assert m.box_id == "BX-7" == m.bench.box_id
        assert m.generated_at.endswith("Z")
        assert len(m.content_hash) == 64
        assert m.content_hash == m.compute_hash()

    def test_hash_ignores_generated_at(self):
        a = _manifest(generated_at="2026-09-23T10:00:00Z")
        b = _manifest(generated_at="2026-09-24T10:00:00Z")
        assert a.content_hash == b.content_hash

    def test_hash_changes_when_the_bench_changes(self):
        base = _manifest().content_hash
        changed = _manifest(_bench(raw_nets=[
            {"name": "psu1", "role": "power-supply", "purpose": "DUT 5V rail"},
        ]))
        assert changed.content_hash != base

    def test_hash_is_stable_across_a_json_round_trip(self):
        m = _manifest()
        again = BenchManifest(**json.loads(m.model_dump_json()))
        assert again.compute_hash() == m.content_hash

    def test_capability_bindings_come_from_the_graph(self):
        bench = _bench()
        graph = build_capability_graph(bench)
        m = build_manifest(bench, graph)
        assert len(m.bench.capability_bindings) == len(graph.nodes)
        roles_for_psu = {b["role"] for b in m.bench.capability_bindings if b["target"] == "psu1"}
        assert "source_power" in roles_for_psu
        # JSON mode: enum members serialise as their values, not repr strings.
        assert all(isinstance(b["role"], str) for b in m.bench.capability_bindings)

    def test_reference_keys_name_the_api_reference_entry_per_net(self):
        m = _manifest()
        assert m.reference_keys == {"psu1": "PowerSupply", "spi0": "SPI"}
        # rotation has no API reference, so arm1 is absent rather than null.

    def test_building_never_mutates_the_shared_bench(self):
        bench = _bench()
        _manifest(bench)
        assert bench.capability_bindings == []

    def test_new_metadata_fields_travel(self):
        m = _manifest()
        spi = next(n for n in m.bench.nets if n.name == "spi0")
        assert spi.dut_connection == "J2 pins 1-4"
        assert spi.test_hints == []


class TestMetadataSources:
    def test_names_the_file_that_authored_each_field(self):
        bench = _bench(bench_cfg={"net_overrides": [
            {"name": "psu1", "purpose": "override wins", "tags": ["rail"]},
        ]})
        assert bench.metadata_sources["psu1"] == {"purpose": "bench.json", "tags": "bench.json"}
        assert bench.metadata_sources["spi0"] == {"notes": "saved_net", "dut_connection": "saved_net"}
        assert "arm1" not in bench.metadata_sources
        psu = next(n for n in bench.nets if n.name == "psu1")
        assert psu.purpose == "override wins"

    def test_a_saved_value_shadowed_by_an_override_reads_as_bench_json(self):
        bench = _bench(bench_cfg={"net_overrides": [{"name": "spi0", "notes": "from bench.json"}]})
        assert bench.metadata_sources["spi0"]["notes"] == "bench.json"

    def test_override_sets_dut_connection_and_test_hints(self):
        bench = _bench(bench_cfg={"net_overrides": [
            {"name": "psu1", "dut_connection": "J1", "test_hints": ["ramp slowly", 7]},
        ]})
        psu = next(n for n in bench.nets if n.name == "psu1")
        assert psu.dut_connection == "J1"
        assert psu.test_hints == ["ramp slowly", "7"]
        assert bench.metadata_sources["psu1"]["test_hints"] == "bench.json"

    def test_a_malformed_override_field_is_not_credited(self):
        bench = _bench(bench_cfg={"net_overrides": [
            {"name": "psu1", "voltage_domain": {"max_v": "not a number"}},
        ]})
        assert "voltage_domain" not in bench.metadata_sources.get("psu1", {})

    def test_the_handler_and_the_loader_agree_on_the_user_fields(self):
        assert set(bench_loader.USER_METADATA_FIELDS) <= set(bench_loader.OVERRIDABLE_NET_FIELDS)
        assert set(bench_loader.USER_METADATA_FIELDS) == {
            "purpose", "notes", "tags", "dut_connection", "test_hints",
        }


class TestBoxIdFallback:
    """A box with no id file is still a box with a name.

    Found on the bench: a long-running box had no ``/etc/lager/box_id`` and
    no ``LAGER_BOX_ID``, so its manifest and every tool reply carried an
    empty id while its hostname was right there.
    """

    def test_loader_falls_back_to_env_then_hostname(self, tmp_path, monkeypatch):
        from lager.mcp.engine.bench_loader import load_from_files

        (tmp_path / "hostname").write_text("PRD-9\n")
        kw = dict(
            saved_nets_path="/nonexistent", bench_json_path="/nonexistent",
            box_id_path=str(tmp_path / "box_id"), version_path="/nonexistent",
            hostname_path=str(tmp_path / "hostname"),
        )
        monkeypatch.delenv("LAGER_BOX_ID", raising=False)
        assert load_from_files(**kw).box_id == "PRD-9"
        monkeypatch.setenv("LAGER_BOX_ID", "from-env")
        assert load_from_files(**kw).box_id == "from-env"
        (tmp_path / "box_id").write_text("from-file")
        assert load_from_files(**kw).box_id == "from-file"

    def test_loader_with_nothing_at_all_stays_empty(self, monkeypatch):
        from lager.mcp.engine.bench_loader import load_from_files

        monkeypatch.delenv("LAGER_BOX_ID", raising=False)
        bench = load_from_files(
            saved_nets_path="/nonexistent", bench_json_path="/nonexistent",
            box_id_path="/nonexistent", version_path="/nonexistent",
            hostname_path="/nonexistent",
        )
        assert bench.box_id == ""

    def test_config_get_box_id_uses_the_same_chain(self, tmp_path, monkeypatch):
        from lager.mcp.config import get_box_id

        monkeypatch.delenv("LAGER_BOX_ID", raising=False)
        kw = dict(box_id_path=str(tmp_path / "box_id"), hostname_path=str(tmp_path / "hostname"))
        assert get_box_id(**kw) == "unknown"
        (tmp_path / "hostname").write_text("PRD-9\n")
        assert get_box_id(**kw) == "PRD-9"
        monkeypatch.setenv("LAGER_BOX_ID", " from-env ")
        assert get_box_id(**kw) == "from-env"
        (tmp_path / "box_id").write_text("from-file\n")
        assert get_box_id(**kw) == "from-file"

    def test_manifest_and_tool_replies_agree_on_the_box(self, tmp_path, monkeypatch):
        """The manifest, discover_bench and a control-tier reply name the same box."""
        from lager.mcp import config
        from lager.mcp.engine.bench_loader import load_from_files
        from lager.mcp.tools._payload import box_id as payload_box_id

        (tmp_path / "hostname").write_text("PRD-9")
        monkeypatch.delenv("LAGER_BOX_ID", raising=False)
        monkeypatch.setattr(config, "BOX_ID_PATH", str(tmp_path / "box_id"))
        monkeypatch.setattr(config, "HOST_HOSTNAME_PATH", str(tmp_path / "hostname"))
        monkeypatch.setattr(config.get_box_id, "__kwdefaults__", {
            "box_id_path": str(tmp_path / "box_id"), "hostname_path": str(tmp_path / "hostname"),
        })
        bench = load_from_files(
            saved_nets_path="/nonexistent", bench_json_path="/nonexistent",
            box_id_path=str(tmp_path / "box_id"), version_path="/nonexistent",
            hostname_path=str(tmp_path / "hostname"),
        )
        assert bench.box_id == payload_box_id() == "PRD-9"


class TestNoRemoteLoader:
    def test_the_http_loader_is_gone(self):
        """``load_from_box`` fetched routes the box never served; nothing called it."""
        assert not hasattr(bench_loader, "load_from_box")
        assert not hasattr(bench_loader, "requests")


class TestInstrumentRecords:
    def test_scanner_shape_flattens_channels_by_role(self):
        inst = instrument_from_record({
            "name": "FTDI_FT232H", "address": "USB0::0x0403::0x6014::AB12::INSTR",
            "serial": "AB12", "net_type": ["uart", "gpio"],
            "channels": {"uart": ["/dev/ttyUSB0"], "gpio": ["0", "1"]},
            "tty_path": "/dev/ttyUSB0",
        })
        assert inst.name == inst.instrument_type == "FTDI_FT232H"
        assert inst.connection.startswith("USB0::")
        assert inst.channels == ["uart:/dev/ttyUSB0", "gpio:0", "gpio:1"]
        assert inst.capabilities == ["uart", "gpio"]
        assert inst.metadata["channels_by_role"] == {"uart": ["/dev/ttyUSB0"], "gpio": ["0", "1"]}
        assert inst.metadata["serial"] == "AB12"
        assert inst.metadata["tty_path"] == "/dev/ttyUSB0"
        assert "custom" not in inst.metadata

    def test_legacy_list_shape_and_nulls(self):
        inst = instrument_from_record({"name": "scope1", "type": "rigol_mso5204", "channels": None})
        assert inst.instrument_type == "rigol_mso5204"
        assert inst.channels == []
        flat = instrument_from_record({"name": "lj", "channels": ["AIN0", "AIN1"]})
        assert flat.channels == ["AIN0", "AIN1"]

    def test_custom_device_is_marked(self):
        inst = instrument_from_record({"name": "MyBoard", "custom": True, "address": "x"})
        assert inst.metadata == {"custom": True}


class TestInstrumentCache:
    def test_scans_once_per_ttl(self):
        clock = {"t": 0.0}
        calls = {"n": 0}

        def scan():
            calls["n"] += 1
            return [{"name": "A"}]

        cache = InstrumentCache(scan, ttl_s=60, clock=lambda: clock["t"])
        assert [r["name"] for r in cache.get()] == ["A"]
        cache.get()
        clock["t"] = 59.9
        cache.get()
        assert calls["n"] == 1
        clock["t"] = 60.0
        cache.get()
        assert calls["n"] == 2

    def test_a_failed_scan_reports_nothing_and_is_not_retried_until_the_ttl(self):
        clock = {"t": 0.0}
        calls = {"n": 0}

        def scan():
            calls["n"] += 1
            raise RuntimeError("no usb")

        cache = InstrumentCache(scan, ttl_s=30, clock=lambda: clock["t"])
        assert cache.get() == []
        assert cache.last_error == "no usb"
        cache.get()
        assert calls["n"] == 1
        clock["t"] = 30
        cache.get()
        assert calls["n"] == 2

    def test_force_and_invalidate_scan_again(self):
        calls = {"n": 0}

        def scan():
            calls["n"] += 1
            return []

        cache = InstrumentCache(scan, ttl_s=1000)
        cache.get()
        cache.get(force=True)
        cache.invalidate()
        cache.get()
        assert calls["n"] == 3

    def test_non_dict_and_non_list_results_are_dropped(self):
        assert InstrumentCache(lambda: [{"name": "ok"}, "junk", None]).get() == [{"name": "ok"}]
        assert InstrumentCache(lambda: {"name": "not a list"}).get() == []

    def test_callers_return_copies(self):
        cache = InstrumentCache(lambda: [{"name": "A"}], ttl_s=1000)
        cache.get().append({"name": "B"})
        assert cache.get() == [{"name": "A"}]

    def test_concurrent_callers_share_one_scan(self):
        started = threading.Event()
        release = threading.Event()
        calls = {"n": 0}

        def slow_scan():
            calls["n"] += 1
            started.set()
            release.wait(5)
            return [{"name": "shared"}]

        cache = InstrumentCache(slow_scan, ttl_s=1000)
        results = []
        workers = [threading.Thread(target=lambda: results.append(cache.get())) for _ in range(3)]
        for w in workers:
            w.start()
        assert started.wait(5)
        release.set()
        for w in workers:
            w.join(5)
        assert calls["n"] == 1
        assert results == [[{"name": "shared"}]] * 3


class TestGetTestExampleWithoutScripts:
    def test_examples_carry_snippets_and_a_repo_pointer_not_file_content(self, monkeypatch):
        """The example scripts are not in the box image; the tool must not
        depend on them, and must say where the full example lives."""
        from lager.mcp import server_state
        from lager.mcp.tools.authoring import get_test_example

        monkeypatch.setattr(server_state, "get_bench", lambda: _bench())
        payload = json.loads(get_test_example("spi"))
        assert payload["box_id"] == "BX-7"
        first = payload["examples"][0]
        assert first["repo_script"].startswith("https://github.com/lagerdata/lager/tree/main/test/api/")
        assert "SPI" in first["example_snippets"]
        assert "Net.get(" in first["example_snippets"]["SPI"]
        assert "script_content" not in first

    def test_no_match_lists_the_patterns(self, monkeypatch):
        from lager.mcp import server_state
        from lager.mcp.tools.authoring import get_test_example

        monkeypatch.setattr(server_state, "get_bench", lambda: _bench())
        payload = json.loads(get_test_example("zzz-nothing"))
        assert payload["box_id"] == "BX-7"
        assert payload["available_patterns"]

    def test_the_file_reader_is_gone(self):
        from lager.mcp.data import test_patterns

        assert not hasattr(test_patterns, "get_script_content")
        assert not hasattr(test_patterns, "_TEST_DIR")
