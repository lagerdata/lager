# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0
"""
Unit tests for GET /nets/state (box/lager/http_handlers/nets_handler.py).

The endpoint reports a one-line live state for every saved net. Two properties
matter more than the formatting, and both are pinned here:

**It must never let one instrument take down the request.** The first version
passed ``timeout=`` to ``as_completed`` inside a ``with ThreadPoolExecutor``
block. ``as_completed`` raises its TimeoutError out of the ``for`` *statement*,
so the ``except`` inside the loop body never saw it and a slow instrument
produced an HTTP 500 for the whole bench. Worse, ``with`` shuts the pool down
with ``wait=True``, so the request blocked on the wedged probe for its full
duration anyway -- holding a box HTTP worker -- before returning that 500. On a
bench with 15 USB nets across 3 hubs, two of them not currently discoverable,
that measured ~20s and a 500.

**It must probe per instrument, not per net.** Every driver wraps each call in
its own open -> operate -> close cycle under the instrument's lock, so N nets on
one hub cost N full enumerate/connect/disconnect cycles, serialised, no matter
how wide the thread pool is. A single Acroname port read measured ~2.4s on real
hardware (~4.5s for a hub that is not discoverable), so the 8-port hub alone was
~20s of the wall clock. Grouping by instrument pays that once per hub.

Hardware-only imports (pyvisa, usb, labjack, ...) are stubbed in sys.modules
before import so these run on any machine; the Flask route is then driven
through its test client.
"""

import os
import sys
import threading
import time
import types
import unittest
from unittest.mock import MagicMock, patch


def _make_module(name: str) -> types.ModuleType:
    mod = types.ModuleType(name)
    mod.__getattr__ = lambda attr: MagicMock()  # type: ignore[method-assign]
    return mod


def _stub(dotted: str) -> None:
    parts = dotted.split('.')
    for i in range(1, len(parts) + 1):
        key = '.'.join(parts[:i])
        if key not in sys.modules:
            sys.modules[key] = _make_module(key)


_HARDWARE_STUBS = [
    'pyvisa', 'pyvisa.constants', 'pyvisa_py',
    'usb', 'usb.util', 'usb.core',
    'pigpio', 'labjack', 'labjack.ljm', 'nidaqmx',
    'phidget22', 'phidget22.Phidget', 'phidget22.Net',
    'bleak', 'picoscope', 'brainstem',
    'serial', 'serial.tools', 'serial.tools.list_ports',
    'spidev', 'smbus', 'smbus2', 'RPi', 'RPi.GPIO', 'gpiod',
    'flask_socketio',
]
for _dep in _HARDWARE_STUBS:
    _stub(_dep)

_BOX_ROOT = os.path.abspath(
    os.path.join(os.path.dirname(__file__), '..', '..', '..', 'box')
)
if _BOX_ROOT not in sys.path:
    sys.path.insert(0, _BOX_ROOT)

from flask import Flask  # noqa: E402
from lager.http_handlers import nets_handler  # noqa: E402


# Two hubs and a LabJack: three instruments, six nets. Mirrors the shape that
# made the original slow -- several nets sharing one physical device.
SAVED_NETS = [
    {"name": "usb1", "role": "usb", "instrument": "Acroname_8Port",
     "address": "USB0::0x24FF::0x0013::AAA::INSTR", "pin": "0"},
    {"name": "usb2", "role": "usb", "instrument": "Acroname_8Port",
     "address": "USB0::0x24FF::0x0013::AAA::INSTR", "pin": "1"},
    {"name": "usb3", "role": "usb", "instrument": "Acroname_8Port",
     "address": "USB0::0x24FF::0x0013::AAA::INSTR", "pin": "2"},
    {"name": "usb9", "role": "usb", "instrument": "YKUSH_Hub",
     "address": "USB0::0x04D8::0xF2F7::YK1::INSTR", "pin": "1"},
    {"name": "gpi1", "role": "gpio", "instrument": "LabJack_T7",
     "address": "ANY", "pin": "FIO0"},
    {"name": "uart1", "role": "uart", "instrument": "FTDI_FT232R",
     "address": "USB0::INSTR", "pin": "/dev/ttyUSB0"},
]


def _make_client():
    app = Flask(__name__)
    nets_handler.register_nets_routes(app)
    return app.test_client()


