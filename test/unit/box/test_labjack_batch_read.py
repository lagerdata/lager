# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0
"""
Unit tests for POST /labjack/batch_read (box/lager/hardware_service.py).

The endpoint reads GPIO/ADC/DAC state for every net on one LabJack T7 in a
single handle session, on behalf of ``GET /nets/state``. Two properties are
pinned here, because the first version of it got both wrong in ways no test
would have caught.

**It must take the lock ``/invoke`` takes.** Every ``lager gpo``/``gpi``/
``adc``/``dac`` goes through ``/invoke``, which locks on the ``device_id`` from
``_physical_device_id()`` -- ``"labjack:" + the net's address``. The batch read
hardcoded ``_get_address_lock("labjack:ANY")``, so on any bench whose LabJack
net carries an address it took a *different lock object* and could interleave
I/O on the shared LJM handle with an in-flight command. Its own docstring
claimed the opposite. The two sides now key on one identity, supplied by the
caller that already resolved it.

**It must not write to the device.** A state display is read-only. The first
version wrote AIN config (``_RANGE``, ``_NEGATIVE_CH``, ``_RESOLUTION_INDEX``,
``_SETTLING_US``) before reading, to avoid "stale differential-mode config from
a previous tool" -- but that config belongs to the caller, and forcing every
channel to single-ended +/-10V silently destroyed a deliberately configured
differential pair mid-measurement. Same defect class as the unconditional GPIO
``dev.input()`` removed from this path earlier: the caller asked what the state
WAS, not to change it.

Hardware-only imports are stubbed in sys.modules before import, so these run on
any machine; the Flask route is driven through its test client.
"""

import os
import sys
import types
import unittest
from unittest.mock import MagicMock, patch


def _make_module(name):
    mod = types.ModuleType(name)
    mod.__getattr__ = lambda attr: MagicMock()  # type: ignore[method-assign]
    return mod


def _stub(dotted):
    parts = dotted.split('.')
    for i in range(1, len(parts) + 1):
        key = '.'.join(parts[:i])
        if key not in sys.modules:
            sys.modules[key] = _make_module(key)


_HARDWARE_STUBS = [
    'pyvisa', 'pyvisa.constants', 'pyvisa_py',
    'usb', 'usb.util', 'usb.core',
    'pigpio',
    'labjack', 'labjack.ljm',
    'nidaqmx',
    'phidget22', 'phidget22.Phidget', 'phidget22.Net',
    'bleak',
    'picoscope',
    'serial', 'serial.tools', 'serial.tools.list_ports',
    'spidev',
    'smbus', 'smbus2',
    'RPi', 'RPi.GPIO',
    'gpiod',
]
for _dep in _HARDWARE_STUBS:
    _stub(_dep)

_BOX_ROOT = os.path.abspath(
    os.path.join(os.path.dirname(__file__), '..', '..', '..', 'box')
)
if _BOX_ROOT not in sys.path:
    sys.path.insert(0, _BOX_ROOT)

import lager.hardware_service as hw  # noqa: E402

# A real bench T7 address. The serial field is empty on this hardware, which is
# exactly why a test using a placeholder address hides the bug: with
# address "ANY", _physical_device_id happens to return "labjack:ANY" and the
# old hardcoded key matched by luck.
T7_ADDRESS = "USB0::0x0CD5::0x0007::::INSTR"
T7_DEVICE_ID = "labjack:" + T7_ADDRESS

NETS = [
    {"name": "gpio5", "role": "gpio", "pin": "EIO0"},
    {"name": "adc1", "role": "adc", "pin": "0"},
    {"name": "dac1", "role": "dac", "pin": "0"},
]


class _FakeLJM:
    """Records every LJM call so a write can be asserted absent."""

    def __init__(self):
        self.reads = []
        self.writes = []

    def eReadName(self, handle, name):
        self.reads.append(name)
        return 0

    def eReadNames(self, handle, count, names):
        self.reads.extend(names)
        return [1.5] * count

    def eWriteName(self, handle, name, value):
        self.writes.append((name, value))


def _post(payload, fake_ljm=None):
    """Drive the route with LJM stubbed, returning (response_json, fake_ljm)."""
    fake_ljm = fake_ljm or _FakeLJM()
    handle_mod = types.ModuleType("lager.io.labjack_handle")
    handle_mod.get_labjack_handle = lambda: 1          # type: ignore[attr-defined]
    handle_mod.ljm = fake_ljm                          # type: ignore[attr-defined]
    handle_mod._LJM_ERR = None                         # type: ignore[attr-defined]
    with patch.dict(sys.modules, {"lager.io.labjack_handle": handle_mod}):
        client = hw.app.test_client()
        resp = client.post("/labjack/batch_read", json=payload)
    return resp, fake_ljm


