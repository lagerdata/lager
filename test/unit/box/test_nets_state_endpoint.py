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

    def test_labjack_gpio_adc_dac_share_one_group(self):
        gpio = nets_handler._group_key(
            {"role": "gpio", "instrument": "LabJack_T7", "address": "ANY"})
        adc = nets_handler._group_key(
            {"role": "adc", "instrument": "LabJack_T7", "address": "ANY"})
        dac = nets_handler._group_key(
            {"role": "dac", "instrument": "LabJack_T7", "address": "ANY"})
        self.assertEqual(gpio, adc)
        self.assertEqual(adc, dac)

    def test_non_labjack_roles_keep_separate_groups(self):
        gpio = nets_handler._group_key(
            {"role": "gpio", "instrument": "RaspberryPi", "address": "ANY"})
        adc = nets_handler._group_key(
            {"role": "adc", "instrument": "RaspberryPi", "address": "ANY"})
        self.assertNotEqual(gpio, adc)


class ProbeGroupTests(unittest.TestCase):
    """_probe_group prefers the role's batch probe, and always answers."""

    def test_batch_probe_is_called_once_for_the_whole_group(self):
        calls = []

        def fake_batch(names, causes=None, codes=None, deadline=None):
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
        """Non-LabJack GPIO nets fall through to per-net probes."""
        seen = []

        def fake_probe(name):
            seen.append(name)
            return "HIGH (1)"

        rpi_gpio = {"name": "rpi1", "role": "gpio", "instrument": "RaspberryPi",
                     "address": "ANY", "pin": "GPIO17"}
        with patch.dict(nets_handler._BRIEF_PROBES, {"gpio": fake_probe}), \
             patch.dict(nets_handler._BATCH_PROBES, {}, clear=True):
            out = nets_handler._probe_group([rpi_gpio])

        self.assertEqual(seen, ["rpi1"])
        self.assertEqual(out[0]["state"], "HIGH (1)")

    def test_raising_batch_probe_yields_nulls_not_an_exception(self):
        def boom(names, causes=None, codes=None, deadline=None):
            raise RuntimeError("hub fell off the bus")

        recs = [r for r in SAVED_NETS if r["instrument"] == "Acroname_8Port"]
        with patch.dict(nets_handler._BATCH_PROBES, {"usb": boom}):
            out = nets_handler._probe_group(recs)

        self.assertEqual([e["state"] for e in out], [None, None, None])
        self.assertEqual([e["name"] for e in out], ["usb1", "usb2", "usb3"])

    def test_labjack_batch_called_for_cross_role_group(self):
        recs = [
            {"name": "g1", "role": "gpio", "instrument": "LabJack_T7",
             "address": "ANY", "pin": "EIO0"},
            {"name": "a1", "role": "adc", "instrument": "LabJack_T7",
             "address": "ANY", "pin": "0"},
        ]
        with patch.object(nets_handler, "_brief_labjack_batch",
                          return_value={"g1": "HIGH (1)", "a1": "3.3V"}) as mock:
            out = nets_handler._probe_group(recs)

        mock.assert_called_once_with(recs)
        self.assertEqual(out[0]["state"], "HIGH (1)")
        self.assertEqual(out[1]["state"], "3.3V")

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
                        {"usb": lambda names, causes=None, codes=None, deadline=None:
                         {n: "enabled" for n in names}}), \
             patch.object(nets_handler, "_brief_labjack_batch",
                          return_value={"gpi1": "LOW (0)"}):
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

        def wedged(names, causes=None, codes=None, deadline=None):
            # Blocks the way a driver blocked on a hub lock does. Released in
            # the finally below so the suite stays fast.
            release.wait(timeout=30)
            return {n: "enabled" for n in names}

        try:
            with patch.object(nets_handler, "_STATE_TIMEOUT", 0.4), \
                 patch.object(nets_handler.Net, "list_saved", return_value=SAVED_NETS), \
                 patch.dict(nets_handler._BATCH_PROBES, {"usb": wedged}), \
                 patch.object(nets_handler, "_brief_labjack_batch",
                             return_value={"gpi1": "LOW (0)"}):
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
        def boom(names, causes=None, codes=None, deadline=None):
            raise RuntimeError("no hub")

        with patch.object(nets_handler.Net, "list_saved", return_value=SAVED_NETS), \
             patch.dict(nets_handler._BATCH_PROBES, {"usb": boom}), \
             patch.object(nets_handler, "_brief_labjack_batch",
                         return_value={"gpi1": "HIGH (1)"}):
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

        def slow_batch(names, causes=None, codes=None, deadline=None):
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