class GroupingTests(unittest.TestCase):
    """_group_key decides what shares one instrument session."""

    def test_same_instrument_and_address_group_together(self):
        keys = {nets_handler._group_key(r) for r in SAVED_NETS if r["role"] == "usb"
                and r["instrument"] == "Acroname_8Port"}
        self.assertEqual(len(keys), 1, "three ports on one hub must be one group")

    def test_different_hubs_do_not_group(self):
        acro = nets_handler._group_key(SAVED_NETS[0])
        ykush = nets_handler._group_key(SAVED_NETS[3])
        self.assertNotEqual(acro, ykush)

    def test_same_instrument_model_different_address_does_not_group(self):
        # Two identical hub models are still two physical devices with two
        # separate locks, so they must be probed in parallel, not serialised.
        a = nets_handler._group_key(
            {"role": "usb", "instrument": "Acroname_8Port", "address": "USB0::A"})
        b = nets_handler._group_key(
            {"role": "usb", "instrument": "Acroname_8Port", "address": "USB0::B"})
        self.assertNotEqual(a, b)

    def test_role_is_part_of_the_key(self):
        # Batch probes are per-role, so a gpio and an adc net on one LabJack
        # cannot share a work unit even though they share a device.
        gpio = nets_handler._group_key(
            {"role": "gpio", "instrument": "LabJack_T7", "address": "ANY"})
        adc = nets_handler._group_key(
            {"role": "adc", "instrument": "LabJack_T7", "address": "ANY"})
        self.assertNotEqual(gpio, adc)


class ProbeGroupTests(unittest.TestCase):
    """_probe_group prefers the role's batch probe, and always answers."""

    def test_batch_probe_is_called_once_for_the_whole_group(self):
        calls = []

        def fake_batch(names):
            calls.append(list(names))
            return {n: "enabled" for n in names}

        recs = [r for r in SAVED_NETS if r["instrument"] == "Acroname_8Port"]
        with patch.dict(nets_handler._BATCH_PROBES, {"usb": fake_batch}):
            out = nets_handler._probe_group(recs)

        # THE perf regression: one call for three nets, not three calls.
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0], ["usb1", "usb2", "usb3"])
        self.assertEqual([e["state"] for e in out], ["enabled"] * 3)

    def test_falls_back_to_per_net_probe_without_a_batch_form(self):
        seen = []

        def fake_probe(name, rec):
            seen.append(name)
            return "HIGH (1)"

        with patch.dict(nets_handler._BRIEF_PROBES, {"gpio": fake_probe}), \
             patch.dict(nets_handler._BATCH_PROBES, {}, clear=True):
            out = nets_handler._probe_group([SAVED_NETS[4]])

        self.assertEqual(seen, ["gpi1"])
        self.assertEqual(out[0]["state"], "HIGH (1)")

    def test_raising_batch_probe_yields_nulls_not_an_exception(self):
        def boom(names):
            raise RuntimeError("hub fell off the bus")

        recs = [r for r in SAVED_NETS if r["instrument"] == "Acroname_8Port"]
        with patch.dict(nets_handler._BATCH_PROBES, {"usb": boom}):
            out = nets_handler._probe_group(recs)

        self.assertEqual([e["state"] for e in out], [None, None, None])
        self.assertEqual([e["name"] for e in out], ["usb1", "usb2", "usb3"])

    def test_role_with_no_probe_at_all_is_null(self):
        out = nets_handler._probe_group([SAVED_NETS[5]])  # uart
        self.assertEqual(out[0]["state"], None)
        self.assertEqual(out[0]["role"], "uart")


