# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0
"""
Unit tests for the /nets/state stream and its fail-fast paths
(box/lager/http_handlers/nets_handler.py).

``GET /nets/state?stream=1`` answers ndjson: one ``states`` line per
instrument as it answers, and a final ``done`` line carrying the nets the
request deadline cut off. Other clients build against that line format, so it
is pinned here -- including that lines really leave the server as they are
written, which is checked on arrival over a real socket, not on final content.

The fail-fast paths apply to the plain array too: an instrument that is not on
the USB bus is answered without being probed, one that does not answer inside
its own budget is cut short on its own, and one that just failed is answered
from a short cooldown rather than probed again behind a lock a leftover probe
still holds.
"""

import http.client
import json
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
from werkzeug.serving import make_server  # noqa: E402

from lager.http_handlers import nets_handler  # noqa: E402
from lager.util import self_restart  # noqa: E402


def _make_app():
    app = Flask(__name__)
    nets_handler.register_nets_routes(app)
    return app


def _rec(name, role="gpio", instrument="FakeIO", address=None):
    return {"name": name, "role": role, "instrument": instrument,
            "address": address or f"TCPIP0::{name}::inst0::INSTR"}


def _lines(body):
    return [json.loads(line) for line in body.decode().splitlines() if line]


class _SweepTestCase(unittest.TestCase):
    """Isolates every test from the module-level cooldown and from the host's
    USB bus, and frees any probe a test left blocked."""

    def setUp(self):
        with nets_handler._cooldown_lock:
            nets_handler._cooldown.clear()
        self.addCleanup(nets_handler._cooldown.clear)
        # The host's real sysfs must not decide what a test sees.
        p = patch.object(nets_handler, "_usb_presence", return_value=None)
        p.start()
        self.addCleanup(p.stop)
        p = patch.object(nets_handler, "_fetch_last_ok", return_value={})
        self.last_ok = p.start()
        self.addCleanup(p.stop)
        self.release = threading.Event()
        self.addCleanup(self.release.set)
        self.client = _make_app().test_client()

    def _stream(self):
        resp = self.client.get('/nets/state?stream=1')
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.mimetype, "application/x-ndjson")
        return _lines(resp.data)