class LabJackBatchProbeTests(unittest.TestCase):
    """_brief_labjack_batch delegates to hardware_service /labjack/batch_read.

    The batch probe sends one HTTP POST to hardware_service (port 8080) which
    owns the LabJack handle.  This avoids the cross-process USB contention
    that would occur if box_http_server (port 9000) opened its own LJM handle.
    """

    MIXED_BENCH = [
        {"name": "gpio1", "role": "gpio", "instrument": "LabJack_T7",
         "address": "ANY", "pin": "EIO0"},
        {"name": "gpio2", "role": "gpio", "instrument": "LabJack_T7",
         "address": "ANY", "pin": "CIO3"},
        {"name": "adc1", "role": "adc", "instrument": "LabJack_T7",
         "address": "ANY", "pin": "0"},
        {"name": "dac1", "role": "dac", "instrument": "LabJack_T7",
         "address": "ANY", "pin": "0"},
    ]

    def _mock_post(self, return_json, status_code=200):
        resp = MagicMock()
        resp.ok = (status_code == 200)
        resp.json.return_value = return_json
        return resp

    def test_sends_one_http_call_with_all_nets(self):
        expected = {"gpio1": "HIGH (1)", "gpio2": "LOW (0)",
                    "adc1": "3.3012V", "dac1": "1.5000V"}
        import requests as _req
        with patch.object(_req, "post",
                          return_value=self._mock_post(expected)) as mock:
            out = nets_handler._brief_labjack_batch(self.MIXED_BENCH)

        self.assertEqual(out, expected)
        mock.assert_called_once()
        payload = mock.call_args[1]["json"]["nets"]
        self.assertEqual(len(payload), 4)

    # A real bench T7 address -- the serial field is genuinely empty on this
    # hardware. Every other case here uses address "ANY", which is precisely
    # what hid the lock bug: with "ANY", _physical_device_id returns
    # "labjack:ANY" and a hardcoded key at the other end matched by luck.
    ADDRESSED_BENCH = [
        {"name": "gpio5", "role": "gpio", "instrument": "LabJack_T7",
         "address": "USB0::0x0CD5::0x0007::::INSTR", "pin": "EIO0"},
        {"name": "adc1", "role": "adc", "instrument": "LabJack_T7",
         "address": "USB0::0x0CD5::0x0007::::INSTR", "pin": "0"},
    ]

    def test_sends_the_device_id_invoke_locks_on(self):
        """hardware_service must be told which lock to take, because /invoke
        locks on _physical_device_id's output and the batch read has to
        contend on the same object or it can interleave I/O on one handle."""
        from lager.http_handlers.net_command import _physical_device_id

        rec = self.ADDRESSED_BENCH[0]
        expected = _physical_device_id(rec["role"], rec["instrument"], rec)

        import requests as _req
        with patch.object(_req, "post",
                          return_value=self._mock_post({})) as mock:
            nets_handler._brief_labjack_batch(self.ADDRESSED_BENCH)

        sent = mock.call_args[1]["json"].get("device_id")
        self.assertEqual(sent, expected)
        self.assertNotEqual(sent, "labjack:ANY",
                            "an addressed bench must not collapse to the ANY key")

    def test_http_failure_returns_all_none(self):
        import requests as _req
        with patch.object(_req, "post", side_effect=_req.ConnectionError("refused")):
            out = nets_handler._brief_labjack_batch(self.MIXED_BENCH)

        for rec in self.MIXED_BENCH:
            self.assertIsNone(out[rec["name"]])

    def test_non_200_returns_all_none(self):
        import requests as _req
        with patch.object(_req, "post",
                          return_value=self._mock_post({}, status_code=500)):
            out = nets_handler._brief_labjack_batch(self.MIXED_BENCH)

        for rec in self.MIXED_BENCH:
            self.assertIsNone(out[rec["name"]])

    def test_endpoint_routes_labjack_to_batch(self):
        """Full endpoint test: LabJack nets go through the batch path."""
        nets = self.MIXED_BENCH
        batch_result = {"gpio1": "HIGH (1)", "gpio2": "LOW (0)",
                        "adc1": "3.3012V", "dac1": "1.5000V"}

        with patch.object(nets_handler.Net, "list_saved", return_value=nets), \
             patch.object(nets_handler, "_brief_labjack_batch",
                          return_value=batch_result) as mock_batch:
            resp = _make_client().get('/nets/state')

        self.assertEqual(resp.status_code, 200)
        mock_batch.assert_called_once()
        by_name = {e["name"]: e["state"] for e in resp.get_json()}
        self.assertEqual(by_name["gpio1"], "HIGH (1)")
        self.assertEqual(by_name["gpio2"], "LOW (0)")
        self.assertEqual(by_name["adc1"], "3.3012V")
        self.assertEqual(by_name["dac1"], "1.5000V")

    def test_batch_failure_yields_nulls_not_500(self):
        nets = self.MIXED_BENCH
        with patch.object(nets_handler.Net, "list_saved", return_value=nets), \
             patch.object(nets_handler, "_brief_labjack_batch",
                          side_effect=RuntimeError("T7 gone")):
            resp = _make_client().get('/nets/state')

        self.assertEqual(resp.status_code, 200)
        for entry in resp.get_json():
            self.assertIsNone(entry["state"])


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

    def test_causes_are_forwarded_from_the_dispatcher(self):
        def fake_states(names, causes=None, codes=None, deadline=None):
            if causes is not None:
                causes["b"] = "DeviceNotFoundError: No Acroname hub detected"
            return {"a": True, "b": None}

        import lager.automation.usb_hub as hub_mod
        causes: dict = {}
        with patch.object(hub_mod, "states", fake_states):
            out = nets_handler._brief_usb_batch(["a", "b"], causes=causes)
        self.assertEqual(out, {"a": "enabled", "b": None})
        self.assertEqual(causes,
                         {"b": "DeviceNotFoundError: No Acroname hub detected"})

    def test_a_dispatcher_that_raises_fills_every_cause(self):
        """Nothing was reached, so every net gets the same reason -- this is
        the path where loading the net definitions or building a controller
        fails, below the dispatcher's own per-hub guard."""
        import lager.automation.usb_hub as hub_mod
        causes: dict = {}
        with patch.object(hub_mod, "states",
                          side_effect=RuntimeError("SDK missing")):
            out = nets_handler._brief_usb_batch(["a", "b"], causes=causes)
        self.assertEqual(out, {"a": None, "b": None})
        self.assertEqual(causes, {"a": "RuntimeError: SDK missing",
                                  "b": "RuntimeError: SDK missing"})