class EndpointTests(unittest.TestCase):

    def setUp(self):
        self.client = _make_client()

    def test_no_saved_nets_returns_empty_list(self):
        with patch.object(nets_handler.Net, "list_saved", return_value=[]):
            resp = self.client.get('/nets/state')
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.get_json(), [])

    def test_list_saved_blowing_up_is_not_a_500(self):
        with patch.object(nets_handler.Net, "list_saved",
                          side_effect=RuntimeError("bad json")):
            resp = self.client.get('/nets/state')
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.get_json(), [])

    def test_one_entry_per_saved_net_in_saved_order(self):
        with patch.object(nets_handler.Net, "list_saved", return_value=SAVED_NETS), \
             patch.dict(nets_handler._BATCH_PROBES,
                        {"usb": lambda names: {n: "enabled" for n in names}}), \
             patch.dict(nets_handler._BRIEF_PROBES,
                        {"gpio": lambda n, rec: "LOW (0)"}):
            resp = self.client.get('/nets/state')

        self.assertEqual(resp.status_code, 200)
        body = resp.get_json()
        self.assertEqual([e["name"] for e in body],
                         [r["name"] for r in SAVED_NETS])
        by_name = {e["name"]: e["state"] for e in body}
        self.assertEqual(by_name["usb1"], "enabled")
        self.assertEqual(by_name["gpi1"], "LOW (0)")
        self.assertIsNone(by_name["uart1"])

    def test_a_wedged_instrument_does_not_500_or_block_the_request(self):
        """THE regression. A probe that never returns must cost its own nets
        their state and nothing else: still 200, still one entry per net, and
        the request must not wait for it."""
        release = threading.Event()

        def wedged(names):
            # Blocks the way a driver blocked on a hub lock does. Released in
            # the finally below so the suite stays fast.
            release.wait(timeout=30)
            return {n: "enabled" for n in names}

        try:
            with patch.object(nets_handler, "_STATE_TIMEOUT", 0.4), \
                 patch.object(nets_handler.Net, "list_saved", return_value=SAVED_NETS), \
                 patch.dict(nets_handler._BATCH_PROBES, {"usb": wedged}), \
                 patch.dict(nets_handler._BRIEF_PROBES, {"gpio": lambda n, rec: "LOW (0)"}):
                started = time.monotonic()
                resp = self.client.get('/nets/state')
                elapsed = time.monotonic() - started
        finally:
            release.set()

        self.assertEqual(resp.status_code, 200, "a wedged probe must not 500")
        # Bounded by the deadline, NOT by the wedged probe. The old code waited
        # for the probe even after the deadline, because `with` joins the pool.
        self.assertLess(elapsed, 5.0,
                        f"request took {elapsed:.1f}s; it must not wait on a "
                        "wedged probe")

        body = resp.get_json()
        self.assertEqual(len(body), len(SAVED_NETS))
        by_name = {e["name"]: e["state"] for e in body}
        # The wedged hubs report unknown...
        self.assertIsNone(by_name["usb1"])
        self.assertIsNone(by_name["usb9"])
        # ...while an unrelated instrument still answers.
        self.assertEqual(by_name["gpi1"], "LOW (0)")

    def test_one_bad_instrument_does_not_hide_the_healthy_ones(self):
        def boom(names):
            raise RuntimeError("no hub")

        with patch.object(nets_handler.Net, "list_saved", return_value=SAVED_NETS), \
             patch.dict(nets_handler._BATCH_PROBES, {"usb": boom}), \
             patch.dict(nets_handler._BRIEF_PROBES, {"gpio": lambda n, rec: "HIGH (1)"}):
            resp = self.client.get('/nets/state')

        self.assertEqual(resp.status_code, 200)
        by_name = {e["name"]: e["state"] for e in resp.get_json()}
        self.assertIsNone(by_name["usb1"])
        self.assertEqual(by_name["gpi1"], "HIGH (1)")

    def test_instruments_are_probed_concurrently(self):
        """Distinct instruments must overlap. If the pool ever collapsed to one
        worker, or grouping merged separate devices, this would serialise."""
        concurrent = []
        lock = threading.Lock()
        active = {"n": 0}

        def slow_batch(names):
            with lock:
                active["n"] += 1
                concurrent.append(active["n"])
            time.sleep(0.25)
            with lock:
                active["n"] -= 1
            return {n: "enabled" for n in names}

        with patch.object(nets_handler.Net, "list_saved", return_value=SAVED_NETS), \
             patch.dict(nets_handler._BATCH_PROBES, {"usb": slow_batch}):
            resp = self.client.get('/nets/state')

        self.assertEqual(resp.status_code, 200)
        # Two distinct hubs -> both in flight at once.
        self.assertGreaterEqual(max(concurrent), 2,
                                "the two hubs should have been probed in parallel")


