# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

"""Tests for the MCP server's bench state: auto-reload, atomic publication,
and the live instrument inventory.

The MCP server holds the bench and its capability graph in one module-level
object. When a user edits ``bench.json`` / ``saved_nets.json`` (e.g. via
``lager dut edit``), the server should pick the change up on the next
request without an explicit ``box_manage(action="reload")`` or restart. The
bench and graph are published together, so a request that lands during a
reload never sees the new bench with the old graph. Instruments come from a
live USB scan, attached only to a bench loaded from disk, never to one a
test injected.
"""

import os
import threading
import time

import pytest

import lager.mcp.server_state as server_state
from lager.mcp.schemas.bench import BenchDefinition, InstrumentDescriptor
from lager.mcp.schemas.net import NetDescriptor


@pytest.fixture(autouse=True)
def _reset_state():
    """Snapshot and restore module globals so tests don't leak state."""
    saved = (server_state._state, server_state._WATCHED_CONFIG_PATHS)
    yield
    (server_state._state, server_state._WATCHED_CONFIG_PATHS) = saved


@pytest.fixture(autouse=True)
def _no_instrument_scan(monkeypatch):
    """No test here may reach the USB scanner; each opts in to a fake."""
    def _refuse():
        raise AssertionError("instrument scan reached in a unit test")
    monkeypatch.setattr("lager.mcp.engine.instruments.cached_instruments", _refuse)


def _bump_mtime(path: str) -> None:
    """Force a future mtime so the change is detectable within the same second."""
    future = time.time() + 10
    os.utime(path, (future, future))


def _no_instruments(monkeypatch):
    monkeypatch.setattr("lager.mcp.engine.instruments.cached_instruments", lambda: [])


def test_auto_reload_on_file_change(tmp_path, monkeypatch):
    cfg = tmp_path / "bench.json"
    cfg.write_text("v1")
    monkeypatch.setattr(server_state, "_WATCHED_CONFIG_PATHS", (str(cfg),))
    _no_instruments(monkeypatch)

    def fake_load_from_files():
        return BenchDefinition(box_id=cfg.read_text())

    monkeypatch.setattr(
        "lager.mcp.engine.bench_loader.load_from_files", fake_load_from_files
    )

    server_state.init_state()
    assert server_state.get_bench().box_id == "v1"

    cfg.write_text("v2")
    _bump_mtime(str(cfg))

    # Next access should transparently reload.
    assert server_state.get_bench().box_id == "v2"


def test_no_reload_when_unchanged(tmp_path, monkeypatch):
    cfg = tmp_path / "bench.json"
    cfg.write_text("stable")
    monkeypatch.setattr(server_state, "_WATCHED_CONFIG_PATHS", (str(cfg),))
    _no_instruments(monkeypatch)

    calls = {"n": 0}

    def fake_load_from_files():
        calls["n"] += 1
        return BenchDefinition(box_id="stable")

    monkeypatch.setattr(
        "lager.mcp.engine.bench_loader.load_from_files", fake_load_from_files
    )

    server_state.init_state()
    assert calls["n"] == 1
    server_state.get_bench()
    server_state.get_bench()
    # No file change → no extra reload.
    assert calls["n"] == 1


def test_injected_state_never_auto_reloads(tmp_path, monkeypatch):
    cfg = tmp_path / "bench.json"
    cfg.write_text("ondisk")
    monkeypatch.setattr(server_state, "_WATCHED_CONFIG_PATHS", (str(cfg),))

    # Injected bench should disable file watching entirely.
    server_state.init_state(bench=BenchDefinition(box_id="injected"))
    assert server_state._state.file_backed is False

    cfg.write_text("changed")
    _bump_mtime(str(cfg))

    assert server_state.get_bench().box_id == "injected"


def test_a_watched_file_that_appears_later_triggers_a_reload(tmp_path, monkeypatch):
    """A box with no bench.json at start gets one after `lager dut edit`.

    The old code keyed auto-reload on a non-empty mtime snapshot, so a box
    whose config files did not exist at startup never noticed them appear.
    """
    cfg = tmp_path / "bench.json"
    monkeypatch.setattr(server_state, "_WATCHED_CONFIG_PATHS", (str(cfg),))
    _no_instruments(monkeypatch)

    def fake_load_from_files():
        return BenchDefinition(box_id=cfg.read_text() if cfg.exists() else "none")

    monkeypatch.setattr(
        "lager.mcp.engine.bench_loader.load_from_files", fake_load_from_files
    )

    server_state.init_state()
    assert server_state.get_bench().box_id == "none"

    cfg.write_text("authored")
    assert server_state.get_bench().box_id == "authored"


def test_bench_and_graph_are_published_together(tmp_path, monkeypatch):
    """After a reload the graph always describes the bench it was built from."""
    cfg = tmp_path / "saved_nets.json"
    cfg.write_text("one")
    monkeypatch.setattr(server_state, "_WATCHED_CONFIG_PATHS", (str(cfg),))
    _no_instruments(monkeypatch)

    def fake_load_from_files():
        name = cfg.read_text()
        return BenchDefinition(
            box_id=name, nets=[NetDescriptor(name=name, net_type="gpio")],
        )

    monkeypatch.setattr(
        "lager.mcp.engine.bench_loader.load_from_files", fake_load_from_files
    )

    server_state.init_state()
    cfg.write_text("two")
    _bump_mtime(str(cfg))

    bench = server_state.get_bench()
    graph = server_state.get_capability_graph()
    assert bench.box_id == "two"
    assert graph.box_id == "two"
    assert graph.by_target("two")
    assert not graph.by_target("one")