class HubUnreadableIsLoggedTests(unittest.TestCase):
    """A hub that will not open turns ALL of its nets null.

    Measured on a two-hub bench: the whole hub came back null while the other
    hub read fine, and the request was nowhere near its deadline. That path is
    ``dispatcher.states``' per-hub except, which used to discard the exception
    with nothing anywhere naming which hub or why -- so it was
    indistinguishable from a hub that answered null (issue #196).
    """

    def _dispatcher(self):
        from lager.automation.usb_hub import dispatcher
        return dispatcher

    def test_a_hub_that_will_not_open_is_logged_with_its_cause(self):
        dispatcher = self._dispatcher()
        nets = {
            "usb1": {"port": 0, "instrument": "Acroname_4Port", "address": "A"},
            "usb2": {"port": 1, "instrument": "Acroname_4Port", "address": "A"},
        }
        controller = MagicMock()
        controller._lock_key.return_value = "acroname::4port"
        controller.states.side_effect = RuntimeError("No Acroname hub detected on USB")

        with patch.object(dispatcher, "_load_net_definitions", return_value=nets), \
             patch.object(dispatcher, "_controller_for", return_value=controller), \
             self.assertLogs(dispatcher.logger, level="WARNING") as captured:
            out = dispatcher.states(["usb1", "usb2"])

        self.assertEqual(out, {"usb1": None, "usb2": None})
        logged = "\n".join(captured.output)
        self.assertIn("acroname::4port", logged, "the log must name which hub")
        self.assertIn("No Acroname hub detected on USB", logged,
                      "the log must carry the driver's own cause")

    def test_the_hub_open_cause_is_handed_to_the_caller(self):
        """The log only helps someone already on the box. The cause has to
        reach the caller too, or the CLI still cannot tell a hub that would not
        open from one that answered null."""
        dispatcher = self._dispatcher()
        nets = {
            "usb1": {"port": 0, "instrument": "Acroname_4Port", "address": "A"},
            "usb2": {"port": 1, "instrument": "Acroname_4Port", "address": "A"},
        }
        controller = MagicMock()
        controller._lock_key.return_value = "acroname::4port"
        controller.states.side_effect = RuntimeError(
            "No Acroname hub detected on USB with serial 0xBFABDDC4")

        causes: dict = {}
        with patch.object(dispatcher, "_load_net_definitions", return_value=nets), \
             patch.object(dispatcher, "_controller_for", return_value=controller), \
             self.assertLogs(dispatcher.logger, level="WARNING"):
            out = dispatcher.states(["usb1", "usb2"], causes=causes)

        self.assertEqual(out, {"usb1": None, "usb2": None})
        expected = ("RuntimeError: No Acroname hub detected on USB "
                    "with serial 0xBFABDDC4")
        self.assertEqual(causes, {"usb1": expected, "usb2": expected})

    def test_a_caller_that_does_not_ask_for_causes_is_unaffected(self):
        """Backward-compat pin: causes is additive and opt-in."""
        dispatcher = self._dispatcher()
        nets = {"usb1": {"port": 0, "instrument": "Acroname_4Port",
                         "address": "A"}}
        controller = MagicMock()
        controller._lock_key.return_value = "acroname::4port"
        controller.states.side_effect = RuntimeError("nope")

        with patch.object(dispatcher, "_load_net_definitions", return_value=nets), \
             patch.object(dispatcher, "_controller_for", return_value=controller), \
             self.assertLogs(dispatcher.logger, level="WARNING"):
            self.assertEqual(dispatcher.states(["usb1"]), {"usb1": None})

    def test_a_readable_hub_records_no_cause(self):
        """A cause means 'we know why this is null'. A hub that answered must
        not leave one behind."""
        dispatcher = self._dispatcher()
        nets = {"usb1": {"port": 0, "instrument": "Acroname_4Port",
                         "address": "A"}}
        controller = MagicMock()
        controller._lock_key.return_value = "acroname::4port"
        controller.states.return_value = {0: True}

        causes: dict = {}
        with patch.object(dispatcher, "_load_net_definitions", return_value=nets), \
             patch.object(dispatcher, "_controller_for", return_value=controller):
            out = dispatcher.states(["usb1"], causes=causes)
        self.assertEqual(out, {"usb1": True})
        self.assertEqual(causes, {})

    def test_one_dead_hub_does_not_stop_the_others_being_read(self):
        dispatcher = self._dispatcher()
        nets = {
            "usb1": {"port": 0, "instrument": "Acroname_4Port", "address": "A"},
            "usb5": {"port": 0, "instrument": "Acroname_8Port", "address": "B"},
        }
        dead, alive = MagicMock(), MagicMock()
        dead._lock_key.return_value = "dead"
        dead.states.side_effect = RuntimeError("hub absent")
        alive._lock_key.return_value = "alive"
        alive.states.return_value = {0: True}

        def pick(info):
            return dead if "4Port" in info["instrument"] else alive

        with patch.object(dispatcher, "_load_net_definitions", return_value=nets), \
             patch.object(dispatcher, "_controller_for", side_effect=pick), \
             self.assertLogs(dispatcher.logger, level="WARNING"):
            out = dispatcher.states(["usb1", "usb5"])

        self.assertIsNone(out["usb1"])
        self.assertTrue(out["usb5"])


