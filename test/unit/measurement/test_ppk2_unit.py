# Copyright 2024-2026 Lager Data LLC
# SPDX-License-Identifier: Apache-2.0

"""
Unit tests for PPK2 watt meter and energy analyzer implementations.

Tests pure logic (location parsing, dispatcher routing, singleton caching,
read method math, energy calculations) without requiring hardware.

Run with:
    python -m pytest test/unit/measurement/test_ppk2_unit.py -v
"""

import pytest
import numpy as np
from unittest.mock import patch, MagicMock

from lager.measurement.watt.ppk2_watt import _parse_location, PPK2Watt, DEFAULT_VOLTAGE_MV
from lager.measurement.energy_analyzer.ppk2_energy import PPK2EnergyAnalyzer
from lager.measurement.watt.dispatcher import WattMeterDispatcher
from lager.measurement.energy_analyzer.dispatcher import EnergyAnalyzerDispatcher
from lager.exceptions import WattBackendError, EnergyAnalyzerBackendError


# ---------------------------------------------------------------------------
# Section 1: _parse_location() — Pure logic, no mocks needed
# ---------------------------------------------------------------------------

class TestParseLocation:

    @pytest.mark.parametrize("location, expected_serial, expected_voltage", [
        (None, None, 3300),
        ("", None, 3300),
        ("  ", None, 3300),
        ("0", None, 3300),
        ("ABC123", "ABC123", 3300),
        ("ppk2:ABC123", "ABC123", 3300),
        ("PPK2:ABC123", "ABC123", 3300),
        ("ppk2:ABC123:1800", "ABC123", 1800),
        ("ppk2:ABC123:5000", "ABC123", 5000),
        ("ppk2:ABC123:notanumber", "ABC123", 3300),
        ("ppk2:", None, 3300),
        ("ppk2:0", None, 3300),
    ])
    def test_parse_location(self, location, expected_serial, expected_voltage):
        serial, voltage = _parse_location(location)
        assert serial == expected_serial
        assert voltage == expected_voltage


# ---------------------------------------------------------------------------
# Section 2: Watt dispatcher routing
# ---------------------------------------------------------------------------

class TestWattDispatcherRouting:

    def setup_method(self):
        self.dispatcher = WattMeterDispatcher()

    @pytest.mark.parametrize("instrument_name", [
        "ppk2", "PPK2", "ppk", "nordic", "Nordic PPK2",
    ])
    def test_choose_driver_ppk2(self, instrument_name):
        driver_cls = self.dispatcher._choose_driver(instrument_name)
        assert driver_cls is PPK2Watt

    def test_choose_driver_joulescope(self):
        from lager.measurement.watt.joulescope_js220 import JoulescopeJS220
        driver_cls = self.dispatcher._choose_driver("joulescope")
        assert driver_cls is JoulescopeJS220

    def test_choose_driver_yoctopuce_default(self):
        from lager.measurement.watt.yocto_watt import YoctoWatt
        assert self.dispatcher._choose_driver("yoctopuce") is YoctoWatt
        assert self.dispatcher._choose_driver("unknown_instrument") is YoctoWatt


# ---------------------------------------------------------------------------
# Section 3: Energy dispatcher routing
# ---------------------------------------------------------------------------

class TestEnergyDispatcherRouting:

    def setup_method(self):
        self.dispatcher = EnergyAnalyzerDispatcher()

    @pytest.mark.parametrize("instrument_name", [
        "ppk2", "PPK2", "ppk", "nordic", "Nordic PPK2",
    ])
    def test_choose_driver_ppk2(self, instrument_name):
        driver_cls = self.dispatcher._choose_driver(instrument_name)
        assert driver_cls is PPK2EnergyAnalyzer

    def test_choose_driver_joulescope(self):
        from lager.measurement.energy_analyzer.joulescope_energy import JoulescopeEnergyAnalyzer
        driver_cls = self.dispatcher._choose_driver("joulescope")
        assert driver_cls is JoulescopeEnergyAnalyzer

    def test_choose_driver_unsupported_raises(self):
        with pytest.raises(EnergyAnalyzerBackendError, match="Unsupported"):
            self.dispatcher._choose_driver("unknown_instrument")