class StreamFormatTests(_SweepTestCase):

    def test_lines_are_typed_and_done_is_last_exactly_once(self):
        nets = [_rec("a"), _rec("b"), _rec("u", role="uart")]
        with patch.object(nets_handler.Net, "list_saved", return_value=nets), \
             patch.dict(nets_handler._BRIEF_PROBES,
                        {"gpio": lambda n: "HIGH (1)"}):
            lines = self._stream()

        self.assertEqual([l["type"] for l in lines[:-1]],
                         ["states"] * (len(lines) - 1))
        self.assertEqual(lines[-1]["type"], "done")
        self.assertEqual([l["type"] for l in lines].count("done"), 1)
        self.assertIsInstance(lines[-1]["elapsed_ms"], int)
        self.assertEqual(lines[-1]["entries"], [])

    def test_every_net_appears_exactly_once_with_the_array_shape(self):
        nets = [_rec("a"), _rec("b"), _rec("u", role="uart")]
        with patch.object(nets_handler.Net, "list_saved", return_value=nets), \
             patch.dict(nets_handler._BRIEF_PROBES,
                        {"gpio": lambda n: "HIGH (1)"}):
            lines = self._stream()
            array = self.client.get('/nets/state').get_json()

        streamed = [e for l in lines for e in l["entries"]]
        self.assertEqual(sorted(e["name"] for e in streamed), ["a", "b", "u"])
        by_name = {e["name"]: e for e in streamed}
        # Same entries as the array, reordered into saved order.
        self.assertEqual([by_name[r["name"]] for r in nets], array)

    def test_nets_with_no_probe_arrive_in_the_first_line(self):
        nets = [_rec("a"), _rec("u", role="uart")]
        with patch.object(nets_handler.Net, "list_saved", return_value=nets), \
             patch.dict(nets_handler._BRIEF_PROBES,
                        {"gpio": lambda n: "HIGH (1)"}):
            lines = self._stream()

        self.assertEqual(lines[0]["entries"],
                         [{"name": "u", "role": "uart", "state": None,
                           "reason": nets_handler.REASON_NO_PROBE}])

    def test_no_saved_nets_is_a_single_done_line(self):
        with patch.object(nets_handler.Net, "list_saved", return_value=[]):
            lines = self._stream()
        self.assertEqual(len(lines), 1)
        self.assertEqual(lines[0]["type"], "done")
        self.assertEqual(lines[0]["entries"], [])

    def test_done_lists_exactly_the_nets_the_deadline_cut_off(self):
        def probe(name):
            if name.startswith("slow"):
                self.release.wait(30)
            return "LOW (0)"

        nets = [_rec("fast"), _rec("slow1"), _rec("slow2")]
        with patch.object(nets_handler, "_STATE_TIMEOUT", 0.5), \
             patch.object(nets_handler.Net, "list_saved", return_value=nets), \
             patch.dict(nets_handler._BRIEF_PROBES, {"gpio": probe}):
            lines = self._stream()

        done = lines[-1]
        self.assertEqual(done["type"], "done")
        self.assertEqual(
            done["entries"],
            [{"name": n, "role": "gpio", "state": None,
              "reason": nets_handler.REASON_DEADLINE}
             for n in ("slow1", "slow2")])
        answered = [e for l in lines[:-1] for e in l["entries"]]
        self.assertEqual(answered, [{"name": "fast", "role": "gpio",
                                     "state": "LOW (0)"}])

    def test_the_array_is_unchanged_for_a_healthy_bench(self):
        nets = [_rec("g1"), _rec("u", role="uart"), _rec("g2")]
        with patch.object(nets_handler.Net, "list_saved", return_value=nets), \
             patch.dict(nets_handler._BRIEF_PROBES,
                        {"gpio": lambda n: "HIGH (1)" if n == "g1" else "LOW (0)"}):
            resp = self.client.get('/nets/state')

        self.assertEqual(resp.mimetype, "application/json")
        self.assertEqual(resp.get_json(), [
            {"name": "g1", "role": "gpio", "state": "HIGH (1)"},
            {"name": "u", "role": "uart", "state": None,
             "reason": "no probe for role"},
            {"name": "g2", "role": "gpio", "state": "LOW (0)"},
        ])


class StreamArrivalTests(_SweepTestCase):
    """Lines must leave the server as they are written. Checked over a real
    socket against Werkzeug's threaded server -- the server port 9000 runs
    (socketio.run with async_mode='threading') -- because the Flask test
    client would pass even if the server buffered the whole body."""

    def setUp(self):
        super().setUp()
        self.server = make_server("127.0.0.1", 0, _make_app(), threaded=True)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.shutdown)

    def test_a_fast_instrument_arrives_while_a_slow_one_is_still_probing(self):
        slow_finished = threading.Event()

        def probe(name):
            if name == "slow":
                self.release.wait(30)
                slow_finished.set()
            return "HIGH (1)"

        nets = [_rec("slow"), _rec("fast")]
        with patch.object(nets_handler.Net, "list_saved", return_value=nets), \
             patch.dict(nets_handler._BRIEF_PROBES, {"gpio": probe}):
            conn = http.client.HTTPConnection("127.0.0.1",
                                              self.server.server_port,
                                              timeout=10)
            conn.request("GET", "/nets/state?stream=1")
            resp = conn.getresponse()
            first = json.loads(resp.readline())
            # The slow probe is still blocked when the fast line arrives.
            self.assertFalse(slow_finished.is_set())
            self.assertEqual([e["name"] for e in first["entries"]], ["fast"])

            self.release.set()
            rest = [json.loads(l) for l in resp.read().decode().splitlines()]
            conn.close()

        self.assertEqual([e["name"] for e in rest[0]["entries"]], ["slow"])
        self.assertEqual(rest[-1]["type"], "done")