class NullReasonTests(unittest.TestCase):
    """``state: null`` used to mean three unrelated things (issue #196).

    A net whose instrument never answered before the shared request deadline,
    a net whose probe failed, and a role that has no probe at all are all
    ``null`` -- and they need different remedies. Each now says which it is.
    """

    def setUp(self):
        self.client = _make_client()

    # ---- the entry builder -------------------------------------------------

    def test_a_net_with_a_state_carries_no_reason(self):
        entry = nets_handler._entry("usb1", "usb", "enabled", "unused")
        self.assertNotIn("reason", entry,
                         "reason present on an answered net reads as a problem")

    def test_a_null_net_carries_its_reason(self):
        entry = nets_handler._entry("usb1", "usb", None, "deadline")
        self.assertEqual(entry["reason"], "deadline")

    # ---- per-net probe -----------------------------------------------------

    def test_role_with_no_probe_says_so(self):
        out = nets_handler._probe_net_state(SAVED_NETS[5])  # uart
        self.assertIsNone(out["state"])
        self.assertEqual(out["reason"], nets_handler.REASON_NO_PROBE)

    def test_a_failing_probe_reports_unreadable_with_the_error(self):
        def boom(name):
            raise RuntimeError("hub fell off the bus")

        rec = {"name": "usb1", "role": "usb"}
        with patch.dict(nets_handler._BRIEF_PROBES, {"usb": boom}):
            out = nets_handler._probe_net_state(rec)

        self.assertIsNone(out["state"])
        self.assertTrue(out["reason"].startswith("unreadable:"), out["reason"])
        self.assertIn("hub fell off the bus", out["reason"])

    # ---- group probe -------------------------------------------------------

    def test_a_raising_batch_is_unreadable_not_deadline(self):
        """The distinction that matters: this instrument was reached and
        failed. Calling it 'deadline' would send someone after the timeout."""
        def boom(names, causes=None, codes=None, deadline=None):
            raise RuntimeError("no hub")

        recs = [r for r in SAVED_NETS if r["instrument"] == "Acroname_8Port"]
        with patch.dict(nets_handler._BATCH_PROBES, {"usb": boom}):
            out = nets_handler._probe_group(recs)

        for entry in out:
            self.assertTrue(entry["reason"].startswith("unreadable:"),
                            entry["reason"])
            self.assertNotEqual(entry["reason"], nets_handler.REASON_DEADLINE)

    def test_a_hub_that_would_not_open_says_why_not_no_value(self):
        """The gap this closes: the dispatcher knew the cause and logged it,
        but over HTTP a hub that would not open was labelled identically to a
        port that simply did not read. One CLI run should now name it."""
        cause = ("DeviceNotFoundError: No Acroname hub detected on USB with "
                 "serial 0xBFABDDC4 [discovery: findAllModules ok, 1 spec(s) "
                 "[0xAAAA0001], no serial match; sysfs: serial present on the "
                 "bus (24ff:0011) -- discovery did not return an enumerated hub]")

        def hub_will_not_open(names, causes=None, codes=None, deadline=None):
            if causes is not None:
                for n in names:
                    causes[n] = cause
            return {}

        recs = [r for r in SAVED_NETS if r["instrument"] == "Acroname_8Port"]
        with patch.dict(nets_handler._BATCH_PROBES, {"usb": hub_will_not_open}):
            out = nets_handler._probe_group(recs)

        for entry in out:
            self.assertIsNone(entry["state"])
            self.assertEqual(entry["reason"], f"unreadable: {cause}")
            self.assertNotEqual(entry["reason"],
                                "unreadable: no value from instrument")

    def test_a_port_that_did_not_read_still_says_no_value(self):
        """The generic wording stays for the case it actually describes: the
        hub answered, this port did not."""
        recs = [r for r in SAVED_NETS if r["instrument"] == "Acroname_8Port"]
        names = [r["name"] for r in recs]

        def partial_read(n, causes=None, codes=None, deadline=None):
            return {names[0]: "enabled", names[2]: "disabled"}

        with patch.dict(nets_handler._BATCH_PROBES, {"usb": partial_read}):
            out = nets_handler._probe_group(recs)

        by_name = {e["name"]: e for e in out}
        self.assertEqual(by_name[names[1]]["reason"],
                         "unreadable: no value from instrument")
        # A net that read carries no reason key at all.
        self.assertNotIn("reason", by_name[names[0]])

    def test_a_cause_is_attached_only_to_the_net_it_names(self):
        """One unreadable hub on a bench must not relabel the nets that read."""
        recs = [r for r in SAVED_NETS if r["instrument"] == "Acroname_8Port"]
        names = [r["name"] for r in recs]

        def mixed(n, causes=None, codes=None, deadline=None):
            if causes is not None:
                causes[names[1]] = "DeviceNotFoundError: nope"
            return {names[0]: "enabled", names[2]: "disabled"}

        with patch.dict(nets_handler._BATCH_PROBES, {"usb": mixed}):
            out = nets_handler._probe_group(recs)

        by_name = {e["name"]: e for e in out}
        self.assertEqual(by_name[names[1]]["reason"],
                         "unreadable: DeviceNotFoundError: nope")
        self.assertEqual(by_name[names[0]]["state"], "enabled")
        self.assertNotIn("reason", by_name[names[0]])

    def test_a_batch_that_omits_one_net_marks_only_that_net(self):
        """The Acroname per-port case: the session completed, one port did
        not read. A partial result is the shape a deadline miss cannot make."""
        recs = [r for r in SAVED_NETS if r["instrument"] == "Acroname_8Port"]
        names = [r["name"] for r in recs]
        partial = {names[0]: "enabled", names[1]: None, names[2]: "disabled"}
        with patch.dict(nets_handler._BATCH_PROBES,
                        {"usb": lambda n, causes=None, codes=None, deadline=None: partial}):
            out = nets_handler._probe_group(recs)

        by_name = {e["name"]: e for e in out}
        self.assertEqual(by_name[names[0]]["state"], "enabled")
        self.assertNotIn("reason", by_name[names[0]])
        self.assertIsNone(by_name[names[1]]["state"])
        self.assertTrue(by_name[names[1]]["reason"].startswith("unreadable:"))

    # ---- the deadline ------------------------------------------------------

    def test_a_net_the_deadline_cut_off_says_deadline(self):
        release = threading.Event()

        def wedged(names, causes=None, codes=None, deadline=None):
            release.wait(timeout=30)
            return {n: "enabled" for n in names}

        try:
            with patch.object(nets_handler, "_STATE_TIMEOUT", 0.4), \
                 patch.object(nets_handler.Net, "list_saved", return_value=SAVED_NETS), \
                 patch.dict(nets_handler._BATCH_PROBES, {"usb": wedged}), \
                 patch.object(nets_handler, "_brief_labjack_batch",
                              return_value={"gpi1": "LOW (0)"}):
                resp = self.client.get('/nets/state')
        finally:
            release.set()

        self.assertEqual(resp.status_code, 200)
        by_name = {e["name"]: e for e in resp.get_json()}
        self.assertEqual(by_name["usb1"]["reason"], nets_handler.REASON_DEADLINE)
        # An instrument that did answer is untouched, and says nothing.
        self.assertEqual(by_name["gpi1"]["state"], "LOW (0)")
        self.assertNotIn("reason", by_name["gpi1"])


