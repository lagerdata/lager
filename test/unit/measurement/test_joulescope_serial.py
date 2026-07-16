# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

"""
Unit tests for JS220 location parsing and serial-number device matching.

Regression tests for the Workbench energy-analyzer bug: the energy
dispatcher passes the net's VISA address (USB0::0x16D0::0x10BA::004446::INSTR)
as `location`, which _parse_serial() misparsed to serial 'INSTR' (last
':'-token), and the scan filter matched serials against str(device) — while
the joulescope v1 API also has no top-level joulescope.Device to re-wrap the
match with. Result: "Joulescope with serial 'INSTR' not found. Available
devices: [<...DeviceJs220 object at 0x...>]" on every Workbench read, while
CLI reads (location unset -> scan_require_one) worked.

Run with:
    python -m pytest test/unit/measurement/test_joulescope_serial.py -v
"""

import types

import pytest

import lager.measurement.watt.joulescope_js220 as js_mod
from lager.measurement.watt.joulescope_js220 import (
    JoulescopeJS220,
    _matches_serial,
    _parse_serial,
)
from lager.measurement.energy_analyzer.joulescope_energy import JoulescopeEnergyAnalyzer
from lager.exceptions import WattBackendError

from .test_joulescope_cache import FakeJoulescopeDevice, make_fake_joulescope

VISA_ADDRESS = "USB0::0x16D0::0x10BA::004446::INSTR"


# ---------------------------------------------------------------------------
# Section 1: _parse_serial()
# ---------------------------------------------------------------------------

class TestParseSerial:

    @pytest.mark.parametrize("location, expected", [
        # VISA USB resource strings (the Workbench/dispatcher path)
        (VISA_ADDRESS, "004446"),
        ("usb0::0x16d0::0x10ba::004446::instr", "004446"),      # case-insensitive
        ("USB0::0x16D0::0x10BA::004446", "004446"),             # no ::INSTR suffix
        ("USB1::0x16D0::0x10BA::SN9::INSTR", "SN9"),
        # Non-USB / malformed VISA resources carry no USB serial
        ("TCPIP0::192.168.1.5::INSTR", None),
        ("USB0::0x16D0::0x10BA", None),
        ("USB0::0x16D0::0x10BA::::INSTR", None),
        # Pre-existing behaviors, unchanged
        (None, None),
        ("", None),
        ("   ", None),
        ("0", None),
        ("004446", "004446"),                                    # bare serial
        ("JS220:SN123", "SN123"),                                # prefix:serial
        ("joulescope:0:SN123", "SN123"),                         # multi-prefix
    ])
    def test_parse(self, location, expected):
        assert _parse_serial(location) == expected


# ---------------------------------------------------------------------------
# Section 2: device matching against v1-style scan results
# ---------------------------------------------------------------------------

class _StrOnlyDevice:
    """Device from an old API version: serial only visible via str()."""

    def __init__(self, serial):
        self._serial = serial

    def __str__(self):
        return "JS220-%s" % self._serial


class _DevicePathOnlyDevice:
    """Device exposing only device_path (no serial_number attribute)."""

    def __init__(self, serial):
        self.device_path = "u/js220/%s" % serial


class TestMatchesSerial:

    def test_matches_v1_serial_number_attribute(self):
        # The v1 Device repr is a bare object repr; matching must use the
        # serial_number attribute, not str(device).
        dev = FakeJoulescopeDevice("004446")
        assert "004446" not in repr(dev)
        assert _matches_serial(dev, "004446")
        assert not _matches_serial(dev, "005555")

    def test_matches_device_path(self):
        dev = _DevicePathOnlyDevice("004446")
        assert _matches_serial(dev, "004446")
        assert not _matches_serial(dev, "005555")

    def test_str_fallback_for_old_api(self):
        dev = _StrOnlyDevice("004446")
        assert _matches_serial(dev, "004446")
        assert not _matches_serial(dev, "005555")

    def test_case_insensitive(self):
        dev = FakeJoulescopeDevice("00ABCD")
        assert _matches_serial(dev, "00abcd")


# ---------------------------------------------------------------------------
# Section 3: end-to-end open by VISA address (the Workbench path)
# ---------------------------------------------------------------------------

@pytest.fixture
def two_device_bench(monkeypatch):
    """Fake joulescope lib with two v1-style devices on the bus."""
    fake, created = make_fake_joulescope(serials=("004446", "005555"))
    monkeypatch.setattr(js_mod, "joulescope", fake)
    JoulescopeJS220.clear_cache()
    JoulescopeEnergyAnalyzer.clear_cache()
    yield fake, created
    with JoulescopeJS220._instance_lock:
        JoulescopeJS220._instances.clear()
    with JoulescopeEnergyAnalyzer._instance_lock:
        JoulescopeEnergyAnalyzer._instances.clear()


class TestOpenByVisaAddress:

    def test_watt_opens_correct_device(self, two_device_bench):
        watt = JoulescopeJS220("watt1", 0, VISA_ADDRESS)
        assert watt._device.serial_number == "004446"
        assert watt._device.is_open
        assert watt.read(0.01) == pytest.approx(0.0066)

    def test_energy_analyzer_reads_via_visa_address(self, two_device_bench):
        # The exact failing Workbench call: energy dispatcher passes the
        # net's VISA address as location.
        energy = JoulescopeEnergyAnalyzer("energy1", 0, VISA_ADDRESS)
        stats = energy.read_stats(0.01)
        assert stats["power"]["mean"] == pytest.approx(0.0066)
        assert energy._js220._device.serial_number == "004446"

    def test_unmatched_serial_errors_with_serial_list(self, two_device_bench):
        # No silent fallback to scan_require_one, and the error lists device
        # serials instead of object reprs.
        with pytest.raises(WattBackendError) as excinfo:
            JoulescopeJS220("watt1", 0, "USB0::0x16D0::0x10BA::999999::INSTR")
        message = str(excinfo.value)
        assert "999999" in message
        assert "004446" in message and "005555" in message
        assert "object at 0x" not in message

    def test_visa_and_bare_serial_share_the_singleton(self, two_device_bench):
        # Same physical device addressed two ways -> one cached instance.
        a = JoulescopeJS220("energy1", 0, VISA_ADDRESS)
        b = JoulescopeJS220("watt1", 0, "004446")
        assert a is b


if __name__ == "__main__":
    import sys
    sys.exit(pytest.main([__file__, "-v"]))