class StreamDisconnectTests(_SweepTestCase):

    def test_closing_the_stream_cancels_queued_probes(self):
        started = []

        def probe(name):
            started.append(name)
            self.release.wait(30)
            return "HIGH (1)"

        # Ten instruments behind an 8-worker cap: two are queued.
        nets = [_rec(f"g{i}") for i in range(10)] + [_rec("u", role="uart")]
        shutdowns = []
        real_pool = nets_handler.ThreadPoolExecutor

        class SpyPool(real_pool):
            def shutdown(self, wait=True, *, cancel_futures=False):
                shutdowns.append((wait, cancel_futures))
                return super().shutdown(wait=wait, cancel_futures=cancel_futures)

        with patch.object(nets_handler, "ThreadPoolExecutor", SpyPool), \
             patch.object(nets_handler.Net, "list_saved", return_value=nets), \
             patch.dict(nets_handler._BRIEF_PROBES, {"gpio": probe}):
            resp = self.client.get('/nets/state?stream=1', buffered=False)
            chunks = iter(resp.response)
            first = json.loads(next(chunks))
            self.assertEqual(first["entries"][0]["name"], "u")
            resp.close()  # what Werkzeug does when the client has gone

            self.assertEqual(shutdowns, [(False, True)])
            self.release.set()
            time.sleep(0.3)

        self.assertEqual(len(started), 8,
                         "a probe queued behind the cap ran after the client left")


class AbsentInstrumentTests(_SweepTestCase):

    SUPPLY = {"name": "vdd", "role": "power-supply", "instrument": "Rigol_DP821",
              "address": "USB0::0x1AB1::0x0E11::DP8X0001::INSTR", "channel": 1}

    def _bus(self, ids):
        return patch.object(nets_handler, "_usb_presence", return_value=ids)

    def test_an_unplugged_instrument_is_answered_without_a_probe(self):
        probe = MagicMock(return_value=("CH1/on/3.30V/0.100A", True))
        bus = {(0x1AB1, 0x0E11): ["DP8OTHER"]}  # a different unit is present
        for path in ('/nets/state', '/nets/state?stream=1'):
            with self.subTest(path=path), self._bus(bus), \
                 patch.object(nets_handler.Net, "list_saved",
                              return_value=[self.SUPPLY]), \
                 patch.dict(nets_handler._BRIEF_PROBES, {"power-supply": probe}), \
                 patch.dict(nets_handler._BATCH_PROBES, {}, clear=True):
                start = time.monotonic()
                resp = self.client.get(path)
                elapsed = time.monotonic() - start

            entries = (resp.get_json() if 'stream' not in path
                       else [e for l in _lines(resp.data) for e in l["entries"]])
            self.assertLess(elapsed, 1.0)
            self.assertEqual(len(entries), 1)
            entry = entries[0]
            self.assertIsNone(entry["state"])
            self.assertNotIn("enabled", entry)
            self.assertEqual(entry["reason_code"], nets_handler.CODE_ABSENT)
            self.assertIn("not connected", entry["reason"])
            self.assertNotIn("off", entry["reason"])
        probe.assert_not_called()

    def test_a_present_instrument_is_probed(self):
        probe = MagicMock(return_value=("CH1/on/3.30V/0.100A", True))
        with self._bus({(0x1AB1, 0x0E11): ["DP8X0001"]}), \
             patch.object(nets_handler.Net, "list_saved",
                          return_value=[self.SUPPLY]), \
             patch.dict(nets_handler._BRIEF_PROBES, {"power-supply": probe}), \
                 patch.dict(nets_handler._BATCH_PROBES, {}, clear=True):
            entry = self.client.get('/nets/state').get_json()[0]
        probe.assert_called_once()
        self.assertEqual(entry["state"], "CH1/on/3.30V/0.100A")

    def test_an_address_that_cannot_be_checked_is_still_probed(self):
        probe = MagicMock(return_value="HIGH (1)")
        with self._bus({(0x1AB1, 0x0E11): ["X"]}), \
             patch.object(nets_handler.Net, "list_saved",
                          return_value=[_rec("g", address="ppk2:ABC123")]), \
             patch.dict(nets_handler._BRIEF_PROBES, {"gpio": probe}):
            entry = self.client.get('/nets/state').get_json()[0]
        probe.assert_called_once()
        self.assertEqual(entry["state"], "HIGH (1)")

    def test_an_empty_bus_listing_is_unknown_not_absent(self):
        """A container with no USB view lists nothing; that must not read as
        every instrument on the bench being unplugged."""
        with patch.object(self_restart, "enumerated_usb_ids", return_value={}):
            self.assertIsNone(_real_usb_presence())

    def test_usb_hub_nets_are_left_to_the_hub_dispatcher(self):
        hub = {"name": "p1", "role": "usb", "instrument": "Acroname_8Port",
               "address": "USB0::0x24FF::0x0013::GONE::INSTR", "pin": "0"}
        def fake(names, causes=None, codes=None, deadline=None):
            return {n: "enabled" for n in names}

        with self._bus({(0x1AB1, 0x0E11): ["X"]}), \
             patch.object(nets_handler.Net, "list_saved", return_value=[hub]), \
             patch.dict(nets_handler._BATCH_PROBES, {"usb": fake}):
            entry = self.client.get('/nets/state').get_json()[0]
        self.assertEqual(entry["state"], "enabled")