class ReasonCodeTests(unittest.TestCase):
    """The machine-readable half of a null answer."""

    def setUp(self):
        self.client = _make_client()

    def _state(self, nets, batch):
        with patch.object(nets_handler.Net, "list_saved", return_value=nets), \
             patch.dict(nets_handler._BATCH_PROBES, {"usb": batch}):
            resp = self.client.get("/nets/state")
        self.assertEqual(resp.status_code, 200)
        return {e["name"]: e for e in resp.get_json()}

    def test_codes_are_forwarded_from_the_dispatcher(self):
        def fake_states(names, causes=None, codes=None, deadline=None):
            if causes is not None:
                causes["b"] = "DeviceNotFoundError: nope"
            if codes is not None:
                codes["b"] = "hub-unreachable"
            return {"a": True, "b": None}

        import lager.automation.usb_hub as hub_mod
        causes: dict = {}
        codes: dict = {}
        with patch.object(hub_mod, "states", fake_states):
            out = nets_handler._brief_usb_batch(["a", "b"], causes=causes,
                                                codes=codes)
        self.assertEqual(out, {"a": "enabled", "b": None})
        self.assertEqual(codes, {"b": "hub-unreachable"})

    def test_a_caller_that_asks_for_no_codes_is_unaffected(self):
        def fake_states(names, causes=None, codes=None, deadline=None):
            self.assertIsNone(codes)
            return {"a": True}

        import lager.automation.usb_hub as hub_mod
        with patch.object(hub_mod, "states", fake_states):
            out = nets_handler._brief_usb_batch(["a"])
        self.assertEqual(out, {"a": "enabled"})

    def test_a_null_net_carries_both_a_reason_and_a_code(self):
        def batch(names, causes=None, codes=None, deadline=None):
            for n in names:
                if causes is not None:
                    causes[n] = "DeviceNotFoundError: hub will not answer"
                if codes is not None:
                    codes[n] = "hub-unreachable"
            return {n: None for n in names}

        by_name = self._state(
            [{"name": "usb1", "role": "usb"}], batch)
        self.assertIsNone(by_name["usb1"]["state"])
        self.assertIn("hub will not answer", by_name["usb1"]["reason"])
        self.assertEqual("hub-unreachable", by_name["usb1"]["reason_code"])

    def test_a_net_with_a_state_carries_no_code(self):
        def batch(names, causes=None, codes=None, deadline=None):
            return {n: "enabled" for n in names}

        by_name = self._state([{"name": "usb1", "role": "usb"}], batch)
        self.assertEqual("enabled", by_name["usb1"]["state"])
        self.assertNotIn("reason_code", by_name["usb1"])
        self.assertNotIn("reason", by_name["usb1"])

    def test_a_reason_with_no_classification_omits_the_code_entirely(self):
        """Absent, not null. An older CLI's .get("reason_code") must see
        nothing rather than something falsy it then has to special-case."""
        def batch(names, causes=None, codes=None, deadline=None):
            return {n: None for n in names}

        by_name = self._state([{"name": "usb1", "role": "usb"}], batch)
        self.assertIn("reason", by_name["usb1"])
        self.assertNotIn("reason_code", by_name["usb1"])

    def test_the_reason_string_alone_names_the_remedy(self):
        """THE COMPATIBILITY CONTRACT, as an executable assertion.

        An older CLI never reads reason_code. So the remedy has to survive in
        the human reason on its own -- if a future change moves the meaning
        into the code, every older CLI silently starts printing less than it
        used to, and this test is what stops that landing quietly.
        """
        def batch(names, causes=None, codes=None, deadline=None):
            for n in names:
                if causes is not None:
                    causes[n] = ("DeviceNotFoundError: No Acroname hub detected "
                                 "on USB with serial 0xE6BACCD5: the hub is on "
                                 "the USB bus but is not answering BrainStem; "
                                 "check hub power and the upstream cable")
                if codes is not None:
                    codes[n] = "hub-unreachable"
            return {n: None for n in names}

        by_name = self._state([{"name": "usb1", "role": "usb"}], batch)
        reason = by_name["usb1"]["reason"]
        self.assertIn("check hub power and the upstream cable", reason)


