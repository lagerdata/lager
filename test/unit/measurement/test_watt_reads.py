# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

"""
Unit tests for the watt-meter current/voltage/all reads, the shared SI
formatter, and the duration (averaging-window) parameter.

These back the `lager watt <net> current|voltage|all` feature: the base class
must degrade gracefully on instruments that only measure power (Yocto-Watt),
the SI formatter must scale small readings (the `0.000 W` fix), and the
current-sensing drivers must forward the averaging window to their capture.

Run with:
    python -m pytest test/unit/measurement/test_watt_reads.py -v
"""

import numpy as np
import pytest
from unittest.mock import patch, MagicMock

from lager.measurement.format_utils import fmt_si
from lager.measurement.watt.accumulator import average_from_accumulators
from lager.measurement.watt.watt_net import WattMeterBase, UnsupportedInstrumentError
from lager.measurement.watt.ppk2_watt import PPK2Watt


# ---------------------------------------------------------------------------
# Section 1: SI formatter (the "0.000 W" resolution fix)
# ---------------------------------------------------------------------------

class TestFmtSi:

    @pytest.mark.parametrize("value, unit, expected", [
        (0.0, "W", "0.000 W"),
        (5.23, "W", "5.230 W"),
        (3.3, "V", "3.300 V"),
        (0.0523, "W", "52.300 mW"),
        (0.012, "A", "12.000 mA"),
        (52.34e-6, "W", "52.340 µW"),
        (5e-9, "A", "5.000 nA"),
        (-0.012, "A", "-12.000 mA"),
        # Small but still representable with an SI prefix -> stays SI.
        (1.2e-11, "W", "0.012 nW"),
        (5.23e-13, "W", "0.001 nW"),
        # Too small for the nano prefix (would round to 0.000) -> scientific.
        (3e-13, "W", "3.000e-13 W"),
        (1e-15, "A", "1.000e-15 A"),
        (-4.5e-13, "W", "-4.500e-13 W"),
    ])
    def test_scaling(self, value, unit, expected):
        assert fmt_si(value, unit) == expected

    def test_small_nonzero_does_not_round_to_base_zero(self):
        # The headline bug: a 52 µW load printed as "0.000 W". It must not.
        assert fmt_si(52e-6, "W") != "0.000 W"
        assert "µW" in fmt_si(52e-6, "W")

    def test_deep_tail_uses_scientific_not_zero(self):
        # Numbers too small for the nano prefix must show in scientific
        # notation rather than rounding to 0.
        out = fmt_si(4e-13, "W")
        assert out == "4.000e-13 W"
        assert "e-" in out
        assert not out.startswith("0.000")


# ---------------------------------------------------------------------------
# Section 1b: Accumulator averaging math (gapless long-window average)
# ---------------------------------------------------------------------------

class TestAccumulatorMath:

    FS = 2_000_000.0  # JS220 sample rate

    def test_basic_average(self):
        # 10s window; Δcharge=0.20 C -> 0.02 A, Δenergy=0.66 J -> 0.066 W,
        # voltage = ΔE/Δq = 3.3 V.
        r = average_from_accumulators(
            charge0=0.0, energy0=0.0, sample0=0.0,
            charge1=0.20, energy1=0.66, sample1=10 * self.FS,
            sample_freq=self.FS,
        )
        assert r["current"] == pytest.approx(0.02)
        assert r["power"] == pytest.approx(0.066)
        assert r["voltage"] == pytest.approx(3.3)

    def test_nonzero_baseline_uses_deltas(self):
        # Accumulators are running integrals; only the delta over the window
        # counts, not the absolute value.
        r = average_from_accumulators(
            charge0=5.0, energy0=17.0, sample0=100 * self.FS,
            charge1=5.20, energy1=17.66, sample1=110 * self.FS,
            sample_freq=self.FS,
        )
        assert r["current"] == pytest.approx(0.02)
        assert r["power"] == pytest.approx(0.066)
        assert r["voltage"] == pytest.approx(3.3)

    def test_zero_charge_uses_fallback_voltage(self):
        r = average_from_accumulators(
            0.0, 0.0, 0.0, 0.0, 0.0, 5 * self.FS, self.FS, fallback_voltage=3.30,
        )
        assert r["current"] == 0.0
        assert r["power"] == 0.0
        assert r["voltage"] == pytest.approx(3.30)

    def test_nonpositive_window_raises(self):
        with pytest.raises(ValueError):
            average_from_accumulators(0, 0, 100, 1, 1, 100, self.FS)


# ---------------------------------------------------------------------------
# Section 2: Base-class graceful degradation (power-only instruments)
# ---------------------------------------------------------------------------

class _PowerOnlyMeter(WattMeterBase):
    """Stand-in for a power-only instrument (e.g. Yocto-Watt)."""

    def read(self, duration: float = 0.1) -> float:
        return 1.23


class TestPowerOnlyDegradation:

    def setup_method(self):
        self.meter = _PowerOnlyMeter("pwr_only", 0)

    def test_read_power_works(self):
        assert self.meter.read() == pytest.approx(1.23)
        # duration is accepted even if ignored
        assert self.meter.read(2.0) == pytest.approx(1.23)

    def test_read_current_unsupported(self):
        with pytest.raises(UnsupportedInstrumentError, match="current"):
            self.meter.read_current()

    def test_read_voltage_unsupported(self):
        with pytest.raises(UnsupportedInstrumentError, match="voltage"):
            self.meter.read_voltage()

    def test_read_all_unsupported(self):
        with pytest.raises(UnsupportedInstrumentError):
            self.meter.read_all()


# ---------------------------------------------------------------------------
# Section 3: Current-sensing driver forwards the averaging window
# ---------------------------------------------------------------------------

def _make_ppk2(mock_api_module, serial="SN1"):
    """Build a PPK2Watt with a mocked PPK2_API backing it."""
    mock_device = MagicMock()
    mock_device.get_modifiers.return_value = None
    mock_api_module.list_devices.return_value = ["/dev/ttyACM0"]
    mock_api_module.return_value = mock_device
    PPK2Watt.clear_cache()
    return PPK2Watt("net", 0, f"ppk2:{serial}")


class TestDurationForwarding:

    @pytest.fixture(autouse=True)
    def cleanup_cache(self):
        PPK2Watt.clear_cache()
        yield
        PPK2Watt.clear_cache()

    @patch("lager.measurement.watt.ppk2_watt.PPK2_API")
    def test_duration_passed_to_capture(self, mock_api_module):
        inst = _make_ppk2(mock_api_module)
        samples = (np.array([0.001, 0.002, 0.003]), 3.3)
        with patch.object(inst, "read_raw", return_value=samples) as rr:
            inst.read(0.7)
            inst.read_current(0.9)
            inst.read_all(1.1)
        forwarded = [c.args[0] for c in rr.call_args_list]
        assert forwarded == [0.7, 0.9, 1.1]

    @patch("lager.measurement.watt.ppk2_watt.PPK2_API")
    def test_default_duration_is_point_one(self, mock_api_module):
        inst = _make_ppk2(mock_api_module)
        samples = (np.array([0.001, 0.002, 0.003]), 3.3)
        with patch.object(inst, "read_raw", return_value=samples) as rr:
            inst.read()
            inst.read_current()
            inst.read_all()
        forwarded = [c.args[0] for c in rr.call_args_list]
        assert forwarded == [0.1, 0.1, 0.1]


if __name__ == "__main__":
    import sys
    sys.exit(pytest.main([__file__, "-v"]))