# The real function, captured before _SweepTestCase patches it per test.
_real_usb_presence = nets_handler._usb_presence


class UsbAddressEnumeratedTests(unittest.TestCase):

    def test_serial_match(self):
        ids = {(0x05E6, 0x2281): ["4518305"]}
        addr = "USB0::0x05E6::0x2281::4518305::INSTR"
        self.assertTrue(self_restart.usb_address_enumerated(addr, ids))
        self.assertFalse(self_restart.usb_address_enumerated(
            "USB0::0x05E6::0x2281::9999999::INSTR", ids))
        self.assertFalse(self_restart.usb_address_enumerated(
            "USB0::0x1AB1::0x0E11::DP8::INSTR", ids))

    def test_unreadable_serial_matches_on_ids(self):
        ids = {(0x05E6, 0x2281): [None]}
        self.assertTrue(self_restart.usb_address_enumerated(
            "USB0::0x05E6::0x2281::anything::INSTR", ids))

    def test_topology_slot_matches_on_ids(self):
        ids = {(0x2230, 0x5411): ["0000"]}
        addr = "USB0::0x2230::0x5411::port-1-1.4::INSTR"
        self.assertTrue(self_restart.usb_address_enumerated(addr, ids))
        # The restart gate keeps its long-standing answer.
        self.assertFalse(self_restart.usb_address_enumerated(
            addr, ids, port_slots=False))

    def test_unknown_cases(self):
        self.assertIsNone(self_restart.usb_address_enumerated(
            "USB0::0x05E6::0x2281::X::INSTR", None))
        self.assertIsNone(self_restart.usb_address_enumerated(
            "TCPIP0::10.0.0.1::inst0::INSTR", {}))


