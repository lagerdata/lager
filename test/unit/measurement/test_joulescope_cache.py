# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

"""
Unit tests for JS220 close-vs-instance-cache coherence.

Regression tests for the warm /net/command path bug: the first watt read
succeeded, then every later read failed with "Joulescope '<net>' is not
connected" until the box runtime restarted. _watt_meter() closes the net
after every read (to release USB between reads), but close() left the
per-serial singleton cached with _initialized=True, so every subsequent
construction returned the dead instance and never reopened the device.
One net's close also poisoned the other net sharing the same physical
JS220 (watt1/energy1).

Run with:
    python -m pytest test/unit/measurement/test_joulescope_cache.py -v
"""

import threading
import types

import numpy as np
import pytest

import lager.measurement.watt.joulescope_js220 as js_mod
from lager.measurement.watt.joulescope_js220 import JoulescopeJS220
from lager.measurement.energy_analyzer.joulescope_energy import JoulescopeEnergyAnalyzer
from lager.exceptions import WattBackendError


# ---------------------------------------------------------------------------
# Fake joulescope library
# ---------------------------------------------------------------------------

class FakeJoulescopeDevice:
    """Mirrors the joulescope v1 Device: `serial_number` attribute,
    open()/close()/read() with 2 mA @ 3.3 V, and a bare object repr
    (the v1 Device defines __str__ but not __repr__)."""

    def __init__(self, serial):
        self.serial_number = serial
        self.is_open = False

    def open(self):
        self.is_open = True

    def close(self):
        self.is_open = False

    def read(self, contiguous_duration=None):
        if not self.is_open:
            raise RuntimeError("device not open")
        # (N, 2) array of [current, voltage] samples
        return np.tile(np.array([0.002, 3.3]), (8, 1))


def make_fake_joulescope(serials=("SNA",)):
    """Build a fake `joulescope` module exposing scan()/scan_require_one().

    Like the real v1 API, scan() creates fresh Device instances on every
    call and there is no top-level Device class.
    """
    created = []

    def scan(name=None, config=None):
        devs = [FakeJoulescopeDevice(s) for s in serials]
        created.extend(devs)
        return devs

    def scan_require_one(name=None, config=None):
        dev = FakeJoulescopeDevice(serials[0])
        created.append(dev)
        return dev

    fake = types.SimpleNamespace(scan=scan, scan_require_one=scan_require_one)
    return fake, created


@pytest.fixture
def fake_joulescope(monkeypatch):
    """Patch the joulescope lib and give each test clean instance caches."""
    fake, created = make_fake_joulescope(serials=("SNA", "SNB"))
    monkeypatch.setattr(js_mod, "joulescope", fake)
    JoulescopeJS220.clear_cache()
    JoulescopeEnergyAnalyzer.clear_cache()
    yield fake, created
    # Force-clear so a failed test can't leak instances into the next one.
    with JoulescopeJS220._instance_lock:
        JoulescopeJS220._instances.clear()
    with JoulescopeEnergyAnalyzer._instance_lock:
        JoulescopeEnergyAnalyzer._instances.clear()


# ---------------------------------------------------------------------------
# Section 1: close() evicts, next construction reopens
# ---------------------------------------------------------------------------

class TestCloseReopens:

    def test_read_close_reconstruct_read(self, fake_joulescope):
        # The exact Workbench sequence: read, close, then read again via a
        # fresh Net.get-style construction. The second read must succeed.
        watt = JoulescopeJS220("watt1", 0, "JS220:SNA")
        dev1 = watt._device
        assert watt.read(0.01) == pytest.approx(0.0066)
        watt.close()

        watt2 = JoulescopeJS220("watt1", 0, "JS220:SNA")
        assert watt2 is not watt
        assert watt2.read(0.01) == pytest.approx(0.0066)
        # A second physical open happened
        assert watt2._device is not dev1
        assert watt2._device.is_open and not dev1.is_open

    def test_close_evicts_cached_instance(self, fake_joulescope):
        watt = JoulescopeJS220("watt1", 0, "JS220:SNA")
        assert JoulescopeJS220._instances  # cached while open

        watt.close()

        assert JoulescopeJS220._instances == {}
        assert watt._initialized is False
        assert watt._device is None

    def test_stale_reference_still_errors_but_does_not_evict_replacement(
        self, fake_joulescope
    ):
        watt = JoulescopeJS220("watt1", 0, "JS220:SNA")
        watt.close()
        replacement = JoulescopeJS220("watt1", 0, "JS220:SNA")

        # Reads on the stale closed handle keep the old clear error
        with pytest.raises(WattBackendError, match="is not connected"):
            watt.read(0.01)

        # Closing the stale handle again must not evict the live replacement
        watt.close()
        assert JoulescopeJS220._instances.get("SNA") is replacement
        assert replacement.read(0.01) == pytest.approx(0.0066)

    def test_is_connection_alive(self, fake_joulescope):
        watt = JoulescopeJS220("watt1", 0, "JS220:SNA")
        assert watt._is_connection_alive() is True
        watt.close()
        assert watt._is_connection_alive() is False