def test_reload_bench_takes_the_reload_lock(monkeypatch):
    """A forced reload and an auto-reload must not rebuild state concurrently."""
    _no_instruments(monkeypatch)
    monkeypatch.setattr(
        "lager.mcp.engine.bench_loader.load_from_files",
        lambda: BenchDefinition(box_id="forced"),
    )
    entered = {"n": 0}

    class RecordingLock:
        def __enter__(self):
            entered["n"] += 1

        def __exit__(self, *exc):
            return False

    monkeypatch.setattr(server_state, "_reload_lock", RecordingLock())

    server_state.reload_bench()
    assert entered["n"] == 1
    assert server_state.get_bench().box_id == "forced"


def test_file_backed_bench_carries_the_live_instruments(monkeypatch):
    """Instruments come from the scan cache, on a copy, never on the shared state."""
    monkeypatch.setattr(
        "lager.mcp.engine.bench_loader.load_from_files",
        lambda: BenchDefinition(box_id="live"),
    )
    scanned = [InstrumentDescriptor(name="Rigol_DP832", instrument_type="Rigol_DP832",
                                    connection="USB0::0x1AB1::0x0E11::X::INSTR")]
    monkeypatch.setattr("lager.mcp.engine.instruments.cached_instruments", lambda: scanned)

    server_state.init_state()
    bench = server_state.get_bench()
    assert [i.name for i in bench.instruments] == ["Rigol_DP832"]
    # The published state is untouched, so a scan result never leaks between reloads.
    assert server_state._state.bench.instruments == []
    # Nets on the copy are the same objects: a shallow copy, not a rebuild.
    assert bench.nets is server_state._state.bench.nets


def test_injected_bench_keeps_its_own_instruments_and_never_scans():
    given = [InstrumentDescriptor(name="lj", instrument_type="labjack_t7", connection="usb")]
    server_state.init_state(bench=BenchDefinition(box_id="t", instruments=given))
    # The autouse fixture makes any scan raise; reaching here proves none ran.
    assert server_state.get_bench().instruments == given


def test_ensure_loaded_loads_from_disk_once(monkeypatch):
    """The :9000 server has no startup hook; the first /bench request loads."""
    _no_instruments(monkeypatch)
    calls = {"n": 0}

    def fake_load_from_files():
        calls["n"] += 1
        return BenchDefinition(box_id="lazy")

    monkeypatch.setattr(
        "lager.mcp.engine.bench_loader.load_from_files", fake_load_from_files
    )
    server_state._state = None

    server_state.ensure_loaded()
    server_state.ensure_loaded()
    assert calls["n"] == 1
    assert server_state.get_bench().box_id == "lazy"


def test_ensure_loaded_leaves_injected_state_alone(monkeypatch):
    server_state.init_state(bench=BenchDefinition(box_id="kept"))
    monkeypatch.setattr(
        "lager.mcp.engine.bench_loader.load_from_files",
        lambda: (_ for _ in ()).throw(AssertionError("must not load")),
    )
    server_state.ensure_loaded()
    assert server_state.get_bench().box_id == "kept"


def test_two_separate_reads_can_straddle_a_reload(tmp_path, monkeypatch):
    """Why get_bench_and_graph exists: two calls are two reads of the state."""
    cfg = tmp_path / "bench.json"
    cfg.write_text("one")
    monkeypatch.setattr(server_state, "_WATCHED_CONFIG_PATHS", (str(cfg),))
    _no_instruments(monkeypatch)

    def fake_load_from_files():
        name = cfg.read_text()
        return BenchDefinition(box_id=name, nets=[NetDescriptor(name=name, net_type="gpio")])

    monkeypatch.setattr(
        "lager.mcp.engine.bench_loader.load_from_files", fake_load_from_files
    )
    server_state.init_state()

    bench = server_state.get_bench()
    cfg.write_text("two")
    _bump_mtime(str(cfg))
    graph = server_state.get_capability_graph()
    assert bench.box_id == "one" and graph.box_id == "two"  # the straddle

    bench, graph = server_state.get_bench_and_graph()
    assert bench.box_id == graph.box_id == "two"


def test_concurrent_readers_during_a_reload_see_a_whole_pair(tmp_path, monkeypatch):
    """Readers racing a reload get either the old pair or the new pair."""
    cfg = tmp_path / "bench.json"
    cfg.write_text("a")
    monkeypatch.setattr(server_state, "_WATCHED_CONFIG_PATHS", (str(cfg),))
    _no_instruments(monkeypatch)

    def fake_load_from_files():
        name = cfg.read_text()
        return BenchDefinition(box_id=name, nets=[NetDescriptor(name=name, net_type="gpio")])

    monkeypatch.setattr(
        "lager.mcp.engine.bench_loader.load_from_files", fake_load_from_files
    )
    server_state.init_state()

    torn = []
    stop = threading.Event()

    def reader():
        while not stop.is_set():
            bench, graph = server_state.get_bench_and_graph()
            if not graph.by_target(bench.box_id):
                torn.append((bench.box_id, graph.box_id))

    threads = [threading.Thread(target=reader) for _ in range(4)]
    for t in threads:
        t.start()
    for name in ("b", "c", "d", "e"):
        cfg.write_text(name)
        _bump_mtime(str(cfg))
        server_state.get_bench()
    stop.set()
    for t in threads:
        t.join()
    assert torn == []