class GroupBudgetTests(_SweepTestCase):

    def test_a_slow_instrument_is_cut_short_on_its_own_budget(self):
        def probe(name):
            if name == "stuck":
                self.release.wait(30)
            return "HIGH (1)"

        nets = [_rec("stuck"), _rec("ok")]
        with patch.object(nets_handler, "_GROUP_BUDGET_S", 0.3), \
             patch.object(nets_handler.Net, "list_saved", return_value=nets), \
             patch.dict(nets_handler._BRIEF_PROBES, {"gpio": probe}):
            start = time.monotonic()
            body = self.client.get('/nets/state').get_json()
            elapsed = time.monotonic() - start

        self.assertLess(elapsed, 2.0, "waited for the request deadline")
        stuck, ok = body
        self.assertIsNone(stuck["state"])
        self.assertEqual(stuck["reason_code"], nets_handler.CODE_TIMEOUT)
        self.assertTrue(stuck["reason"].startswith("timed out:"), stuck)
        self.assertNotEqual(stuck["reason"], nets_handler.REASON_DEADLINE)
        self.assertEqual(ok["state"], "HIGH (1)")

    def test_a_multi_net_instrument_keeps_the_nets_it_finished(self):
        def probe(name):
            if name == "ch2":
                self.release.wait(30)
            return "LOW (0)"

        nets = [_rec("ch1", address="TCPIP0::sup::inst0::INSTR"),
                _rec("ch2", address="TCPIP0::sup::inst0::INSTR")]
        with patch.object(nets_handler, "_GROUP_BUDGET_S", 0.2), \
             patch.object(nets_handler, "_GROUP_BUDGET_PER_NET_S", 0.1), \
             patch.object(nets_handler.Net, "list_saved", return_value=nets), \
             patch.dict(nets_handler._BRIEF_PROBES, {"gpio": probe}):
            body = self.client.get('/nets/state').get_json()

        self.assertEqual(body[0]["state"], "LOW (0)")
        self.assertEqual(body[1]["reason_code"], nets_handler.CODE_TIMEOUT)

    def test_budget_grows_per_net_on_the_one_at_a_time_path(self):
        three = [_rec(f"c{i}", role="power-supply") for i in range(3)]
        self.assertEqual(nets_handler._group_budget(three),
                         nets_handler._GROUP_BUDGET_S
                         + 2 * nets_handler._GROUP_BUDGET_PER_NET_S)
        hubs = [_rec(f"p{i}", role="usb") for i in range(3)]
        self.assertEqual(nets_handler._group_budget(hubs),
                         nets_handler._GROUP_BUDGET_S)