class GpioReadSafetyTests(unittest.TestCase):
    """A state *display* must not reconfigure a pin.

    On a LabJack T7, eReadName on a DIO channel reconfigures that pin as an
    input. io/gpio/labjack_t7.py guards only FIO pins (direction check, then
    DIO_STATE when the pin is an output); _get_pin_number returns None for
    anything not FIO<n>, so EIO/CIO/MIO take the unguarded direct read. A pin
    holding a target in reset would be silently released by a state sweep.

    Pin names below are a real bench layout: 23 GPIO nets on one T7, of which
    only FIO0-7 are safe to read.
    """

    def _rec(self, pin, instrument="LabJack_T7"):
        return {"name": "g", "role": "gpio", "instrument": instrument,
                "address": "ANY", "pin": pin}

    def test_fio_pins_are_safe(self):
        for pin in ["FIO0", "FIO7", "fio3"]:
            self.assertTrue(
                nets_handler._gpio_read_is_side_effect_free(self._rec(pin)), pin)

    def test_bare_index_is_treated_as_fio(self):
        self.assertTrue(
            nets_handler._gpio_read_is_side_effect_free(self._rec("5")))

    def test_eio_cio_mio_are_not_safe(self):
        for pin in ["EIO0", "EIO7", "CIO0", "CIO3", "MIO0", "MIO2", "cio1"]:
            self.assertFalse(
                nets_handler._gpio_read_is_side_effect_free(self._rec(pin)), pin)

    def test_non_labjack_backends_are_left_alone(self):
        # The hazard is specific to the T7 driver's unguarded path.
        self.assertTrue(nets_handler._gpio_read_is_side_effect_free(
            self._rec("GPIO17", instrument="RaspberryPi")))

    def test_unsafe_pin_reports_null_without_touching_the_device(self):
        called = []

        def should_not_run(netname, role):
            called.append(netname)
            raise AssertionError("the probe must not open the device")

        with patch.object(nets_handler, "_proxy", should_not_run, create=True):
            out = nets_handler._brief_gpio("gpio5", "gpio", self._rec("EIO0"))

        self.assertIsNone(out)
        self.assertEqual(called, [], "no device access for an unsafe pin")

    def test_endpoint_reports_null_for_unsafe_pins_and_reads_safe_ones(self):
        nets = [
            {"name": "gpio1", "role": "gpio", "instrument": "LabJack_T7",
             "address": "ANY", "pin": "CIO0"},
            {"name": "gpio5", "role": "gpio", "instrument": "LabJack_T7",
             "address": "ANY", "pin": "EIO0"},
            {"name": "gpio16", "role": "gpio", "instrument": "LabJack_T7",
             "address": "ANY", "pin": "FIO0"},
        ]
        reads = []

        def fake_proxy(netname, role, timeout=None):
            reads.append(netname)
            dev = MagicMock()
            dev.input.return_value = 1
            return dev

        with patch.object(nets_handler.Net, "list_saved", return_value=nets), \
             patch("lager.http_handlers.net_command._proxy", fake_proxy):
            resp = _make_client().get('/nets/state')

        by_name = {e["name"]: e["state"] for e in resp.get_json()}
        self.assertIsNone(by_name["gpio1"])
        self.assertIsNone(by_name["gpio5"])
        self.assertEqual(by_name["gpio16"], "HIGH (1)")
        # Only the FIO net was ever read.
        self.assertEqual(reads, ["gpio16"])


class UsbBatchProbeTests(unittest.TestCase):
    """_brief_usb_batch maps the dispatcher's booleans onto display strings."""

    def test_maps_true_false_and_none(self):
        fake = MagicMock()
        fake.states.return_value = {"a": True, "b": False, "c": None}
        with patch.dict(sys.modules, {}):
            with patch.object(nets_handler, "_brief_usb_batch",
                              nets_handler._brief_usb_batch):
                import lager.automation.usb_hub as hub_mod
                with patch.object(hub_mod, "states", fake.states):
                    out = nets_handler._brief_usb_batch(["a", "b", "c"])
        self.assertEqual(out, {"a": "enabled", "b": "disabled", "c": None})

    def test_dispatcher_failure_yields_all_none(self):
        import lager.automation.usb_hub as hub_mod
        with patch.object(hub_mod, "states",
                          side_effect=RuntimeError("SDK missing")):
            out = nets_handler._brief_usb_batch(["a", "b"])
        self.assertEqual(out, {"a": None, "b": None})


if __name__ == "__main__":
    unittest.main()