class LockIdentityTests(unittest.TestCase):
    """The batch read and /invoke must contend on ONE lock object."""

    def test_locks_on_the_device_id_the_caller_sent(self):
        seen = []
        real = hw._get_address_lock

        def spy(address):
            seen.append(address)
            return real(address)

        with patch.object(hw, "_get_address_lock", side_effect=spy):
            _post({"nets": NETS, "device_id": T7_DEVICE_ID})

        self.assertEqual(seen, [T7_DEVICE_ID])

    def test_lock_is_the_same_object_invoke_would_take(self):
        """The property that actually matters -- not the string, the lock."""
        taken = []
        real = hw._get_address_lock

        def capture(address):
            lock = real(address)
            taken.append(lock)
            return lock

        with patch.object(hw, "_get_address_lock", side_effect=capture):
            _post({"nets": NETS, "device_id": T7_DEVICE_ID})

        # /invoke resolves its lock from the same device_id, via this helper.
        invoke_lock = real(T7_DEVICE_ID)
        self.assertEqual(len(taken), 1)
        self.assertIs(taken[0], invoke_lock)

    def test_addressed_bench_does_not_collapse_to_the_ANY_key(self):
        """The regression itself: a hardcoded "labjack:ANY" is a different
        lock from the one an addressed net resolves to, so the two paths could
        run concurrently on one USB handle."""
        self.assertNotEqual(T7_DEVICE_ID, "labjack:ANY")
        self.assertIsNot(hw._get_address_lock(T7_DEVICE_ID),
                         hw._get_address_lock("labjack:ANY"))

    def test_missing_device_id_falls_back_to_the_ANY_key(self):
        """Older callers send no device_id. _physical_device_id yields
        "labjack:ANY" for an address-less record, so that stays the fallback."""
        seen = []
        with patch.object(hw, "_get_address_lock",
                          side_effect=lambda a: seen.append(a) or hw._get_device_lock(a)):
            _post({"nets": NETS})
        self.assertEqual(seen, ["labjack:ANY"])


def _post_with(handle_mod_attrs, payload):
    handle_mod = types.ModuleType("lager.io.labjack_handle")
    for k, v in handle_mod_attrs.items():
        setattr(handle_mod, k, v)
    with patch.dict(sys.modules, {"lager.io.labjack_handle": handle_mod}):
        return hw.app.test_client().post("/labjack/batch_read", json=payload)


class DeviceErrorHeaderTests(unittest.TestCase):
    """Why nets came back null travels in X-Lager-Device-Error, so
    `lager nets state` can say it instead of "no value from instrument"."""

    def test_an_unplugged_t7_names_the_open_error(self):
        def not_found():
            raise RuntimeError("LabJack T7 not found on USB: check that it is plugged in (1227)")
        resp = _post_with({"get_labjack_handle": not_found, "ljm": _FakeLJM(), "_LJM_ERR": None},
                          {"nets": NETS, "device_id": T7_DEVICE_ID})
        self.assertEqual(resp.get_json(), {n["name"]: None for n in NETS})
        self.assertIn("not found on USB", resp.headers.get("X-Lager-Device-Error", ""))

    def test_a_missing_ljm_library_is_named(self):
        resp = _post_with({"get_labjack_handle": lambda: 1, "ljm": None,
                           "_LJM_ERR": "libLabJackM.dylib is not available"},
                          {"nets": NETS, "device_id": T7_DEVICE_ID})
        self.assertEqual(resp.get_json(), {n["name"]: None for n in NETS})
        self.assertIn("LJM library not available", resp.headers["X-Lager-Device-Error"])
        self.assertIn("libLabJackM.dylib", resp.headers["X-Lager-Device-Error"])

    def test_no_header_when_every_net_read(self):
        resp, _ = _post({"nets": NETS, "device_id": T7_DEVICE_ID})
        self.assertNotIn("X-Lager-Device-Error", resp.headers)

    def test_the_header_is_one_bounded_ascii_line(self):
        def noisy():
            raise RuntimeError("line one\nline two \u2014 " + "x" * 1000)
        resp = _post_with({"get_labjack_handle": noisy, "ljm": _FakeLJM(), "_LJM_ERR": None},
                          {"nets": NETS, "device_id": T7_DEVICE_ID})
        value = resp.headers["X-Lager-Device-Error"]
        self.assertNotIn("\n", value)
        self.assertLessEqual(len(value), hw._DEVICE_ERROR_HEADER_MAX)
        value.encode("ascii")


class ReadOnlyTests(unittest.TestCase):
    """A state probe must not reconfigure the instrument."""

    def test_probe_issues_no_writes_at_all(self):
        resp, ljm = _post({"nets": NETS, "device_id": T7_DEVICE_ID})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(ljm.writes, [],
                         "a read-only state probe wrote to the device: %r" % (ljm.writes,))

    def test_does_not_touch_ain_range_or_negative_channel(self):
        """Named explicitly: these are the registers that silently destroyed a
        caller's differential-pair configuration."""
        _, ljm = _post({"nets": NETS, "device_id": T7_DEVICE_ID})
        written = {name for name, _ in ljm.writes}
        for reg in ("AIN0_RANGE", "AIN0_NEGATIVE_CH",
                    "AIN0_RESOLUTION_INDEX", "AIN0_SETTLING_US"):
            self.assertNotIn(reg, written)

    def test_gpio_read_uses_direction_and_state_registers(self):
        """Kept from the batch design: DIO_DIRECTION + DIO_STATE rather than a
        per-pin eReadName, which is what made EIO/CIO/MIO readable without
        mutating pin direction."""
        _, ljm = _post({"nets": NETS, "device_id": T7_DEVICE_ID})
        self.assertIn("DIO_DIRECTION", ljm.reads)
        self.assertIn("DIO_STATE", ljm.reads)

    def test_still_returns_a_value_per_net(self):
        resp, _ = _post({"nets": NETS, "device_id": T7_DEVICE_ID})
        body = resp.get_json()
        for net in NETS:
            self.assertIn(net["name"], body)


if __name__ == "__main__":
    unittest.main()