class CooldownTests(_SweepTestCase):

    def _wedge(self, calls):
        def probe(name):
            calls.append(name)
            self.release.wait(30)
            return "HIGH (1)"
        return probe

    def _sweep_once(self, probe):
        with patch.object(nets_handler, "_GROUP_BUDGET_S", 0.2), \
             patch.object(nets_handler.Net, "list_saved",
                          return_value=[_rec("g")]), \
             patch.dict(nets_handler._BRIEF_PROBES, {"gpio": probe}):
            return self.client.get('/nets/state').get_json()[0]

    def test_a_timed_out_instrument_is_not_probed_again_inside_the_window(self):
        calls = []
        first = self._sweep_once(self._wedge(calls))
        self.assertEqual(first["reason_code"], nets_handler.CODE_TIMEOUT)

        start = time.monotonic()
        second = self._sweep_once(self._wedge(calls))
        self.assertLess(time.monotonic() - start, 0.5)
        self.assertEqual(calls, ["g"], "probed again during the cooldown")
        self.assertIsNone(second["state"])
        self.assertEqual(second["reason_code"], nets_handler.CODE_COOLDOWN)
        self.assertIn(first["reason"], second["reason"])
        self.assertTrue(second["reason"].startswith("not probed:"))

    def test_the_cooldown_ends_after_its_window(self):
        self._sweep_once(self._wedge([]))
        later = time.monotonic() + nets_handler._COOLDOWN_S + 1
        with patch.object(nets_handler, "_cooldown_clock", return_value=later):
            entry = self._sweep_once(lambda n: "LOW (0)")
        self.assertEqual(entry["state"], "LOW (0)")
        self.assertEqual(nets_handler._cooldown, {})

    def test_a_success_through_hardware_service_ends_it_early(self):
        rec = _rec("g")
        self._sweep_once(self._wedge([]))
        time.sleep(0.05)
        # Any caller completed an operation on this instrument just now.
        self.last_ok.return_value = {rec["address"]: 0.001}
        entry = self._sweep_once(lambda n: "LOW (0)")
        self.assertEqual(entry["state"], "LOW (0)")

    def test_an_older_success_does_not_end_it(self):
        rec = _rec("g")
        self._sweep_once(self._wedge([]))
        self.last_ok.return_value = {rec["address"]: 3600.0}
        entry = self._sweep_once(lambda n: "LOW (0)")
        self.assertEqual(entry["reason_code"], nets_handler.CODE_COOLDOWN)

    def test_a_successful_probe_clears_it(self):
        key = nets_handler._group_key(_rec("g"))
        nets_handler._cooldown_set(key, [_rec("g")], "timed out: x")
        nets_handler._settle_cooldown(
            key, [_rec("g")], [{"name": "g", "role": "gpio", "state": "HIGH (1)"}])
        self.assertNotIn(key, nets_handler._cooldown)

    def test_a_busy_instrument_goes_into_cooldown(self):
        key = nets_handler._group_key(_rec("g"))
        nets_handler._settle_cooldown(key, [_rec("g")], [
            {"name": "g", "role": "gpio", "state": None,
             "reason": "unreadable: device-busy: x",
             "reason_code": nets_handler.CODE_BUSY}])
        self.assertIn(key, nets_handler._cooldown)

    def test_an_ordinary_unreadable_answer_does_not(self):
        key = nets_handler._group_key(_rec("g"))
        nets_handler._settle_cooldown(key, [_rec("g")], [
            {"name": "g", "role": "gpio", "state": None,
             "reason": "unreadable: parse error"}])
        self.assertNotIn(key, nets_handler._cooldown)


class DeviceFailureReasonTests(_SweepTestCase):
    """The per-net probes swallow every error; under a budget the sweep still
    says which hardware_service failure emptied the answer."""

    def test_a_busy_device_is_reported_busy_not_off(self):
        from lager.nets import device as device_mod

        def probe(name):
            try:
                device_mod.Device("fake", {"address": "x"}).read_state()
            except Exception:
                return None
            return ("CH1/on/1.00V/0.100A", True)

        busy = MagicMock(ok=False, status_code=503)
        busy.json.return_value = {"error": "device-busy: fake.read_state: busy"}
        with patch.object(device_mod._session, "post", return_value=busy) as post, \
             patch.object(nets_handler.Net, "list_saved",
                          return_value=[_rec("v", role="power-supply")]), \
             patch.dict(nets_handler._BRIEF_PROBES, {"power-supply": probe}), \
                 patch.dict(nets_handler._BATCH_PROBES, {}, clear=True):
            entry = self.client.get('/nets/state').get_json()[0]

        self.assertIsNone(entry["state"])
        self.assertNotIn("enabled", entry)
        self.assertEqual(entry["reason_code"], nets_handler.CODE_BUSY)
        sent = json.loads(post.call_args[1]["data"])
        self.assertLessEqual(sent["lock_timeout_s"], nets_handler._GROUP_BUDGET_S)
        self.assertLessEqual(post.call_args[1]["timeout"],
                             nets_handler._GROUP_BUDGET_S)

    def test_without_a_budget_device_calls_are_unchanged(self):
        from lager.nets import device as device_mod

        ok = MagicMock(ok=True, content=b'{"a": 1}')
        with patch.object(device_mod._session, "post", return_value=ok) as post:
            device_mod.Device("fake", {"address": "x"}).read_state()
        self.assertNotIn("lock_timeout_s", json.loads(post.call_args[1]["data"]))
        self.assertEqual(post.call_args[1]["timeout"],
                         device_mod.Device.DEFAULT_TIMEOUT)


