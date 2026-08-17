# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0
"""
Unit tests for box/lager/automation/usb_hub/acroname.py.

Two regressions bound this driver's shape, pulling in opposite directions:

* Contention: hubs were once held connected indefinitely (class-level
  _cached_hubs), pinning the exclusive USB claim so another process could not
  connect. So the hub must never be claimed beyond a KNOWN bound.
* Latency: opening a hub is a native BrainStem connect costing whole seconds,
  so paying a fresh open per operation made every interactive command slow.

The resolution is the bounded session hold (``usb_net.HubSessionPool``): a
one-shot operation parks its connection and the cross-process lock for a short
idle window; operations inside the window reuse it; the idle timer disconnects
and releases after it. Most classes here run with the hold disabled
(``idle_s=0``, byte-identical to the old open/operate/close-per-op cycle) to
pin the open/cache/retry mechanics; ``AcronameSessionHoldTests`` pins the hold
itself with a fake clock and hand-fired timers.

BrainStem is stubbed, so no hardware is needed. (The real Acroname path still
needs a hardware smoke test before merge — see the plan.)
"""

import atexit
import importlib.util
import io
import os
import subprocess
import sys
import tempfile
import textwrap
import threading
import time
import types
import unittest
from unittest.mock import patch


_created_pkg_stubs = []