# ---------------------------------------------------------------------------
# Helpers: mock PPK2_API factory
# ---------------------------------------------------------------------------

def _make_mock_ppk2_api(serial="SN123", port="/dev/ttyACM0",
                         samples=None):
    """Build a mock PPK2_API class and device instance."""
    if samples is None:
        samples = [1000.0, 2000.0, 3000.0]  # microamps

    mock_device = MagicMock()
    mock_device.get_modifiers.return_value = None
    mock_device.use_source_meter.return_value = None
    mock_device.set_source_voltage.return_value = None
    mock_device.start_measuring.return_value = None
    mock_device.stop_measuring.return_value = None
    mock_device.get_data.return_value = b"\x00" * 100
    mock_device.get_samples.return_value = samples

    mock_api = MagicMock()
    mock_api.list_devices.return_value = [port]
    mock_api.return_value = mock_device

    return mock_api, mock_device


# ---------------------------------------------------------------------------
# Section 4: PPK2Watt with mocked PPK2_API
# ---------------------------------------------------------------------------

class TestPPK2Watt:

    @pytest.fixture(autouse=True)
    def cleanup_cache(self):
        """Clear singleton caches before and after each test."""
        PPK2Watt.clear_cache()
        yield
        PPK2Watt.clear_cache()

    @patch("lager.measurement.watt.ppk2_watt.PPK2_API")
    @patch("lager.measurement.watt.ppk2_watt.time")
    def test_read_returns_correct_power(self, mock_time, mock_api_module):
        mock_api, mock_device = _make_mock_ppk2_api()
        mock_api_module.list_devices = mock_api.list_devices
        mock_api_module.return_value = mock_device

        watt = PPK2Watt("test_net", 0, "ppk2:SN123")
        power = watt.read()

        # samples [1000, 2000, 3000] µA → [0.001, 0.002, 0.003] A
        # mean = 0.002 A, voltage = 3.3 V → power = 0.0066 W
        assert power == pytest.approx(0.0066)

    @patch("lager.measurement.watt.ppk2_watt.PPK2_API")
    @patch("lager.measurement.watt.ppk2_watt.time")
    def test_read_current(self, mock_time, mock_api_module):
        mock_api, mock_device = _make_mock_ppk2_api()
        mock_api_module.list_devices = mock_api.list_devices
        mock_api_module.return_value = mock_device

        watt = PPK2Watt("test_net", 0, "ppk2:SN123")
        current = watt.read_current()

        assert current == pytest.approx(0.002)

    @patch("lager.measurement.watt.ppk2_watt.PPK2_API")
    @patch("lager.measurement.watt.ppk2_watt.time")
    def test_read_voltage(self, mock_time, mock_api_module):
        mock_api, mock_device = _make_mock_ppk2_api()
        mock_api_module.list_devices = mock_api.list_devices
        mock_api_module.return_value = mock_device

        watt = PPK2Watt("test_net", 0, "ppk2:SN123:3300")
        voltage = watt.read_voltage()

        assert voltage == pytest.approx(3.3)

    @patch("lager.measurement.watt.ppk2_watt.PPK2_API")
    @patch("lager.measurement.watt.ppk2_watt.time")
    def test_read_all(self, mock_time, mock_api_module):
        mock_api, mock_device = _make_mock_ppk2_api()
        mock_api_module.list_devices = mock_api.list_devices
        mock_api_module.return_value = mock_device

        watt = PPK2Watt("test_net", 0, "ppk2:SN123")
        result = watt.read_all()

        assert "current" in result
        assert "voltage" in result
        assert "power" in result
        assert result["current"] == pytest.approx(0.002)
        assert result["voltage"] == pytest.approx(3.3)
        assert result["power"] == pytest.approx(0.0066)

    @patch("lager.measurement.watt.ppk2_watt.PPK2_API")
    @patch("lager.measurement.watt.ppk2_watt.time")
    def test_read_raw(self, mock_time, mock_api_module):
        mock_api, mock_device = _make_mock_ppk2_api()
        mock_api_module.list_devices = mock_api.list_devices
        mock_api_module.return_value = mock_device

        watt = PPK2Watt("test_net", 0, "ppk2:SN123")
        current_amps, voltage_v = watt.read_raw(0.5)

        assert isinstance(current_amps, np.ndarray)
        assert isinstance(voltage_v, float)
        np.testing.assert_array_almost_equal(
            current_amps, [0.001, 0.002, 0.003]
        )
        assert voltage_v == pytest.approx(3.3)

    @patch("lager.measurement.watt.ppk2_watt.PPK2_API")
    @patch("lager.measurement.watt.ppk2_watt.time")
    def test_close_calls_stop_measuring(self, mock_time, mock_api_module):
        mock_api, mock_device = _make_mock_ppk2_api()
        mock_api_module.list_devices = mock_api.list_devices
        mock_api_module.return_value = mock_device

        watt = PPK2Watt("test_net", 0, "ppk2:SN123")
        watt.close()

        mock_device.stop_measuring.assert_called()

    @patch("lager.measurement.watt.ppk2_watt.PPK2_API")
    @patch("lager.measurement.watt.ppk2_watt.time")
    def test_custom_voltage(self, mock_time, mock_api_module):
        mock_api, mock_device = _make_mock_ppk2_api()
        mock_api_module.list_devices = mock_api.list_devices
        mock_api_module.return_value = mock_device

        watt = PPK2Watt("test_net", 0, "ppk2:SN123:1800")

        assert watt.read_voltage() == pytest.approx(1.8)
        mock_device.set_source_voltage.assert_called_with(1800)


