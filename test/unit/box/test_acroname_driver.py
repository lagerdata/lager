# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0
"""
Unit tests for box/lager/automation/usb_hub/acroname.py.

Same device-contention regression as the YKUSH driver: Acroname hubs were held
connected indefinitely (class-level _cached_hubs), pinning the exclusive USB
claim so another process could not connect. The fix opens a fresh connection
per operation, disconnects immediately after, and serialises the whole cycle
within and across processes via the shared `hub_access` lock.

BrainStem is stubbed, so no hardware is needed. (The real Acroname path still
needs a hardware smoke test before merge — see the plan.)
"""

import importlib.util
import os
import sys
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


_load_real("lager.util.device_lock", "lager/util/device_lock.py")
_load_real("lager.automation.usb_hub.usb_net", "lager/automation/usb_hub/usb_net.py")
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
        with self.assertRaises(acroname.DeviceNotFoundError) as ctx:
            self.net._open_hub()
        return str(ctx.exception)

    def test_message_keeps_its_prefix_and_serial(self):
        acroname.AcronameUSBNet._brainstem = _make_failing_brainstem([])
        msg = self._open()
        self.assertTrue(
            msg.startswith("No Acroname hub detected on USB with serial 0xBFABDDC4"),
            msg,
        )

    def test_scan_serials_are_in_the_message_when_none_match(self):
        """The evidence the original report could not produce: discovery
        returned a hub, just not this one."""
        acroname.AcronameUSBNet._brainstem = _make_failing_brainstem(
            [_FakeSpec(0x12345678)]
        )
        msg = self._open()
        self.assertIn("0x12345678", msg)
        self.assertIn("no serial match", msg)

    def test_find_all_raising_is_distinguishable_from_an_empty_scan(self):
        acroname.AcronameUSBNet._brainstem = _make_failing_brainstem(
            [], find_all_raises=RuntimeError("brainstem boom")
        )
        msg = self._open()
        self.assertIn("findAllModules raised RuntimeError", msg)
        self.assertIn("brainstem boom", msg)
        self.assertNotIn("0 spec(s)", msg)

    def test_an_empty_scan_says_so(self):
        acroname.AcronameUSBNet._brainstem = _make_failing_brainstem([])
        self.assertIn("findAllModules ok, 0 spec(s)", self._open())

    def test_connect_return_codes_appear_in_the_message(self):
        _RefusingHub.rc = 7
        acroname.AcronameUSBNet._brainstem = _make_failing_brainstem([])
        self.assertIn("rc=7", self._open())

    def test_a_raising_connect_from_spec_is_reported_not_swallowed(self):
        acroname.AcronameUSBNet._brainstem = _make_failing_brainstem(
            [_FakeSpec(0xBFABDDC4)], hub_cls=_ThrowingHub
        )
        msg = self._open()
        self.assertIn("connectFromSpec RuntimeError", msg)
        self.assertIn("usb transport went away", msg)

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

    def test_message_stays_bounded_on_a_crowded_bus(self):
        acroname.AcronameUSBNet._brainstem = _make_failing_brainstem(
            [_FakeSpec(0x1000 + i) for i in range(40)]
        )
        msg = self._open()
        self.assertLess(len(msg), 400, msg)
        self.assertIn("more", msg)


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
        with patch.object(acroname, "enumerate_usb_devices", return_value=devices):
            with self.assertRaises(acroname.DeviceNotFoundError) as ctx:
                self.net._open_hub()
        return str(ctx.exception)

    def test_hub_on_the_bus_but_not_discovered_says_exactly_that(self):
        msg = self._open_with_bus([
            {"vid": "24ff", "pid": "0011", "serial": "BFABDDC4"},
            {"vid": "24ff", "pid": "8011", "serial": None},
        ])
        self.assertIn("serial present on the bus", msg)
        self.assertIn("discovery did not return an enumerated hub", msg)

    def test_a_lowercase_sysfs_serial_still_matches(self):
        """sysfs hands back a string, the address parses to an int."""
        msg = self._open_with_bus(
            [{"vid": "24ff", "pid": "0011", "serial": "bfabddc4"}]
        )
        self.assertIn("serial present on the bus", msg)

    def test_other_acroname_devices_but_not_this_serial(self):
        msg = self._open_with_bus([
            {"vid": "24ff", "pid": "0013", "serial": "AAAA0001"},
            {"vid": "24ff", "pid": "8013", "serial": "AAAA0001"},
        ])
        self.assertIn("none with serial 0xBFABDDC4", msg)
        self.assertIn("24ff:0013", msg)

    def test_a_device_with_no_serial_descriptor_is_counted_not_ignored(self):
        """An empty iSerial must not read as 'the hub is absent'."""
        msg = self._open_with_bus([
            {"vid": "24ff", "pid": "8011", "serial": ""},
            {"vid": "24ff", "pid": "0013", "serial": "AAAA0001"},
        ])
        self.assertIn("1 with no serial descriptor", msg)

    def test_nothing_on_the_bus_says_nothing_on_the_bus(self):
        self.assertIn("no Acroname (24ff) device on the bus",
                      self._open_with_bus([]))

    def test_sysfs_failure_cannot_change_the_exception_type(self):
        """The guard-rail: a diagnostic must never replace the real error."""
        with patch.object(acroname, "enumerate_usb_devices",
                          side_effect=OSError("sysfs is gone")):
            with self.assertRaises(acroname.DeviceNotFoundError) as ctx:
                self.net._open_hub()
        msg = str(ctx.exception)
        self.assertIn("sysfs: unavailable", msg)
        self.assertNotIn("sysfs is gone", msg)

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


if __name__ == "__main__":
    unittest.main()