def _load_real(module_name, relpath):
    box_root = os.path.abspath(
        os.path.join(os.path.dirname(__file__), "..", "..", "..", "box")
    )
    for pkg in ("lager", "lager.util", "lager.automation", "lager.automation.usb_hub"):
        if pkg not in sys.modules:
            mod = types.ModuleType(pkg)
            mod.__path__ = []
            sys.modules[pkg] = mod
            _created_pkg_stubs.append(pkg)
    if module_name in sys.modules and getattr(sys.modules[module_name], "__file__", None):
        return sys.modules[module_name]
    path = os.path.join(box_root, relpath)
    spec = importlib.util.spec_from_file_location(module_name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


device_lock_mod = _load_real("lager.util.device_lock", "lager/util/device_lock.py")
usb_net = _load_real(
    "lager.automation.usb_hub.usb_net", "lager/automation/usb_hub/usb_net.py"
)
acroname = _load_real(
    "lager.automation.usb_hub.acroname", "lager/automation/usb_hub/acroname.py"
)

# The bare package stubs exist only so the drivers' top-level imports resolve
# while loading. Drop them now: a pathless `lager` left in sys.modules poisons
# every later real `import lager.*` in the same pytest process (this file
# collects first alphabetically, so it used to break 7 other test modules).
for _pkg in _created_pkg_stubs:
    sys.modules.pop(_pkg, None)


class _FakeResult:
    NO_ERROR = 0


# Shared simulated-exclusive-claim state, mirroring real hub behaviour.
_claim = {"held_by": None}
_opened: list = []
_closed: list = []
# The PHYSICAL hub persists port state across connect/disconnect cycles (real
# hardware does), so it lives at module scope — not on a per-connection object.
_hub_ports: dict = {}
_port_hook = None  # optional callable() invoked during a port operation


class _FakeUsb:
    def setPortEnable(self, port):
        if _port_hook:
            _port_hook()
        _hub_ports[port] = True

    def setPortDisable(self, port):
        if _port_hook:
            _port_hook()
        _hub_ports[port] = False

    def getPortState(self, port):
        val = 0b11 if _hub_ports.get(port) else 0
        return types.SimpleNamespace(error=_FakeResult.NO_ERROR, value=val)


class _FakeHub:
    _counter = 0

    def __init__(self):
        _FakeHub._counter += 1
        self.id = _FakeHub._counter
        self.connected = False
        self.usb = _FakeUsb()

    def discoverAndConnect(self, spec, serial=None):
        # Exclusive: if the hub is already claimed, connect "fails".
        if _claim["held_by"] is not None:
            return 1  # != NO_ERROR
        _claim["held_by"] = self.id
        self.connected = True
        _opened.append(self.id)
        return _FakeResult.NO_ERROR

    def disconnect(self):
        if self.connected:
            self.connected = False
            _claim["held_by"] = None
            _closed.append(self.id)


def _make_brainstem():
    stem = types.SimpleNamespace(
        USBHub3p=_FakeHub, USBHub3c=_FakeHub, USBHub2x4=_FakeHub
    )
    link = types.SimpleNamespace(Spec=types.SimpleNamespace(USB="usb-spec"))
    return types.SimpleNamespace(stem=stem, link=link)


# ---------------------------------------------------------------------------
# Session-pool test doubles: a hand-advanced clock and hand-fired timers, so
# the idle window is exercised without a single real sleep.
# ---------------------------------------------------------------------------

class _Clock:
    def __init__(self, start=1000.0):
        self.t = start

    def __call__(self):
        return self.t

    def advance(self, seconds):
        self.t += seconds


class _FakeTimer:
    """Records its delay and callback; the test fires it by calling .fn()."""

    def __init__(self, delay, fn):
        self.delay = delay
        self.fn = fn
        self.daemon = None
        self.started = False
        self.cancelled = False

    def start(self):
        self.started = True

    def cancel(self):
        self.cancelled = True


def _install_no_hold_pool(testcase):
    """A session pool with the hold disabled (idle_s=0): every claim closes
    on release, byte-identical to the old open/operate/close-per-op cycle.
    Lets the open/cache/retry tests keep pinning exactly what they pinned."""
    prior = acroname.AcronameUSBNet._session_pool
    acroname.AcronameUSBNet._session_pool = usb_net.HubSessionPool(idle_s=0)
    testcase.addCleanup(
        setattr, acroname.AcronameUSBNet, "_session_pool", prior)


class AcronameDriverTests(unittest.TestCase):
    def setUp(self):
        global _port_hook
        _claim["held_by"] = None
        _opened.clear()
        _closed.clear()
        _hub_ports.clear()
        _port_hook = None
        _FakeHub._counter = 0
        # Bypass the lazy BrainStem import by pre-seeding the module/Result.
        acroname.AcronameUSBNet._brainstem = _make_brainstem()
        acroname.AcronameUSBNet._Result = _FakeResult
        # The discovery cache is class-level (per-process); isolate tests.
        acroname.AcronameUSBNet._conn_cache = {}
        _install_no_hold_pool(self)
        self.net = acroname.AcronameUSBNet(
            {"address": "USB0::0x24FF::0x0013::BFABDDC4::INSTR"}
        )

    def test_address_and_lock_key(self):
        self.assertEqual(self.net._serial, 0xBFABDDC4)
        self.assertEqual(self.net._lock_key(), "USB0::0x24FF::0x0013::BFABDDC4::INSTR")

    def test_hub_disconnected_after_each_operation(self):
        self.net.enable("CHARGE", 0)
        self.assertIsNone(
            _claim["held_by"], "hub left connected after enable() — the pinning bug"
        )
        self.assertEqual(len(_opened), 1)
        self.assertEqual(_opened, _closed)

    def test_sequential_ops_connect_fresh_and_disconnect(self):
        self.net.enable("CHARGE", 0)
        self.net.disable("CHARGE", 0)
        self.assertIsNone(_claim["held_by"])
        self.assertEqual(len(_opened), 2)
        self.assertEqual(_opened, _closed)

    def test_connect_fails_while_another_owner_holds_hub(self):
        _claim["held_by"] = 999
        with self.assertRaises(acroname.DeviceNotFoundError):
            self.net.enable("CHARGE", 0)
        _claim["held_by"] = None
        self.net.enable("CHARGE", 0)  # succeeds once freed
        self.assertIsNone(_claim["held_by"])

    def test_toggle_reads_then_flips(self):
        # port starts off → toggle returns True (now on)
        self.assertTrue(self.net.toggle("CHARGE", 0))
        # Each op reconnects fresh, so state must come from the (persistent)
        # hardware — the second toggle reads "on" and flips back to off.
        self.assertFalse(self.net.toggle("CHARGE", 0))
        self.assertIsNone(_claim["held_by"])

    def test_cross_thread_access_is_serialized_by_lock(self):
        global _port_hook
        state = {"n": 0, "peak": 0}
        guard = threading.Lock()

        def hook():
            with guard:
                state["n"] += 1
                state["peak"] = max(state["peak"], state["n"])
            time.sleep(0.02)
            with guard:
                state["n"] -= 1

        _port_hook = hook
        threads = [
            threading.Thread(target=self.net.enable, args=("CHARGE", 0))
            for _ in range(6)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(
            state["peak"], 1, "hub_access did not serialise concurrent access"
        )


# ---------------------------------------------------------------------------
# Discovery-metadata cache (perf regression fix: 2.1s/op on 0.32.1)
# ---------------------------------------------------------------------------

_discover_calls: list = []
_spec_connects: list = []


class _FakeSpec:
    def __init__(self, serial_number):
        self.serial_number = serial_number
        self.stale = False


class _FakeSpecHub(_FakeHub):
    """A hub whose SDK supports discover.findAllModules + connectFromSpec."""

    def connectFromSpec(self, spec):
        _spec_connects.append(self.id)
        if spec.stale:
            return 1  # != NO_ERROR
        return self.discoverAndConnect("usb-spec", spec.serial_number)


def _make_spec_brainstem(specs, find_all_raises=None):
    """A spec-capable fake SDK.

    ``find_all_raises``: an exception instance for ``findAllModules`` to raise,
    so a scan that BLEW UP can be told apart from a scan that found nothing.
    """
    stem = types.SimpleNamespace(
        USBHub3p=_FakeSpecHub, USBHub3c=_FakeSpecHub, USBHub2x4=_FakeSpecHub
    )
    link = types.SimpleNamespace(Spec=types.SimpleNamespace(USB="usb-spec"))

    def find_all(transport):
        _discover_calls.append(transport)
        if find_all_raises is not None:
            raise find_all_raises
        return list(specs)

    discover = types.SimpleNamespace(findAllModules=find_all)
    return types.SimpleNamespace(stem=stem, link=link, discover=discover)


class _RefusingHub(_FakeHub):
    """A hub that refuses every connect with a chosen vendor return code."""

    rc = 7

    def discoverAndConnect(self, spec, serial=None) -> int:
        return type(self).rc

    def connectFromSpec(self, spec) -> int:
        return type(self).rc


class _ThrowingHub(_RefusingHub):
    """A hub whose connectFromSpec raises rather than returning a code."""

    def connectFromSpec(self, spec) -> int:
        raise RuntimeError("usb transport went away")


def _make_failing_brainstem(specs, hub_cls=_RefusingHub, find_all_raises=None):
    """A fake SDK where nothing will connect, for the open-failure paths."""
    stem = types.SimpleNamespace(
        USBHub3p=hub_cls, USBHub3c=hub_cls, USBHub2x4=hub_cls
    )
    link = types.SimpleNamespace(Spec=types.SimpleNamespace(USB="usb-spec"))

    def find_all(transport):
        _discover_calls.append(transport)
        if find_all_raises is not None:
            raise find_all_raises
        return list(specs)

    discover = types.SimpleNamespace(findAllModules=find_all)
    return types.SimpleNamespace(stem=stem, link=link, discover=discover)


class AcronameDiscoveryCacheTests(unittest.TestCase):
    def setUp(self):
        _claim["held_by"] = None
        _opened.clear()
        _closed.clear()
        _hub_ports.clear()
        _FakeHub._counter = 0
        _discover_calls.clear()
        _spec_connects.clear()
        self.spec = _FakeSpec(0xBFABDDC4)
        acroname.AcronameUSBNet._brainstem = _make_spec_brainstem([self.spec])
        acroname.AcronameUSBNet._Result = _FakeResult
        acroname.AcronameUSBNet._conn_cache = {}
        _install_no_hold_pool(self)
        self.net = acroname.AcronameUSBNet(
            {"address": "USB0::0x24FF::0x0013::BFABDDC4::INSTR"}
        )

    def test_discovery_runs_once_then_connects_from_cached_spec(self):
        self.net.enable("CHARGE", 0)
        self.net.disable("CHARGE", 0)
        self.net.state("CHARGE", 0)
        # One discovery scan total; every connect went through the spec.
        self.assertEqual(len(_discover_calls), 1)
        self.assertEqual(len(_spec_connects), 3)
        # The never-pin invariant still holds: nothing left claimed.
        self.assertIsNone(_claim["held_by"])
        self.assertEqual(_opened, _closed)

    def test_stale_cached_spec_is_invalidated_and_rediscovered(self):
        self.net.enable("CHARGE", 0)
        self.assertEqual(len(_discover_calls), 1)
        # Simulate a re-enumeration: the cached spec no longer connects,
        # but a fresh discovery hands out a working one.
        self.spec.stale = True
        fresh = _FakeSpec(0xBFABDDC4)
        acroname.AcronameUSBNet._brainstem = _make_spec_brainstem([fresh])
        self.net.enable("CHARGE", 0)
        self.assertEqual(len(_discover_calls), 2, "stale spec should re-discover")
        self.assertIsNone(_claim["held_by"])

    def test_the_cache_is_shared_across_controller_instances(self):
        """Every request builds a fresh controller (dispatcher._controller_for),
        so a cache that lived on the instance would be cold on every request
        and each command would silently pay full discovery. Pin that the
        SECOND instance's open is a connectFromSpec with no new scan."""
        self.net.enable("CHARGE", 0)
        second = acroname.AcronameUSBNet(
            {"address": "USB0::0x24FF::0x0013::BFABDDC4::INSTR"})
        second.disable("CHARGE", 0)
        self.assertEqual(len(_discover_calls), 1,
                         "a fresh controller re-ran discovery")
        self.assertEqual(len(_spec_connects), 2)

    def test_a_string_spec_serial_still_matches_the_address(self):
        """The address parses to an int; the SDK owns spec.serial_number's
        type. A raw == between mismatched types fails silently, and the cost
        is every operation paying full discovery while the scan looks healthy
        — the exact shape of the multi-second interactive commands."""
        stringly = _FakeSpec("BFABDDC4")
        acroname.AcronameUSBNet._brainstem = _make_spec_brainstem([stringly])
        self.net.enable("CHARGE", 0)
        self.assertEqual(len(_spec_connects), 1,
                         "the scanned spec was not matched to the address")

    def test_no_matching_serial_falls_back_to_discover_and_connect(self):
        # Discovery sees only some other hub: the driver must still connect
        # via the per-class discoverAndConnect fallback.
        other = _FakeSpec(0x12345678)
        acroname.AcronameUSBNet._brainstem = _make_spec_brainstem([other])
        self.net.enable("CHARGE", 0)
        self.assertIsNone(_claim["held_by"])
        self.assertEqual(len(_opened), 1)


class AcronameOpenFailureTests(unittest.TestCase):
    """What a hub that will not open says about itself (issue #196).

    The failure these cover is real and unfixed: BrainStem discovery not
    returning a hub the kernel has enumerated. These pin the EVIDENCE, which is
    what turns "one hub is intermittently null" into something diagnosable.
    """

    ADDRESS = "USB0::0x24FF::0x0011::BFABDDC4::INSTR"

    def setUp(self):
        _claim["held_by"] = None
        _opened.clear()
        _closed.clear()
        _hub_ports.clear()
        _FakeHub._counter = 0
        _discover_calls.clear()
        _spec_connects.clear()
        _RefusingHub.rc = 7
        acroname.AcronameUSBNet._Result = _FakeResult
        acroname.AcronameUSBNet._conn_cache = {}
        self.net = acroname.AcronameUSBNet({"address": self.ADDRESS})
        # Keep the bus out of it unless a test says otherwise: these assert on
        # the discovery half of the message.
        patcher = patch.object(acroname, "enumerate_usb_devices", return_value=[])
        patcher.start()
        self.addCleanup(patcher.stop)

    def _open(self):
        """Open, expecting failure. Returns the exception.

        The open-attempt breakdown lives on ``.detail``, not in the message:
        the message carries the remedy, because a wall of return codes buries
        the one sentence that says what to do about it.
        """
        with self.assertRaises(acroname.DeviceNotFoundError) as ctx:
            self.net._open_hub()
        return ctx.exception

    def test_message_keeps_its_prefix_and_serial(self):
        acroname.AcronameUSBNet._brainstem = _make_failing_brainstem([])
        msg = str(self._open())
        self.assertTrue(
            msg.startswith("No Acroname hub detected on USB with serial 0xBFABDDC4"),
            msg,
        )

    def test_the_message_names_the_remedy_not_the_return_codes(self):
        """What the reader needs is the next action, not the vendor's rc.

        setUp stubs the bus empty, so this is the hub-absent remedy.
        """
        _RefusingHub.rc = 7
        acroname.AcronameUSBNet._brainstem = _make_failing_brainstem([])
        msg = str(self._open())
        self.assertIn("the hub is unplugged or its upstream port is off", msg)
        self.assertNotIn("rc=7", msg)
        self.assertNotIn("findAllModules", msg)

    def test_scan_serials_are_in_the_detail_when_none_match(self):
        """The evidence the original report could not produce: discovery
        returned a hub, just not this one."""
        acroname.AcronameUSBNet._brainstem = _make_failing_brainstem(
            [_FakeSpec(0x12345678)]
        )
        detail = self._open().detail
        self.assertIn("0x12345678", detail)
        self.assertIn("no serial match", detail)

    def test_find_all_raising_is_distinguishable_from_an_empty_scan(self):
        acroname.AcronameUSBNet._brainstem = _make_failing_brainstem(
            [], find_all_raises=RuntimeError("brainstem boom")
        )
        detail = self._open().detail
        self.assertIn("findAllModules raised RuntimeError", detail)
        self.assertIn("brainstem boom", detail)
        self.assertNotIn("0 spec(s)", detail)

    def test_an_empty_scan_says_so(self):
        acroname.AcronameUSBNet._brainstem = _make_failing_brainstem([])
        self.assertIn("findAllModules ok, 0 spec(s)", self._open().detail)

    def test_connect_return_codes_appear_in_the_detail(self):
        _RefusingHub.rc = 7
        acroname.AcronameUSBNet._brainstem = _make_failing_brainstem([])
        self.assertIn("rc=7", self._open().detail)

    def test_a_raising_connect_from_spec_is_reported_not_swallowed(self):
        acroname.AcronameUSBNet._brainstem = _make_failing_brainstem(
            [_FakeSpec(0xBFABDDC4)], hub_cls=_ThrowingHub
        )
        detail = self._open().detail
        self.assertIn("connectFromSpec RuntimeError", detail)
        self.assertIn("usb transport went away", detail)

    def test_warning_is_logged_with_the_uncapped_detail(self):
        acroname.AcronameUSBNet._brainstem = _make_failing_brainstem(
            [_FakeSpec(0x12345678)]
        )
        with self.assertLogs(acroname.logger, level="WARNING") as cm:
            with self.assertRaises(acroname.DeviceNotFoundError):
                self.net._open_hub()
        blob = "\n".join(cm.output)
        self.assertIn(self.ADDRESS, blob)          # which hub
        self.assertIn("0x12345678", blob)          # what discovery saw
        self.assertIn("rc=7", blob)                # what each attempt returned

    def test_the_detail_stays_bounded_on_a_crowded_bus(self):
        acroname.AcronameUSBNet._brainstem = _make_failing_brainstem(
            [_FakeSpec(0x1000 + i) for i in range(40)]
        )
        exc = self._open()
        self.assertLess(len(exc.detail), 400, exc.detail)
        self.assertIn("more", exc.detail)
        # The message is the remedy sentence and stays short regardless of how
        # crowded the bus is -- it never carried the serials in the first place.
        self.assertLess(len(str(exc)), 200, str(exc))


class AcronameSysfsCrossCheckTests(unittest.TestCase):
    """Separating a vendor-library fault from an unplugged cable."""

    ADDRESS = "USB0::0x24FF::0x0011::BFABDDC4::INSTR"

    def setUp(self):
        _claim["held_by"] = None
        _opened.clear()
        _closed.clear()
        _FakeHub._counter = 0
        _discover_calls.clear()
        _RefusingHub.rc = 7
        acroname.AcronameUSBNet._Result = _FakeResult
        acroname.AcronameUSBNet._conn_cache = {}
        acroname.AcronameUSBNet._brainstem = _make_failing_brainstem([])
        self.net = acroname.AcronameUSBNet({"address": self.ADDRESS})

    def _open_with_bus(self, devices):
        """Open against a fake bus, expecting failure. Returns the exception."""
        with patch.object(acroname, "enumerate_usb_devices", return_value=devices):
            with self.assertRaises(acroname.DeviceNotFoundError) as ctx:
                self.net._open_hub()
        return ctx.exception

    def test_hub_on_the_bus_but_not_discovered_says_exactly_that(self):
        exc = self._open_with_bus([
            {"vid": "24ff", "pid": "0011", "serial": "BFABDDC4"},
            {"vid": "24ff", "pid": "8011", "serial": None},
        ])
        self.assertIn("serial present on the bus", exc.detail)
        self.assertIn("discovery did not return an enumerated hub", exc.detail)
        self.assertEqual(acroname.HUB_UNREACHABLE, exc.classification)
        # The remedy points at the bench, which is the whole reason to classify.
        self.assertIn("check hub power and the upstream cable", str(exc))

    def test_a_lowercase_sysfs_serial_still_matches(self):
        """sysfs hands back a string, the address parses to an int."""
        exc = self._open_with_bus(
            [{"vid": "24ff", "pid": "0011", "serial": "bfabddc4"}]
        )
        self.assertIn("serial present on the bus", exc.detail)
        self.assertEqual(acroname.HUB_UNREACHABLE, exc.classification)

    def test_other_acroname_devices_but_not_this_serial(self):
        exc = self._open_with_bus([
            {"vid": "24ff", "pid": "0013", "serial": "AAAA0001"},
            {"vid": "24ff", "pid": "8013", "serial": "AAAA0001"},
        ])
        self.assertIn("none with serial 0xBFABDDC4", exc.detail)
        self.assertIn("24ff:0013", exc.detail)
        self.assertEqual(acroname.HUB_SERIAL_MISMATCH, exc.classification)
        self.assertIn("check the net's address", str(exc))

    def test_a_device_with_no_serial_descriptor_is_counted_not_ignored(self):
        """An empty iSerial must not read as 'the hub is absent'."""
        exc = self._open_with_bus([
            {"vid": "24ff", "pid": "8011", "serial": ""},
            {"vid": "24ff", "pid": "0013", "serial": "AAAA0001"},
        ])
        self.assertIn("1 with no serial descriptor", exc.detail)
        self.assertNotEqual(acroname.HUB_ABSENT, exc.classification)

    def test_nothing_on_the_bus_says_nothing_on_the_bus(self):
        exc = self._open_with_bus([])
        self.assertIn("no Acroname (24ff) device on the bus", exc.detail)
        self.assertEqual(acroname.HUB_ABSENT, exc.classification)
        self.assertIn("the hub is unplugged", str(exc))

    def test_sysfs_failure_cannot_change_the_exception_type(self):
        """The guard-rail: a diagnostic must never replace the real error."""
        with patch.object(acroname, "enumerate_usb_devices",
                          side_effect=OSError("sysfs is gone")):
            with self.assertRaises(acroname.DeviceNotFoundError) as ctx:
                self.net._open_hub()
        exc = ctx.exception
        self.assertIn("sysfs: unavailable", exc.detail)
        self.assertNotIn("sysfs is gone", exc.detail)
        self.assertNotIn("sysfs is gone", str(exc))
        # Unreadable sysfs is "we could not tell", not "nothing is there".
        self.assertEqual(acroname.HUB_OPEN_FAILED, exc.classification)

    def test_the_cross_check_filters_on_vendor_not_product(self):
        """One hub enumerates under several pids; a pid filter would hide the
        component that proves it is physically present."""
        with patch.object(acroname, "enumerate_usb_devices",
                          return_value=[]) as enum:
            with self.assertRaises(acroname.DeviceNotFoundError):
                self.net._open_hub()
        self.assertEqual(enum.call_args.kwargs, {"vid": "24ff"})


class OpenFailureDetailTests(unittest.TestCase):
    """The summary formatter alone: no SDK, no bus, no exception."""

    def test_reports_each_piece_it_was_given(self):
        out = acroname._open_failure_detail(
            {"cache": "miss",
             "discovery": "findAllModules ok, 1 spec(s)",
             "spec_serials": ["0x12345678"],
             "spec_match": False,
             "attempts": ["USBHub2x4/discover discoverAndConnect rc=7"]},
            "no Acroname (24ff) device on the bus",
        )
        self.assertIn("0x12345678", out)
        self.assertIn("no serial match", out)
        self.assertIn("rc=7", out)
        self.assertIn("no Acroname (24ff) device on the bus", out)

    def test_a_cache_hit_that_failed_is_named(self):
        out = acroname._open_failure_detail(
            {"cache": "hit (USBHub3p), connectFromSpec rc=1",
             "discovery": "findAllModules ok, 0 spec(s)",
             "spec_serials": [], "spec_match": False, "attempts": []},
            None,
        )
        self.assertIn("cached spec: hit (USBHub3p)", out)

    def test_a_clean_miss_does_not_mention_the_cache(self):
        out = acroname._open_failure_detail(
            {"cache": "miss", "discovery": "findAllModules ok, 0 spec(s)",
             "spec_serials": [], "spec_match": False, "attempts": []},
            None,
        )
        self.assertNotIn("cached spec", out)

    def test_missing_sysfs_renders_as_unavailable(self):
        out = acroname._open_failure_detail(
            {"cache": "miss", "discovery": "x", "spec_serials": [],
             "spec_match": False, "attempts": []},
            None,
        )
        self.assertIn("sysfs: unavailable", out)

    def test_long_serial_lists_are_capped(self):
        out = acroname._open_failure_detail(
            {"cache": "miss", "discovery": "findAllModules ok, 40 spec(s)",
             "spec_serials": [f"0x{i:08X}" for i in range(40)],
             "spec_match": False, "attempts": []},
            None,
        )
        self.assertIn("+34 more", out)
        self.assertLessEqual(len(out), acroname._OPEN_FAILURE_DETAIL_MAX)

    def test_the_detail_is_truncated_to_its_bound(self):
        out = acroname._open_failure_detail(
            {"cache": "miss", "discovery": "x", "spec_serials": [],
             "spec_match": False, "attempts": ["y" * 500]},
            None,
        )
        self.assertLessEqual(len(out), acroname._OPEN_FAILURE_DETAIL_MAX)

    def test_the_bus_verdict_survives_a_long_attempt_list(self):
        """The verdict is the point of the message -- it must not be the thing
        that gets cut when the attempt list is long."""
        out = acroname._open_failure_detail(
            {"cache": "miss", "discovery": "findAllModules ok, 1 spec(s)",
             "spec_serials": ["0x12345678"], "spec_match": False,
             "attempts": [f"USBHub{i}/discover discoverAndConnect rc={i}"
                          for i in range(30)]},
            "serial present on the bus (24ff:0011) -- discovery did not "
            "return an enumerated hub",
        )
        self.assertLessEqual(len(out), acroname._OPEN_FAILURE_DETAIL_MAX)
        self.assertIn("discovery did not return an enumerated hub", out)
        self.assertIn("0x12345678", out)

    def test_identical_attempts_are_collapsed(self):
        """Three hub classes failing the same way is one fact, not three."""
        out = acroname._open_failure_detail(
            {"cache": "miss", "discovery": "findAllModules ok, 0 spec(s)",
             "spec_serials": [], "spec_match": False,
             "attempts": ["discoverAndConnect rc=7"] * 3},
            None,
        )
        self.assertIn("discoverAndConnect rc=7 (x3)", out)

    def test_it_is_deterministic(self):
        """Every net on a failed hub gets this string; the CLI groups identical
        reasons into one footnote line, so it must not vary run to run."""
        diag = {"cache": "miss", "discovery": "findAllModules ok, 2 spec(s)",
                "spec_serials": ["0x1", "0x2"], "spec_match": False,
                "attempts": ["a rc=1", "b rc=2"]}
        first = acroname._open_failure_detail(diag, "on the bus")
        for _ in range(5):
            self.assertEqual(acroname._open_failure_detail(diag, "on the bus"),
                             first)


class AcronameContextHealthTests(unittest.TestCase):
    """Can this PROCESS reach the bus, as distinct from finding OUR hub?

    This is the signal that separates a wedged in-process USB context, which a
    service restart repairs, from a hub that is simply not answering, which it
    cannot. Getting it wrong in the optimistic direction suppresses the one
    recovery that works; getting it wrong in the pessimistic direction restarts
    a service for nothing and drops unrelated in-flight work.
    """

    ADDRESS = "USB0::0x24FF::0x0011::BFABDDC4::INSTR"

    def setUp(self):
        _claim["held_by"] = None
        _opened.clear()
        _closed.clear()
        _FakeHub._counter = 0
        _discover_calls.clear()
        _RefusingHub.rc = 7
        acroname.AcronameUSBNet._Result = _FakeResult
        acroname.AcronameUSBNet._conn_cache = {}
        self.net = acroname.AcronameUSBNet({"address": self.ADDRESS})
        patcher = patch.object(acroname, "enumerate_usb_devices", return_value=[])
        patcher.start()
        self.addCleanup(patcher.stop)

    def _health(self):
        with self.assertRaises(acroname.DeviceNotFoundError) as ctx:
            self.net._open_hub()
        return ctx.exception.usb_context_healthy

    def test_a_scan_that_listed_another_hub_proves_the_context_healthy(self):
        """The bench case: discovery returned the neighbouring hub, so this
        process demonstrably walked the bus and the missing hub is the fault."""
        acroname.AcronameUSBNet._brainstem = _make_failing_brainstem(
            [_FakeSpec(0x12345678)]
        )
        self.assertIs(True, self._health())

    def test_an_empty_scan_does_not_prove_the_context_healthy(self):
        """Zero specs cannot tell a silent hub from a broken USB stack, and on
        a single-hub bench those are the only two possibilities. Must stay
        unknown so the restart path is not wrongly suppressed."""
        acroname.AcronameUSBNet._brainstem = _make_failing_brainstem([])
        self.assertIsNone(self._health())

    def test_a_raising_scan_reports_the_context_unhealthy(self):
        acroname.AcronameUSBNet._brainstem = _make_failing_brainstem(
            [], find_all_raises=RuntimeError("brainstem boom")
        )
        self.assertIs(False, self._health())

    def test_an_sdk_without_find_all_leaves_it_unknown(self):
        brainstem = _make_failing_brainstem([])
        brainstem.discover = types.SimpleNamespace()  # no findAllModules
        acroname.AcronameUSBNet._brainstem = brainstem
        self.assertIsNone(self._health())

    def test_the_driver_declares_it_holds_a_context_between_ops(self):
        """The bounded session hold parks a live connection for the idle
        window after a one-shot operation, so a re-enumeration inside that
        window CAN orphan a handle — the self-restart recovery must stay
        reachable for this driver (sysfs-gated and cooldown-bounded)."""
        self.assertTrue(acroname.AcronameUSBNet.holds_usb_context_between_ops)


class AcronameOpenRetryTests(unittest.TestCase):
    """One retry, and only on the evidence that a retry can help."""

    ADDRESS = "USB0::0x24FF::0x0011::BFABDDC4::INSTR"
    ON_BUS = [{"vid": "24ff", "pid": "0011", "serial": "BFABDDC4"}]
    OTHERS = [{"vid": "24ff", "pid": "0013", "serial": "AAAA0001"}]

    def setUp(self):
        _claim["held_by"] = None
        _opened.clear()
        _closed.clear()
        _FakeHub._counter = 0
        _discover_calls.clear()
        _RefusingHub.rc = 7
        acroname.AcronameUSBNet._Result = _FakeResult
        acroname.AcronameUSBNet._conn_cache = {}
        acroname.AcronameUSBNet._brainstem = _make_failing_brainstem([])
        _install_no_hold_pool(self)
        self.net = acroname.AcronameUSBNet({"address": self.ADDRESS})
        sleep = patch.object(acroname.time, "sleep")
        self.sleep = sleep.start()
        self.addCleanup(sleep.stop)

    def _attempt(self, devices, **kw):
        with patch.object(acroname, "enumerate_usb_devices",
                          return_value=devices):
            with self.assertRaises(acroname.DeviceNotFoundError):
                self.net._with_hub(lambda hub: None, **kw)

    def test_an_unreachable_hub_is_retried_once(self):
        self._attempt(self.ON_BUS)
        self.assertEqual(2, len(_discover_calls), _discover_calls)
        self.sleep.assert_called_once_with(acroname._OPEN_RETRY_SETTLE_S)

    def test_the_second_failure_is_what_propagates(self):
        """The caller sees the retry's failure, fully classified -- not a
        stand-in raised by the retry machinery itself."""
        with patch.object(acroname, "enumerate_usb_devices",
                          return_value=self.ON_BUS):
            with self.assertRaises(acroname.DeviceNotFoundError) as ctx:
                self.net._with_hub(lambda hub: None)
        exc = ctx.exception
        self.assertEqual(acroname.HUB_UNREACHABLE, exc.classification)
        self.assertIn("check hub power and the upstream cable", str(exc))
        self.assertIn("rc=7", exc.detail)

    def test_an_absent_hub_is_not_retried(self):
        """If the serial is not on the bus, attempt two provably cannot
        connect either. This is what keeps the retry off the deadline."""
        self._attempt([])
        self.assertEqual(1, len(_discover_calls), _discover_calls)
        self.sleep.assert_not_called()

    def test_a_serial_mismatch_is_not_retried(self):
        self._attempt(self.OTHERS)
        self.assertEqual(1, len(_discover_calls), _discover_calls)
        self.sleep.assert_not_called()

    def test_the_batch_path_never_retries(self):
        """Pins the promise that `lager nets state` timing is unchanged.

        states() is the polling path and its deadline is shared across every
        instrument on the box, so it must cost exactly one open attempt even
        for the one classification the single-net path does retry.
        """
        with patch.object(acroname, "enumerate_usb_devices",
                          return_value=self.ON_BUS):
            with self.assertRaises(acroname.DeviceNotFoundError):
                self.net.states([0, 1, 2, 3])
        self.assertEqual(1, len(_discover_calls), _discover_calls)
        self.sleep.assert_not_called()

    def test_a_caller_budget_clamps_the_operation_deadline(self):
        """``states(timeout=)`` bounds the whole cycle (issue #205): the
        ``run_hub_op`` deadline is at most the caller's budget, never the
        full ``HUB_OP_TIMEOUT_S`` -- that is what lets the state sweep's
        dispatcher hand each hub only the time actually remaining."""
        calls = []
        real = acroname.run_hub_op

        def spy(key, fn, timeout=None):
            calls.append(timeout)
            return real(key, fn, timeout=timeout)

        with patch.object(acroname, "enumerate_usb_devices",
                          return_value=self.ON_BUS), \
             patch.object(acroname, "run_hub_op", spy):
            with self.assertRaises(acroname.DeviceNotFoundError):
                self.net.states([0, 1], timeout=3.0)
        self.assertEqual(1, len(calls))
        self.assertLessEqual(calls[0], 3.0)
        # The lock was uncontended, so nearly the whole budget survives to
        # the operation deadline.
        self.assertGreater(calls[0], 2.0)

    def test_the_stale_cache_is_dropped_before_the_retry(self):
        acroname.AcronameUSBNet._conn_cache[self.ADDRESS] = {
            "cls": _RefusingHub, "spec": None,
        }
        self._attempt(self.ON_BUS)
        self.assertNotIn(self.ADDRESS, acroname.AcronameUSBNet._conn_cache)

    def test_both_attempts_share_ONE_operation_deadline(self):
        """The retry lives inside run_hub_op, not around it.

        Wrapping the retry outside would hand each attempt its own
        HUB_OP_TIMEOUT_S, so a hub that hangs on open could hold a caller for
        twice the bound the CLI waits on -- turning the box's structured 504
        into a transport timeout, which is the failure that deadline exists to
        prevent. One call to run_hub_op, two opens inside it.
        """
        calls = []
        real = acroname.run_hub_op

        def spy(key, fn, timeout=None):
            calls.append(timeout)
            return real(key, fn, timeout=timeout)

        with patch.object(acroname, "run_hub_op", spy):
            self._attempt(self.ON_BUS)
        self.assertEqual(1, len(calls), f"run_hub_op called {len(calls)}x")
        self.assertEqual(2, len(_discover_calls), "expected the retry to happen")

    def test_the_never_pin_invariant_survives_a_failed_retry(self):
        """Non-negotiable: a failed open must leave the hub unclaimed, or the
        next process to want it is blocked by a connection nobody holds."""
        self._attempt(self.ON_BUS)
        self.assertIsNone(_claim["held_by"])
        self.assertEqual(sorted(_opened), sorted(_closed))


class AcronameSessionHoldTests(unittest.TestCase):
    """The bounded session hold: reuse inside the idle window, release after.

    Driven entirely by a fake clock and hand-fired timers — no sleeps. The
    flock is real (a private DeviceLockManager under this test's own lock
    directory), so "another process can acquire after the window" is proven
    against the actual fcntl mechanism, not a stand-in: flock conflicts are
    per open file description, so a second fd in this process is a faithful
    simulation of a second process.
    """

    ADDRESS = "USB0::0x24FF::0x0013::BFABDDC4::INSTR"

    def setUp(self):
        global _port_hook
        _claim["held_by"] = None
        _opened.clear()
        _closed.clear()
        _hub_ports.clear()
        _port_hook = None
        _FakeHub._counter = 0
        _discover_calls.clear()
        _spec_connects.clear()
        self.spec = _FakeSpec(0xBFABDDC4)
        acroname.AcronameUSBNet._brainstem = _make_spec_brainstem([self.spec])
        acroname.AcronameUSBNet._Result = _FakeResult
        acroname.AcronameUSBNet._conn_cache = {}

        self.clock = _Clock()
        self.timers = []

        def _timer_factory(delay, fn):
            timer = _FakeTimer(delay, fn)
            self.timers.append(timer)
            return timer

        self.mgr = device_lock_mod.DeviceLockManager(
            lock_subdir=f"lager_test_hub_sessions_{os.getpid()}_{id(self)}")
        self.pool = usb_net.HubSessionPool(
            idle_s=2.5, lock_manager=self.mgr, now=self.clock,
            timer_factory=_timer_factory)
        prior = acroname.AcronameUSBNet._session_pool
        acroname.AcronameUSBNet._session_pool = self.pool
        self.addCleanup(
            setattr, acroname.AcronameUSBNet, "_session_pool", prior)
        self.addCleanup(self.pool.drain)
        # The pool registers its exit drain on the first park; drop it again
        # so a pytest process does not accumulate one dead test pool per test.
        self.addCleanup(atexit.unregister, self.pool._drain_at_exit)
        self.net = acroname.AcronameUSBNet({"address": self.ADDRESS})

    # -- helpers ------------------------------------------------------- #

    def _flock_from_elsewhere(self):
        """Try to take the hub's flock the way a second process would.
        Returns True (and immediately unlocks) if it could be taken."""
        import fcntl
        path = self.mgr._get_lock_path(self.ADDRESS)
        with open(path, "a+") as fh:
            try:
                fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                return False
            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
            return True

    def _fire_last_timer(self):
        self.timers[-1].fn()

    # -- reuse --------------------------------------------------------- #

    def test_a_burst_of_one_shot_ops_pays_one_open(self):
        self.net.enable("CHARGE", 0)
        self.net.disable("CHARGE", 0)
        self.assertTrue(self.net.toggle("CHARGE", 0))
        self.assertEqual(len(_opened), 1, "ops inside the window reconnected")
        self.assertEqual(len(_spec_connects), 1)
        self.assertEqual(_closed, [], "the session closed early")
        self.assertIsNotNone(_claim["held_by"], "session did not stay open")

    def test_each_one_shot_op_refreshes_the_idle_window(self):
        self.net.enable("CHARGE", 0)
        self.clock.advance(2.0)
        self.net.disable("CHARGE", 0)
        # The prior timer was cancelled and a fresh full window armed.
        self.assertTrue(self.timers[-2].cancelled)
        self.assertEqual(self.timers[-1].delay, 2.5)

    def test_the_idle_timer_is_a_daemon(self):
        """A parked session must never be what keeps a `lager python`
        script's interpreter from exiting."""
        self.net.enable("CHARGE", 0)
        self.assertTrue(self.timers[-1].started)
        self.assertIs(self.timers[-1].daemon, True)

    # -- release ------------------------------------------------------- #

    def test_idle_expiry_disconnects_and_releases_the_flock(self):
        self.net.enable("CHARGE", 0)
        self.assertFalse(self._flock_from_elsewhere(),
                         "flock free while the session held the hub")
        self.clock.advance(2.5)
        self._fire_last_timer()
        self.assertIsNone(_claim["held_by"], "hub still claimed after expiry")
        self.assertEqual(_opened, _closed)
        self.assertTrue(self._flock_from_elsewhere(),
                        "another process cannot take the hub after the window")

    # -- release at interpreter exit ------------------------------------ #

    def test_process_exit_closes_a_parked_session(self):
        """No open USB handle may survive into interpreter finalisation.

        A `lager python` script that enables a hub net and exits inside the
        idle window leaves the session parked -- the expiry timer is a daemon
        and never fires (test_the_idle_timer_is_a_daemon). BrainStem's ZeroMQ
        sockets are then still open when CPython finalises: czmq reports them
        dangling and aborts, so a script whose own logic passed dies with
        SIGABRT after printing its results.
        """
        self.net.enable("CHARGE", 0)
        self.assertIsNotNone(_claim["held_by"], "nothing was parked")
        self.pool._drain_at_exit()
        self.assertIsNone(_claim["held_by"],
                          "the hub was still claimed at interpreter exit")
        self.assertEqual(_opened, _closed)
        self.assertEqual(self.pool._sessions, {})
        self.assertTrue(self._flock_from_elsewhere(),
                        "the flock outlived the process")

    def test_a_real_interpreter_exit_drains_a_parked_session(self):
        """The same thing again, but through CPython's own shutdown rather
        than a hand-called hook -- a real park, a real daemon timer with a
        window that has not expired, and a real `sys.exit(0)`.

        This is the defect's exact shape: every piece below the registration
        worked, and the handle still leaked because nothing was wired to
        process exit. A test that calls the hook itself cannot tell the two
        apart. No hardware and no vendor SDK: the pool is loaded by path with
        stub parent packages, the way this module loads it.
        """
        box_root = os.path.abspath(
            os.path.join(os.path.dirname(__file__), "..", "..", "..", "box"))
        with tempfile.TemporaryDirectory() as tmp:
            receipt = os.path.join(tmp, "closed")
            script = textwrap.dedent(f"""
                import os, sys, types
                box = {box_root!r}
                for pkg, sub in (
                        ("lager", "lager"),
                        ("lager.util", "lager/util"),
                        ("lager.automation", "lager/automation"),
                        ("lager.automation.usb_hub", "lager/automation/usb_hub")):
                    mod = types.ModuleType(pkg)
                    mod.__path__ = [os.path.join(box, sub)]
                    sys.modules[pkg] = mod
                from lager.automation.usb_hub import usb_net

                def _close(handle):
                    with open({receipt!r}, "w") as fh:
                        fh.write(handle)

                # A window far longer than the process lives, so the only
                # thing that can close this handle is the exit drain.
                pool = usb_net.HubSessionPool(
                    idle_s=600.0,
                    lock_manager=types.SimpleNamespace(
                        acquire_lock=lambda key, timeout=None: True,
                        release_lock=lambda key: None))
                pool._park("hub-key", "HANDLE", _close, pool._now() + 600.0)
                sys.exit(0)
            """)
            proc = subprocess.run([sys.executable, "-c", script],
                                  capture_output=True, text=True, timeout=60)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertEqual(proc.stderr, "",
                             "exit cleanup must be silent, not noisy")
            self.assertTrue(
                os.path.exists(receipt),
                "the parked handle was never closed: it was still open when "
                "the interpreter finalised, which is what aborts the process")
            with open(receipt) as fh:
                self.assertEqual(fh.read(), "HANDLE")

    def _start_expiry(self, key, close_fn):
        """Park `key`, let its window lapse, and run the expiry on a thread of
        its own -- the shape production has when the idle timer fires. Returns
        the thread, having waited until the session is popped and the close is
        genuinely in flight."""
        self.mgr.acquire_lock(key, timeout=1.0)
        self.pool._park(key, "H", close_fn, self.clock() + 2.5)
        self.clock.advance(2.5)
        worker = threading.Thread(target=self.timers[-1].fn)
        worker.start()
        self.addCleanup(worker.join, 10)
        deadline = time.monotonic() + 5
        while key in self.pool._sessions and time.monotonic() < deadline:
            time.sleep(0.01)
        return worker

    def test_the_exit_drain_waits_for_a_teardown_already_in_flight(self):
        """Once the idle timer has fired the session is gone from _sessions,
        so the drain finds nothing -- while the disconnect it started is still
        running on a daemon thread that finalisation would kill midway,
        leaving the same dangling sockets. Exit has to wait for it.

        On a 2.5s window and a ~2s disconnect that gap is about as wide as the
        window, so any test idling a few seconds after its last hub operation
        lands in it.
        """
        closed = []

        def _slow_close(handle):
            time.sleep(0.3)
            closed.append(handle)

        self._start_expiry("slow-hub", _slow_close)
        self.assertEqual(self.pool._sessions, {},
                         "the timer had not popped the session yet")
        self.assertEqual(closed, [], "the close finished before it was tested")

        self.pool._drain_at_exit()

        self.assertEqual(closed, ["H"],
                         "exit did not wait for the teardown already running")

    def test_the_exit_wait_for_a_teardown_is_bounded(self):
        """A disconnect slower than the bound is a wedged hub, and waiting one
        out at exit buys nothing -- the handle is lost either way. Exit must
        leave rather than hang."""
        release = threading.Event()

        def _never_finishes(handle):
            release.wait(30)

        self._start_expiry("wedged-hub", _never_finishes)
        # Registered AFTER _start_expiry's join, so LIFO cleanup frees the
        # close before anything waits on it -- otherwise the teardown this
        # test deliberately wedges is waited out at teardown.
        self.addCleanup(release.set)

        with patch.object(usb_net, "_EXIT_TEARDOWN_WAIT_S", 0.2):
            start = time.monotonic()
            self.pool._drain_at_exit()
            waited = time.monotonic() - start

        self.assertLess(waited, 5, "exit hung on a teardown that never finishes")

    def test_the_exit_drain_is_registered_the_first_time_a_session_is_parked(self):
        """Lazy, and exactly once. Registering at import would land before the
        vendor SDK's own import, and atexit runs LIFO."""
        with patch.object(usb_net, "atexit") as fake_atexit:
            self.net.states([0])                 # hold=False: never parks
            fake_atexit.register.assert_not_called()
            self.net.enable("CHARGE", 0)
            fake_atexit.register.assert_called_once_with(
                self.pool._drain_at_exit)
            self.net.disable("CHARGE", 0)        # parks again
            fake_atexit.register.assert_called_once()

    def test_the_exit_drain_does_not_spawn_a_worker_thread(self):
        """A thread cannot be created during interpreter shutdown (CPython
        3.12+ raises), so a deadline round the exit close would raise instead
        of bounding it -- and a run_hub_op timeout fires the hang hook, which
        for a box service ends in os._exit(70). The exit close is inline."""
        self.net.enable("CHARGE", 0)
        with patch.object(usb_net, "run_hub_op",
                          side_effect=AssertionError(
                              "the exit drain must not use a worker thread")):
            self.pool.drain()
        self.assertEqual(_opened, _closed)

    def test_a_hub_that_refuses_to_close_does_not_strand_the_others(self):
        """One unresponsive hub must not leave every other hub on the box
        claimed for the next process. Driven at the pool level: the driver
        fakes share one global claim slot, so two hubs cannot go through it."""
        closed = []

        def _refuses(handle):
            raise RuntimeError("hub will not disconnect")

        self.mgr.acquire_lock("bad-hub", timeout=1.0)
        self.mgr.acquire_lock("good-hub", timeout=1.0)
        self.pool._park("bad-hub", object(), _refuses, self.clock() + 2.5)
        self.pool._park("good-hub", "H", closed.append, self.clock() + 2.5)

        with self.assertLogs(usb_net.logger, level="ERROR"):
            self.pool._drain_at_exit()

        self.assertEqual(closed, ["H"], "a raising close stranded the next hub")
        self.assertEqual(self.pool._sessions, {})
        self.assertEqual(self.mgr.lock_handles, {},
                         "a hub that would not close kept its flock")

    def test_an_early_timer_fire_is_a_no_op(self):
        """A refresh re-arms the window; a stale fire from the cancelled
        timer must not tear down the refreshed session."""
        self.net.enable("CHARGE", 0)
        stale = self.timers[-1]
        self.clock.advance(1.0)
        self.net.disable("CHARGE", 0)  # refreshes; cancels `stale`
        stale.fn()  # fires anyway (cancel raced the fire)
        self.assertIsNotNone(_claim["held_by"],
                             "a cancelled timer tore down a live session")

    # -- the polling sweep --------------------------------------------- #

    def test_states_alone_never_creates_a_session(self):
        self.net.states([0, 1])
        self.assertEqual(len(_opened), 1)
        self.assertEqual(_opened, _closed, "the polling sweep parked a session")
        self.assertIsNone(_claim["held_by"])
        self.assertEqual(self.timers, [])
        self.assertTrue(self._flock_from_elsewhere())

    def test_states_rides_a_session_without_extending_it(self):
        self.net.enable("CHARGE", 0)
        self.clock.advance(1.0)
        out = self.net.states([0, 1])
        self.assertEqual(out, {0: True, 1: False})
        self.assertEqual(len(_opened), 1, "states() reconnected needlessly")
        # Re-armed for the REMAINDER of the window the enable opened — under
        # 1 Hz polling the session still dies at the original expiry.
        self.assertAlmostEqual(self.timers[-1].delay, 1.5)

    def test_states_does_not_resurrect_an_expired_window(self):
        self.net.enable("CHARGE", 0)
        self.clock.advance(3.0)  # window over; timer just hasn't fired yet
        self.net.states([0, 1])
        self.assertIsNone(_claim["held_by"],
                          "polling kept a session alive past its window")
        self.assertEqual(_opened, _closed)

    # -- staleness and wedges ------------------------------------------ #

    def test_a_stale_held_handle_is_reopened_once(self):
        global _port_hook
        self.net.enable("CHARGE", 0)
        calls = {"n": 0}

        def _fail_first():
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("handle orphaned by re-enumeration")

        _port_hook = _fail_first
        self.net.enable("CHARGE", 0)  # reuse fails once, fresh open succeeds
        self.assertEqual(len(_opened), 2)
        self.assertIn(_opened[0], _closed, "the stale handle was not closed")
        self.assertEqual(_claim["held_by"], _opened[1],
                         "the fresh handle should be parked for the window")

    def test_a_wedged_op_inside_a_session_poisons_only_this_process(self):
        global _port_hook
        address = "USB0::0x24FF::0x0013::BFABDDC4::INSTR::WEDGE"
        net = acroname.AcronameUSBNet({"address": address})
        net.enable("CHARGE", 0)
        def _wedged_native_call():
            time.sleep(30)

        _port_hook = _wedged_native_call
        with patch.object(acroname, "HUB_OP_TIMEOUT_S", 0.2):
            with self.assertRaises(usb_net.HubOperationTimeout):
                net.enable("CHARGE", 0)
        # The stuck thread owns the handle: never disconnected...
        self.assertIsNotNone(_claim["held_by"])
        # ...the session is gone, so nothing will hand that handle out again...
        self.assertEqual(self.pool._sessions, {})
        # ...this process fails fast (thread lock stays held)...
        self.assertFalse(
            usb_net._local_hub_lock(address).acquire(timeout=0.05))
        # ...and the flock is released, because the wedge is per-process.
        import fcntl
        path = self.mgr._get_lock_path(address)
        with open(path, "a+") as fh:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)

    def test_the_exit_drain_never_touches_a_wedged_hub(self):
        """The stuck thread owns that handle, so closing it from the exit path
        would be a use-after-free -- and its per-hub thread lock is held
        forever by design, so a drain that waited on it would hang the
        interpreter at exit. That this test terminates is the assertion.

        Safe by construction rather than by check: claim() popped the session
        and abandon_wedged() never re-parks, so a wedged handle is not in
        _sessions for the drain to find.
        """
        global _port_hook
        address = "USB0::0x24FF::0x0013::BFABDDC4::INSTR::WEDGE-EXIT"
        net = acroname.AcronameUSBNet({"address": address})
        net.enable("CHARGE", 0)
        held, closed_before = _claim["held_by"], list(_closed)

        def _wedged_native_call():
            time.sleep(30)

        _port_hook = _wedged_native_call
        with patch.object(acroname, "HUB_OP_TIMEOUT_S", 0.2):
            with self.assertRaises(usb_net.HubOperationTimeout):
                net.enable("CHARGE", 0)
        _port_hook = None

        self.pool._drain_at_exit()
        self.assertEqual(_closed, closed_before,
                         "the exit drain closed a handle a stuck thread owns")
        self.assertEqual(_claim["held_by"], held)
        self.assertFalse(usb_net._local_hub_lock(address).acquire(timeout=0.05),
                         "the wedged hub's thread lock was released")

    # -- the warm fast path (regression from the 7-9s/op incident) ------ #

    def test_a_warm_open_after_expiry_is_scan_free(self):
        """After the window closes, the next op must reopen from the cached
        spec — one connectFromSpec, zero discovery scans. This is the exact
        regression behind the multi-second interactive commands: the reopen
        silently paying full discovery on every call."""
        self.net.enable("CHARGE", 0)
        self.clock.advance(2.5)
        self._fire_last_timer()
        self.net.enable("CHARGE", 0)
        self.assertEqual(len(_discover_calls), 1,
                         "the warm reopen ran a discovery scan")
        self.assertEqual(len(_spec_connects), 2,
                         "the warm reopen did not use connectFromSpec")

    # -- the timing log ------------------------------------------------ #

    def test_a_completed_cycle_logs_its_phase_breakdown_at_debug(self):
        with self.assertLogs(acroname.logger, level="DEBUG") as cm:
            self.net.enable("CHARGE", 0)
        line = next(l for l in cm.output if "cycle" in l)
        self.assertIn("enable cycle", line)
        self.assertIn("lock", line)
        self.assertIn("open", line)
        self.assertIn("[discovery]", line)   # which open path ran
        self.assertIn("close held", line)    # session parked, not closed
        self.assertIn("-> ok", line)
        self.assertTrue(line.startswith("DEBUG"), line)

    def test_a_session_reuse_cycle_names_its_path(self):
        self.net.enable("CHARGE", 0)
        with self.assertLogs(acroname.logger, level="DEBUG") as cm:
            self.net.disable("CHARGE", 0)
        line = next(l for l in cm.output if "cycle" in l)
        self.assertIn("[session-reuse]", line)

    def test_a_slow_cycle_logs_at_info(self):
        """The escalation is what makes a silently-slow open path visible in
        a box log that only keeps INFO and up."""
        with patch.object(acroname, "_SLOW_CYCLE_INFO_S", 0.0):
            with self.assertLogs(acroname.logger, level="INFO") as cm:
                self.net.enable("CHARGE", 0)
        line = next(l for l in cm.output if "cycle" in l)
        self.assertTrue(line.startswith("INFO"), line)

    def test_a_failed_cycle_still_logs_with_its_outcome(self):
        acroname.AcronameUSBNet._brainstem = _make_failing_brainstem([])
        with patch.object(acroname, "enumerate_usb_devices", return_value=[]):
            with self.assertLogs(acroname.logger, level="DEBUG") as cm:
                with self.assertRaises(acroname.DeviceNotFoundError):
                    self.net.states([0])
        line = next(l for l in cm.output if "cycle" in l)
        self.assertIn("-> DeviceNotFoundError", line)


class HubExitTraceTests(unittest.TestCase):
    """The opt-in exit trace, and the swallow it was added to open up.

    Two properties matter more than the text it prints. It must be INERT
    unless asked for, because it ships enabled-by-default nowhere; and it
    must never raise, because it runs inside the atexit hook whose whole
    purpose is that a passing script does not die during cleanup. A trace
    that throws at shutdown would cause the exact failure it exists to
    diagnose.
    """

    def _trace_to(self, stream, enabled):
        with patch.object(usb_net, "_EXIT_DEBUG", enabled), \
                patch.object(usb_net.sys, "stderr", stream):
            usb_net._exit_trace("hello")

    def test_the_exit_trace_is_silent_unless_enabled(self):
        stream = io.StringIO()
        self._trace_to(stream, enabled=False)
        self.assertEqual("", stream.getvalue())

    def test_the_exit_trace_writes_when_enabled(self):
        stream = io.StringIO()
        self._trace_to(stream, enabled=True)
        self.assertEqual("[hub-exit] hello\n", stream.getvalue())

    def test_the_exit_trace_survives_a_stderr_that_is_gone(self):
        """`sys.stderr` can be None or closed by the time atexit runs."""
        for stream in (None, _ClosedStream()):
            with self.subTest(stream=type(stream).__name__):
                self._trace_to(stream, enabled=True)  # must not raise

    def test_a_failed_disconnect_is_recorded_rather_than_discarded(self):
        """``_close_hub`` stays best-effort, but no longer silent.

        This is the innermost swallow on the teardown path: ``drain`` logs a
        failed close and could never see one, because this handler ate the
        exception first.
        """
        class _Boom:
            def disconnect(self):
                raise RuntimeError("hub went away")

        with self.assertLogs(acroname.logger, level="DEBUG") as caught:
            acroname.AcronameUSBNet._close_hub(_Boom())  # must not raise
        self.assertTrue(
            any("hub went away" in line for line in caught.output),
            f"disconnect failure was not recorded: {caught.output}",
        )


class _ClosedStream:
    """A stream that raises on write, like stderr after finalisation."""

    def write(self, _text):
        raise ValueError("I/O operation on closed file")

    def flush(self):
        raise ValueError("I/O operation on closed file")


if __name__ == "__main__":
    unittest.main()