# ---------------------------------------------------------------------------
# Section 5: PPK2Watt singleton behavior
# ---------------------------------------------------------------------------

class TestPPK2WattSingleton:

    @pytest.fixture(autouse=True)
    def cleanup_cache(self):
        PPK2Watt.clear_cache()
        yield
        PPK2Watt.clear_cache()

    @patch("lager.measurement.watt.ppk2_watt.PPK2_API")
    def test_same_serial_returns_same_instance(self, mock_api_module):
        mock_api, mock_device = _make_mock_ppk2_api()
        mock_api_module.list_devices = mock_api.list_devices
        mock_api_module.return_value = mock_device

        a = PPK2Watt("net_a", 0, "ppk2:SN123")
        b = PPK2Watt("net_b", 1, "ppk2:SN123")

        assert a is b

    @patch("lager.measurement.watt.ppk2_watt.PPK2_API")
    def test_different_serial_returns_different_instance(self, mock_api_module):
        mock_api_module.list_devices.return_value = [
            "/dev/ttyACM0",
            "/dev/ttyACM1",
        ]
        mock_device = MagicMock()
        mock_device.get_modifiers.return_value = None
        mock_device.use_source_meter.return_value = None
        mock_device.set_source_voltage.return_value = None
        mock_api_module.return_value = mock_device

        a = PPK2Watt("net_a", 0, "ppk2:SN123")
        b = PPK2Watt("net_b", 1, "ppk2:SN456")

        assert a is not b

    @patch("lager.measurement.watt.ppk2_watt.PPK2_API")
    def test_clear_cache_empties_instances(self, mock_api_module):
        mock_api, mock_device = _make_mock_ppk2_api()
        mock_api_module.list_devices = mock_api.list_devices
        mock_api_module.return_value = mock_device

        PPK2Watt("net_a", 0, "ppk2:SN123")
        assert len(PPK2Watt._instances) == 1

        PPK2Watt.clear_cache()
        assert len(PPK2Watt._instances) == 0
        mock_device.stop_measuring.assert_called()


# ---------------------------------------------------------------------------
# Section 6: PPK2EnergyAnalyzer with mocked PPK2Watt
# ---------------------------------------------------------------------------