class HubBudgetPlumbingTests(unittest.TestCase):
    """/nets/state hands its budget to the USB batch, and a hub that budget
    skipped answers with its own reason -- never the request's "deadline"
    (issue #205)."""

    def setUp(self):
        self.client = _make_client()

    def test_the_usb_batch_receives_the_request_deadline(self):
        seen = {}

        def fake_states(names, causes=None, codes=None, deadline=None):
            seen["deadline"] = deadline
            return {n: True for n in names}

        import lager.automation.usb_hub as hub_mod
        with patch.object(nets_handler.Net, "list_saved",
                          return_value=[{"name": "usb1", "role": "usb"}]), \
             patch.object(hub_mod, "states", fake_states):
            before = time.monotonic()
            resp = self.client.get("/nets/state")
        self.assertEqual(resp.status_code, 200)
        deadline = seen["deadline"]
        self.assertIsNotNone(deadline, "the batch probe got no deadline")
        # The absolute monotonic form of _STATE_TIMEOUT, measured from the
        # request -- not a relative number of seconds.
        self.assertGreater(deadline, before)
        self.assertLessEqual(deadline,
                             before + nets_handler._STATE_TIMEOUT + 1.0)

    def test_a_budget_skipped_hub_does_not_read_as_deadline(self):
        def fake_states(names, causes=None, codes=None, deadline=None):
            # The dispatcher's skip shape: the net maps to None and the
            # attribution dicts carry the skip -- what dispatcher.states
            # produces when the budget cannot cover a hub.
            if causes is not None:
                causes["usb2"] = ("not probed: slower instruments consumed "
                                  "the state budget")
            if codes is not None:
                codes["usb2"] = "hub-skipped"
            return {"usb1": True, "usb2": None}

        import lager.automation.usb_hub as hub_mod
        with patch.object(nets_handler.Net, "list_saved",
                          return_value=[{"name": "usb1", "role": "usb"},
                                        {"name": "usb2", "role": "usb"}]), \
             patch.object(hub_mod, "states", fake_states):
            resp = self.client.get("/nets/state")
        self.assertEqual(resp.status_code, 200)
        by_name = {e["name"]: e for e in resp.get_json()}
        self.assertEqual(by_name["usb1"]["state"], "enabled")
        self.assertIsNone(by_name["usb2"]["state"])
        self.assertNotEqual(by_name["usb2"]["reason"], "deadline")
        self.assertIn("not probed", by_name["usb2"]["reason"])
        self.assertEqual(by_name["usb2"]["reason_code"], "hub-skipped")


if __name__ == "__main__":
    unittest.main()