class SupplyBatchTests(_SweepTestCase):
    """One supply, every channel, one hardware_service call."""

    NETS = [
        {"name": f"v{ch}", "role": "power-supply", "instrument": "Rigol_DP821",
         "address": "USB0::0x1AB1::0x0E11::DP8X::INSTR", "channel": ch}
        for ch in (1, 2, 3)
    ]

    def _resolve(self, name, role, error_class):
        ch = int(name[1:])
        return ("rigol_dp800", {"name": name, "channel": ch,
                                "address": self.NETS[0]["address"],
                                "instrument": "Rigol_DP821"}, ch)

    def test_three_channels_are_one_invoke(self):
        from lager.nets import device as device_mod

        ok = MagicMock(ok=True, content=json.dumps({
            "1": {"enabled": True, "voltage": 3.3, "current": 0.1},
            "2": {"enabled": False, "voltage": 0.0, "current": 0.0},
            "3": {"enabled": None, "voltage": None, "current": None},
        }).encode())
        with patch("lager.dispatchers.helpers.resolve_net_proxy",
                   side_effect=self._resolve), \
             patch.object(device_mod._session, "post", return_value=ok) as post, \
             patch.object(nets_handler.Net, "list_saved", return_value=self.NETS):
            body = self.client.get('/nets/state').get_json()

        post.assert_called_once()
        sent = json.loads(post.call_args[1]["data"])
        self.assertEqual(sent["function"], "get_monitor_states")
        self.assertEqual(sent["args"], [[1, 2, 3]])
        self.assertEqual(body, [
            {"name": "v1", "role": "power-supply",
             "state": "CH1/on/3.30V/0.100A", "enabled": True},
            {"name": "v2", "role": "power-supply",
             "state": "CH2/off/0.00V/0.000A", "enabled": False},
            {"name": "v3", "role": "power-supply", "state": "CH3/?/?V/?A"},
        ])

    def test_falls_back_per_channel_without_the_method(self):
        from lager.nets import device as device_mod

        missing = MagicMock(ok=False, status_code=404)
        missing.json.return_value = {"error": "Function not found: get_monitor_states"}
        one = MagicMock(ok=True, content=json.dumps(
            {"enabled": True, "voltage": 1.0, "current": 0.5}).encode())
        with patch("lager.dispatchers.helpers.resolve_net_proxy",
                   side_effect=self._resolve), \
             patch.object(device_mod._session, "post",
                          side_effect=[missing, one, one, one]) as post, \
             patch.object(nets_handler.Net, "list_saved", return_value=self.NETS):
            body = self.client.get('/nets/state').get_json()

        self.assertEqual(post.call_count, 4)
        self.assertEqual([e["state"] for e in body],
                         ["CH1/on/1.00V/0.500A", "CH2/on/1.00V/0.500A",
                          "CH3/on/1.00V/0.500A"])

    def test_a_busy_supply_is_busy_on_every_channel(self):
        from lager.nets import device as device_mod

        busy = MagicMock(ok=False, status_code=503)
        busy.json.return_value = {"error": "device-busy: rigol_dp800: busy"}
        with patch("lager.dispatchers.helpers.resolve_net_proxy",
                   side_effect=self._resolve), \
             patch.object(device_mod._session, "post", return_value=busy), \
             patch.object(nets_handler.Net, "list_saved", return_value=self.NETS):
            body = self.client.get('/nets/state').get_json()

        for entry in body:
            self.assertIsNone(entry["state"])
            self.assertEqual(entry["reason_code"], nets_handler.CODE_BUSY)
            self.assertIn("device-busy", entry["reason"])
        self.assertIn(nets_handler._group_key(self.NETS[0]),
                      nets_handler._cooldown)


if __name__ == '__main__':
    unittest.main()