class TestPPK2EnergyAnalyzer:

    @pytest.fixture(autouse=True)
    def cleanup_cache(self):
        PPK2EnergyAnalyzer.clear_cache()
        PPK2Watt.clear_cache()
        yield
        PPK2EnergyAnalyzer.clear_cache()
        PPK2Watt.clear_cache()

    @patch("lager.measurement.watt.ppk2_watt.PPK2Watt")
    def test_read_energy(self, MockPPK2Watt):
        mock_watt = MagicMock()
        MockPPK2Watt.return_value = mock_watt
        mock_watt.read_raw.return_value = (
            np.array([0.001, 0.002, 0.003]),
            3.3,
        )

        analyzer = PPK2EnergyAnalyzer("test_net", 0, "ppk2:SN123")
        result = analyzer.read_energy(1.0)

        # n=3, dt=1.0/3
        # charge_c = sum([0.001, 0.002, 0.003]) * (1/3) = 0.006 * 0.333... = 0.002
        # energy_j = sum([0.0033, 0.0066, 0.0099]) * (1/3) = 0.0198 * 0.333... = 0.0066
        assert result["charge_c"] == pytest.approx(0.002)
        assert result["energy_j"] == pytest.approx(0.0066)
        assert result["energy_wh"] == pytest.approx(0.0066 / 3600.0)
        assert result["charge_ah"] == pytest.approx(0.002 / 3600.0)
        assert result["duration_s"] == 1.0

    @patch("lager.measurement.watt.ppk2_watt.PPK2Watt")
    def test_read_stats(self, MockPPK2Watt):
        mock_watt = MagicMock()
        MockPPK2Watt.return_value = mock_watt
        mock_watt.read_raw.return_value = (
            np.array([0.001, 0.002, 0.003]),
            3.3,
        )

        analyzer = PPK2EnergyAnalyzer("test_net", 0, "ppk2:SN123")
        result = analyzer.read_stats(1.0)

        assert result["current"]["mean"] == pytest.approx(0.002)
        assert result["current"]["min"] == pytest.approx(0.001)
        assert result["current"]["max"] == pytest.approx(0.003)
        assert result["voltage"]["mean"] == pytest.approx(3.3)
        assert result["voltage"]["std"] == pytest.approx(0.0)
        assert result["power"]["mean"] == pytest.approx(0.0066)
        assert result["duration_s"] == 1.0

        # All stat values should be floats
        for key in ("current", "voltage", "power"):
            for stat in ("mean", "min", "max", "std"):
                assert isinstance(result[key][stat], float)


# ---------------------------------------------------------------------------
# Section 7: Error handling
# ---------------------------------------------------------------------------

class TestPPK2WattErrors:

    @pytest.fixture(autouse=True)
    def cleanup_cache(self):
        PPK2Watt.clear_cache()
        yield
        # Force-clear the cache dict directly since failed inits may leave
        # partially-constructed instances that can't be .close()'d safely.
        with PPK2Watt._instance_lock:
            PPK2Watt._instances.clear()

    @patch("lager.measurement.watt.ppk2_watt.PPK2_API")
    def test_no_devices_found(self, mock_api_module):
        mock_api_module.list_devices.return_value = []

        with pytest.raises(WattBackendError, match="No PPK2 devices found"):
            PPK2Watt("test_net", 0, None)

    @pytest.mark.filterwarnings("ignore::pytest.PytestUnraisableExceptionWarning")
    @patch("lager.measurement.watt.ppk2_watt.PPK2_API")
    def test_device_open_failure(self, mock_api_module):
        mock_api_module.list_devices.return_value = ["/dev/ttyACM0"]
        mock_api_module.return_value.get_modifiers.side_effect = Exception("USB error")

        with pytest.raises(WattBackendError, match="Failed to open PPK2 device"):
            PPK2Watt("test_net", 0, None)

    @pytest.mark.filterwarnings("ignore::pytest.PytestUnraisableExceptionWarning")
    @patch("lager.measurement.watt.ppk2_watt.PPK2_API", None)
    def test_missing_ppk2_library(self):
        with pytest.raises(WattBackendError, match="ppk2-api library not installed"):
            PPK2Watt("test_net", 0, None)