# ---------------------------------------------------------------------------
# Section 2: clear_cache() must not deadlock now that close() takes the lock
# ---------------------------------------------------------------------------

class TestClearCache:

    def _run_with_timeout(self, fn, timeout=5.0):
        worker = threading.Thread(target=fn, daemon=True)
        worker.start()
        worker.join(timeout)
        assert not worker.is_alive(), "call deadlocked"

    def test_clear_cache_does_not_deadlock_and_empties(self, fake_joulescope):
        _, created = fake_joulescope
        JoulescopeJS220("watt1", 0, "JS220:SNA")
        JoulescopeJS220("watt2", 0, "JS220:SNB")
        assert len(JoulescopeJS220._instances) == 2

        self._run_with_timeout(JoulescopeJS220.clear_cache)

        assert JoulescopeJS220._instances == {}
        assert all(not dev.is_open for dev in created)

    def test_energy_clear_cache_does_not_deadlock(self, fake_joulescope):
        _, created = fake_joulescope
        JoulescopeEnergyAnalyzer("energy1", 0, "JS220:SNA")

        self._run_with_timeout(JoulescopeEnergyAnalyzer.clear_cache)

        assert JoulescopeEnergyAnalyzer._instances == {}
        # Releasing the analyzer also closed (and evicted) the shared JS220
        assert JoulescopeJS220._instances == {}
        assert all(not dev.is_open for dev in created)


# ---------------------------------------------------------------------------
# Section 3: two nets sharing one physical JS220 (watt1 + energy1 on STG-1)
# ---------------------------------------------------------------------------

class TestSharedSerialAcrossNets:

    def test_watt_close_does_not_break_energy_net(self, fake_joulescope):
        energy = JoulescopeEnergyAnalyzer("energy1", 0, "JS220:SNA")
        watt = JoulescopeJS220("watt1", 0, "JS220:SNA")
        assert energy._js220 is watt  # one shared device handle
        dev1 = watt._device

        assert watt.read(0.01) == pytest.approx(0.0066)
        watt.close()

        # The energy net re-acquires a freshly opened device instead of
        # failing on the closed shared handle
        stats = energy.read_stats(0.01)
        assert stats["power"]["mean"] == pytest.approx(0.0066)
        assert energy._js220._device is not dev1
        assert energy._js220._device.is_open and not dev1.is_open

        # And the watt net converges back onto the same healed instance
        watt2 = JoulescopeJS220("watt1", 0, "JS220:SNA")
        assert watt2 is energy._js220
        assert watt2.read(0.01) == pytest.approx(0.0066)

    def test_energy_close_does_not_break_watt_net(self, fake_joulescope):
        energy = JoulescopeEnergyAnalyzer("energy1", 0, "JS220:SNA")
        dev1 = energy._js220._device
        assert energy.read_stats(0.01)["voltage"]["mean"] == pytest.approx(3.3)
        energy.close()

        # Analyzer evicted itself and closed/evicted the shared JS220
        assert JoulescopeEnergyAnalyzer._instances == {}
        assert JoulescopeJS220._instances == {}

        watt = JoulescopeJS220("watt1", 0, "JS220:SNA")
        assert watt.read(0.01) == pytest.approx(0.0066)
        assert watt._device is not dev1
        assert watt._device.is_open and not dev1.is_open

    def test_interleaved_reads_with_close_after_each(self, fake_joulescope):
        # Mirrors the hardware verification sequence: watt, watt, watt,
        # energy, watt — each watt read followed by close() as _watt_meter()
        # does on the box.
        for _ in range(3):
            watt = JoulescopeJS220("watt1", 0, "JS220:SNA")
            assert watt.read(0.01) == pytest.approx(0.0066)
            watt.close()

        energy = JoulescopeEnergyAnalyzer("energy1", 0, "JS220:SNA")
        assert energy.read_stats(0.01)["current"]["mean"] == pytest.approx(0.002)

        watt = JoulescopeJS220("watt1", 0, "JS220:SNA")
        assert watt.read(0.01) == pytest.approx(0.0066)
        watt.close()


if __name__ == "__main__":
    import sys
    sys.exit(pytest.main([__file__, "-v"]))
