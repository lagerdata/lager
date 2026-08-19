# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0
"""
Unit tests for box/lager/automation/usb_hub/plugable.py.

The Plugable dock has no vendor SDK, so the driver drives the STANDARD USB hub
class over pyusb. That makes the interesting failure modes different from the
other hub drivers, and these tests pin the ones that would otherwise be silent:

  * a hub that advertises GANGED switching must be refused, not obeyed --
    obeying it would cut every port while the caller asked for one;
  * a hub that accepts CLEAR_FEATURE but whose power bit never drops must
    raise, not report success;
  * a disable must NOT be judged by whether the device disappeared. While a
    port is unpowered the hub raises no change bit, so the kernel never
    processes the disconnect and the sysfs node persists for the whole off
    window. A driver that waits for it to vanish fails every successful
    power-down -- and the version that did also undid it. See
    TestDisableIsNotJudgedByPresence;
  * a cycle must restore power on EVERY path, including failure, because a
    port left dark strands a bench nobody can reach;
  * a port whose subtree carries a network device must be refused, because
    cutting it can drop the box off the network with no way back;
  * the two cascaded hubs of ONE dock must share ONE lock key.

Loads the REAL usb_net + device_lock (so hub_access is genuinely exercised)
against a fake sysfs tree and a fake pyusb, so it needs no hardware.
"""

import importlib.util
import os
import sys
import types
import unittest
from unittest import mock

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
_load_real("lager.util.watchdog", "lager/util/watchdog.py")
usb_net = _load_real("lager.automation.usb_hub.usb_net",
                     "lager/automation/usb_hub/usb_net.py")
plugable = _load_real("lager.automation.usb_hub.plugable",
                      "lager/automation/usb_hub/plugable.py")

for _pkg in _created_pkg_stubs:
    sys.modules.pop(_pkg, None)

PortStateError = usb_net.PortStateError
DeviceNotFoundError = usb_net.DeviceNotFoundError


# ────────────────────────  fake sysfs  ────────────────────────
# Mirrors real /sys/bus/usb/devices: a flat directory of symlinks into a nested
# /sys/devices tree. The nesting is what makes the driver's realpath-based
# parent lookup meaningful -- a flat fixture would pass while the real thing
# resolved every parent to the same directory.
#
# Shape is the confirmed UD-CAM topology: Hub A entirely internal (billboard,
# audio, NIC, inter-hub link), Hub B carrying the four external sockets.
DOCK = {
    "1-1":       ("05e3", "0610", 1, 2),    # intermediate Genesys hub
    "1-1.4":     ("2230", "5411", 1, 15),   # HUB A - root tier
    "1-1.4.1":   ("0bda", "5440", 1, 16),   # billboard
    "1-1.4.3":   ("0bda", "8153", 1, 17),   # the dock's own NIC
    "1-1.4.4":   ("2230", "5411", 1, 18),   # HUB B - user tier
    "1-1.4.4.1": ("1366", "0101", 1, 20),   # J-Link
    "1-1.4.4.4": ("10c4", "ea60", 1, 19),   # CP210x
}
IFACES = {"1-1.4.3:1.0": "r8152", "1-1.4.4.4:1.0": "cp210x"}


def build_sysfs(root, devices=DOCK, ifaces=IFACES):
    nested, flat = root / "devices", root / "bus"
    flat.mkdir(parents=True)
    for name, (vid, pid, bus, dev) in sorted(devices.items(), key=lambda kv: len(kv[0])):
        parts = name.split(".")
        d = nested / "usb1"
        for i in range(len(parts)):
            d = d / (".".join(parts[: i + 1]))
        d.mkdir(parents=True, exist_ok=True)
        (d / "idVendor").write_text(vid + "\n")
        (d / "idProduct").write_text(pid + "\n")
        (d / "busnum").write_text(f"{bus}\n")
        (d / "devnum").write_text(f"{dev}\n")
        os.symlink(d, flat / name)
    for iname, driver in ifaces.items():
        parent = iname.split(":")[0]
        parts = parent.split(".")
        d = nested / "usb1"
        for i in range(len(parts)):
            d = d / (".".join(parts[: i + 1]))
        idir = d / iname
        idir.mkdir(parents=True, exist_ok=True)
        drv = nested / "drivers" / driver
        drv.mkdir(parents=True, exist_ok=True)
        os.symlink(drv, idir / "driver")
        os.symlink(idir, flat / iname)
    return flat


# ────────────────────────  fake pyusb  ────────────────────────
class FakeDevice:
    """Decodes the four real hub-class requests against an in-memory model."""

    def __init__(self, nports=4, characteristics=0x00a9, connected=(1, 4),
                 lies=False, sysfs_flat=None, reconnect_polls=1,
                 fail_repower=False):
        self.sysfs_flat = sysfs_flat
        self.nports = nports
        self.characteristics = characteristics
        self.power = {p: True for p in range(1, nports + 1)}
        self.connected = set(connected)
        self.occupied = set(connected)   # what is PHYSICALLY plugged in
        self.change = {p: 0 for p in range(1, nports + 1)}
        self.lies = lies          # CLEAR_FEATURE accepted, power bit stays set
        # Polls of GET_STATUS after re-power before the device reappears, so a
        # test can exercise the wait loop rather than always hitting it on the
        # first read.
        self.reconnect_polls = reconnect_polls
        self._pending = {}
        self.fail_repower = fail_repower
        self.transfers = []
        self.disposed = 0

    # NOTE: powering a port down deliberately does NOT remove the sysfs node.
    # That is what the real kernel does -- with the port unpowered the hub
    # raises no change bit, so the disconnect is never processed and the node
    # (and its /dev/ttyUSB*) survive the whole off window. Modelling the node
    # as vanishing is what made the driver's original presence check look
    # correct in tests while failing on every real power-down.

    # These two would tear down the whole downstream tree. Nothing may call them.
    def set_configuration(self, *a, **k):
        raise AssertionError("set_configuration() must never be called on a hub")

    def detach_kernel_driver(self, *a, **k):
        raise AssertionError("detach_kernel_driver() must never be called on a hub")

    def ctrl_transfer(self, bm, req, wvalue, windex, data):
        self.transfers.append((bm, req, wvalue, windex))
        if bm == 0xA0 and req == 0x06:
            return bytes([9, 0x29, self.nports,
                          self.characteristics & 0xFF, self.characteristics >> 8,
                          0, 0, 0, 0])
        if bm == 0xA3 and req == 0x00:
            # A pending re-enumeration counts down on each poll of that port.
            if windex in self._pending:
                self._pending[windex] -= 1
                if self._pending[windex] <= 0:
                    del self._pending[windex]
                    if windex in self.occupied:
                        self.connected.add(windex)
                        self.change[windex] |= 0x0001   # C_PORT_CONNECTION
            w = 0
            if windex in self.connected:
                w |= 0x0001
            if self.power.get(windex):
                w |= 0x0100
            c = self.change.get(windex, 0)
            return bytes([w & 0xFF, w >> 8, c & 0xFF, c >> 8])
        if bm == 0x23 and req in (0x01, 0x03):
            on = req == 0x03
            if on:
                if self.fail_repower:
                    raise OSError("simulated re-power failure")
                self.power[windex] = True
                self._pending[windex] = self.reconnect_polls
            elif not self.lies:
                self.power[windex] = False
                # An unpowered port reports neither connect nor enable, and
                # raises no change bit -- so the kernel never notices.
                self.connected.discard(windex)
                self.change[windex] = 0
                self._pending.pop(windex, None)
            return None
        raise AssertionError(f"unexpected control transfer {bm:#x}/{req:#x}")

    def feature_calls(self):
        return [t for t in self.transfers if t[0] == 0x23]


def install_fake_usb(device):
    """Inject a fake `usb` package in place of the real one."""
    core = types.ModuleType("usb.core")
    util = types.ModuleType("usb.util")
    pkg = types.ModuleType("usb")
    core.find = lambda **kw: device
    core.USBError = OSError

    def dispose(dev):
        dev.disposed += 1

    util.dispose_resources = dispose
    pkg.core, pkg.util = core, util
    return mock.patch.dict(sys.modules,
                           {"usb": pkg, "usb.core": core, "usb.util": util})


ADDR = "USB0::0x2230::0x5411::port-1-1.4::INSTR"


class _Base(unittest.TestCase):
    def setUp(self):
        import tempfile, pathlib, shutil
        self._tmp = pathlib.Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self._tmp, ignore_errors=True)
        self.flat = build_sysfs(self._tmp)
        patcher = mock.patch.object(plugable, "_SYS_USB", self.flat)
        patcher.start()
        self.addCleanup(patcher.stop)

    def driver(self, **kw):
        return plugable.PlugableUSBNet({"address": ADDR, **kw})

    def run_with(self, dev, fn):
        with install_fake_usb(dev):
            return fn()


class TestAddressing(_Base):
    def test_topology_path_parsed_and_serial_ignored(self):
        self.assertEqual(plugable.path_from_address(ADDR), "1-1.4")
        self.assertIsNone(plugable.serial_from_address(ADDR))

    def test_real_serial_address_still_works(self):
        a = "USB0::0x2230::0x5411::ABC123::INSTR"
        self.assertIsNone(plugable.path_from_address(a))
        self.assertEqual(plugable.serial_from_address(a), "ABC123")

    def test_lock_key_is_the_dock_not_the_hub_instance(self):
        # Every net on one dock -- whichever tier its port physically lands on
        # -- must serialise on ONE key, or two processes drive one dock at once.
        keys = {self.driver()._lock_key() for _ in range(3)}
        self.assertEqual(len(keys), 1)
        self.assertIn("1-1.4", keys.pop())


class TestTopology(_Base):
    def test_resolves_root_and_downstream_tiers(self):
        dock = self.driver()._resolve_dock()
        self.assertEqual(dock["root"].sysfs, "1-1.4")
        self.assertEqual(dock["downstream"].sysfs, "1-1.4.4")

    def test_missing_dock_raises_device_not_found(self):
        for name in ("1-1.4", "1-1.4.4"):
            (self.flat / name).unlink()
        with self.assertRaises(DeviceNotFoundError):
            self.driver()._resolve_dock()

    def test_unexpected_shape_is_reported_not_guessed(self):
        (self.flat / "1-1.4.4").unlink()          # no downstream tier
        with self.assertRaises(DeviceNotFoundError) as cm:
            self.driver()._resolve_dock()
        self.assertIn("validated shape", str(cm.exception))


class TestCapabilityGate(_Base):
    def test_ganged_hub_refused_and_no_port_touched(self):
        dev = FakeDevice(characteristics=0x0008, sysfs_flat=self.flat)  # LPSM 00
        with self.assertRaises(PortStateError) as cm:
            self.run_with(dev, lambda: self.driver().disable(None, 2))
        self.assertIn("ganged", str(cm.exception))
        # The critical half: refusing must not have switched anything first.
        self.assertEqual(dev.feature_calls(), [])

    def test_no_power_switching_refused(self):
        dev = FakeDevice(characteristics=0x000a, sysfs_flat=self.flat)  # LPSM 10
        with self.assertRaises(PortStateError):
            self.run_with(dev, lambda: self.driver().disable(None, 2))
        self.assertEqual(dev.feature_calls(), [])

    def test_hub_that_lies_about_switching_raises(self):
        dev = FakeDevice(lies=True, sysfs_flat=self.flat)
        with self.assertRaises(PortStateError) as cm:
            self.run_with(dev, lambda: self.driver().disable(None, 2))
        self.assertIn("does not honour", str(cm.exception))

    def test_a_hub_that_lies_is_not_second_guessed(self):
        # The surviving failure: the power bit reads back set. The port is
        # therefore still powered, so there is nothing to restore -- the driver
        # must NOT issue a compensating SET_FEATURE. (An earlier version did,
        # because it also treated "the device is still enumerated" as a
        # failure, which is not observable while a port is off.)
        dev = FakeDevice(lies=True, connected=(1,), sysfs_flat=self.flat)
        with self.assertRaises(PortStateError):
            self.run_with(dev, lambda: self.driver().disable(None, 1))
        self.assertEqual(
            [t for t in dev.feature_calls() if t[1] == 0x03], [],
            "nothing to restore on a port that never lost power")


class TestDisableIsNotJudgedByPresence(_Base):
    """The regression that made this dock look unsupportable.

    With a port unpowered the hub raises no change bit, so Linux never polls
    the port and never processes the disconnect: the sysfs node, the lsusb
    line, and any /dev/ttyUSB* all persist for the entire off window. A driver
    that waits for the device to disappear waits forever, reports a working
    hub as having no VBUS FET, and (in the version that shipped on this branch)
    powered the port back up again.
    """

    def test_disable_succeeds_while_the_device_node_persists(self):
        dev = FakeDevice(connected=(1, 4), sysfs_flat=self.flat)
        node = self.flat / "1-1.4.4.1"
        self.assertTrue(node.exists(), "fixture precondition")

        self.run_with(dev, lambda: self.driver().disable(None, 1))

        self.assertTrue(node.exists(),
                        "the kernel cannot see the disconnect, so the node stays")
        self.assertFalse(dev.power[1], "and the port must be LEFT unpowered")

    def test_disable_does_not_re_power_the_port(self):
        # The specific undo that made `lager usb <net> disable` a no-op.
        dev = FakeDevice(connected=(1,), sysfs_flat=self.flat)
        self.run_with(dev, lambda: self.driver().disable(None, 1))
        self.assertEqual(dev.feature_calls(), [(0x23, 0x01, 8, 1)],
                         "exactly one CLEAR_FEATURE and no SET_FEATURE after it")

    def test_disable_of_an_empty_port_is_not_special_cased(self):
        dev = FakeDevice(connected=(), sysfs_flat=self.flat)
        self.run_with(dev, lambda: self.driver().disable(None, 2))
        self.assertFalse(dev.power[2])

    def test_disable_reports_that_a_device_was_attached(self):
        # Drives the "the device stays listed in lsusb" warning. Without it,
        # every reader checks lsusb, sees the device, and concludes the switch
        # failed -- which is exactly how this driver shipped broken.
        dev = FakeDevice(connected=(1,), sysfs_flat=self.flat)
        had = self.run_with(dev, lambda: self.driver().disable(None, 1))
        self.assertIs(had, True)

    def test_disable_reports_nothing_attached_for_a_bare_port(self):
        dev = FakeDevice(connected=(), sysfs_flat=self.flat)
        had = self.run_with(dev, lambda: self.driver().disable(None, 2))
        self.assertIs(had, False)

    def test_the_attached_check_reads_before_it_switches(self):
        # Reading it AFTER clearing PORT_POWER would always report False: an
        # unpowered port reports no connect. Order is the whole test.
        dev = FakeDevice(connected=(1,), sysfs_flat=self.flat)
        self.run_with(dev, lambda: self.driver().disable(None, 1))
        kinds = [t[0] for t in dev.transfers]
        self.assertLess(kinds.index(0xA3), kinds.index(0x23),
                        "GET_STATUS must precede CLEAR_FEATURE")


class TestSafety(_Base):
    def test_refuses_port_carrying_a_network_device(self):
        drv = self.driver()
        with self.assertRaises(PortStateError) as cm:
            drv._guard("1-1.4", 3)          # Hub A port 3 = the dock's NIC
        self.assertIn("network device", str(cm.exception))

    def test_the_refusal_message_does_not_depend_on_sysfs_order(self):
        # Regression from CI. sysfs iteration order is filesystem-dependent and
        # _guard stops at its first match, so this port refused with the
        # driver-based message on one OS and the vid:pid one on another. The
        # test above pinned the first, passed on a laptop, and failed in CI.
        # Interfaces must sort ahead of devices so the message is stable.
        entries = self.driver()._subtree("1-1.4", 3)
        self.assertEqual(entries, ["1-1.4.3:1.0", "1-1.4.3"])

    def test_refuses_port_feeding_the_second_hub_tier(self):
        with self.assertRaises(PortStateError) as cm:
            self.driver()._guard("1-1.4", 4)
        self.assertIn("second hub tier", str(cm.exception))

    def test_allow_network_override_skips_the_guard(self):
        dev = FakeDevice(connected=(1, 4), sysfs_flat=self.flat)
        drv = plugable.PlugableUSBNet(
            {"address": ADDR, "params": {"allow_network": True}})
        self.assertTrue(drv.allow_network)
        self.run_with(dev, lambda: drv.disable(None, 4))
        self.assertTrue(dev.feature_calls())

    def test_user_ports_are_clean(self):
        # The whole point of exposing only Hub B: no user port carries the NIC
        # or the inter-hub link, so the guard never fires on the normal path.
        drv = self.driver()
        for port in plugable._USER_PORTS:
            drv._guard("1-1.4.4", port)


class TestControlTransfers(_Base):
    def test_disable_issues_clear_feature_on_the_right_port(self):
        dev = FakeDevice(sysfs_flat=self.flat)
        self.run_with(dev, lambda: self.driver().disable(None, 4))
        self.assertIn((0x23, 0x01, 8, 4), dev.feature_calls())

    def test_enable_issues_set_feature(self):
        dev = FakeDevice(sysfs_flat=self.flat)
        self.run_with(dev, lambda: self.driver().enable(None, 2))
        self.assertIn((0x23, 0x03, 8, 2), dev.feature_calls())

    def test_state_reads_power_bit(self):
        dev = FakeDevice(sysfs_flat=self.flat)
        self.assertTrue(self.run_with(dev, lambda: self.driver().state(None, 2)))
        dev.power[2] = False
        self.assertFalse(self.run_with(dev, lambda: self.driver().state(None, 2)))
        self.assertEqual(dev.feature_calls(), [])       # state must not write

    def test_port_outside_user_range_rejected_without_any_transfer(self):
        dev = FakeDevice(sysfs_flat=self.flat)
        for bad in (0, 5, 99):
            with self.assertRaises(PortStateError):
                self.run_with(dev, lambda: self.driver().disable(None, bad))
        self.assertEqual(dev.transfers, [])

    def test_states_reads_all_ports_in_one_session(self):
        dev = FakeDevice(sysfs_flat=self.flat)
        out = self.run_with(dev, lambda: self.driver().states([1, 2, 3, 4]))
        self.assertEqual(set(out), {1, 2, 3, 4})
        self.assertEqual(dev.disposed, 1, "one session, not one per port")
        self.assertEqual(len([t for t in dev.transfers if t[0] == 0xA0]), 1)


class TestHandleDiscipline(_Base):
    def test_handle_disposed_after_every_operation(self):
        dev = FakeDevice(sysfs_flat=self.flat)
        self.run_with(dev, lambda: self.driver().state(None, 1))
        self.run_with(dev, lambda: self.driver().enable(None, 2))
        self.assertEqual(dev.disposed, 2)

    def test_handle_disposed_even_when_the_operation_fails(self):
        dev = FakeDevice(characteristics=0x0008, sysfs_flat=self.flat)
        with self.assertRaises(PortStateError):
            self.run_with(dev, lambda: self.driver().disable(None, 2))
        self.assertEqual(dev.disposed, 1)

    def test_driver_declares_it_holds_no_usb_context(self):
        # Gates self_restart: restarting box_http_server for a driver that opens
        # and closes per call drops every other in-flight operation to fix
        # nothing (see usb_net.USBNet.holds_usb_context_between_ops).
        self.assertFalse(plugable.PlugableUSBNet.holds_usb_context_between_ops)

    def test_permission_error_names_the_udev_remedy(self):
        dev = FakeDevice(sysfs_flat=self.flat)
        err = OSError("Access denied")
        err.errno = 13
        dev.ctrl_transfer = mock.Mock(side_effect=err)
        with self.assertRaises(PortStateError) as cm:
            self.run_with(dev, lambda: self.driver().state(None, 1))
        self.assertIn("udev", str(cm.exception))


class TestCycle(_Base):
    """cycle() is the only operation that can prove VBUS really drops, because
    it is the only one that observes the re-power edge."""

    def setUp(self):
        super().setUp()
        # No real sleeping: off_time and the reconnect poll would otherwise put
        # seconds of wall time into the suite.
        patcher = mock.patch.object(plugable.time, "sleep", lambda _s: None)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_clears_then_sets_power_on_the_same_port(self):
        dev = FakeDevice(connected=(1,), sysfs_flat=self.flat)
        self.run_with(dev, lambda: self.driver().cycle(None, 1))
        self.assertEqual(dev.feature_calls(),
                         [(0x23, 0x01, 8, 1), (0x23, 0x03, 8, 1)])
        self.assertTrue(dev.power[1], "a cycle always ends powered")

    def test_reports_the_device_came_back(self):
        dev = FakeDevice(connected=(1,), sysfs_flat=self.flat, reconnect_polls=3)
        got = self.run_with(dev, lambda: self.driver().cycle(None, 1))
        self.assertIs(got, True)

    def test_reconnect_timeout_reports_rather_than_raises(self):
        # The device never comes back. Power is on, which is the safe state, so
        # this is information -- not an error that would mask it.
        dev = FakeDevice(connected=(1,), sysfs_flat=self.flat)
        dev.occupied = set()          # unplugged while it was dark
        # Shortened so the test does not spend the real 5s budget waiting; the
        # budget itself is pinned in TestOffTimeContract.
        with mock.patch.object(plugable, "_RECONNECT_WAIT_S", 0.02):
            got = self.run_with(dev, lambda: self.driver().cycle(None, 1))
        self.assertIs(got, False)
        self.assertTrue(dev.power[1])

    def test_empty_port_reports_none(self):
        dev = FakeDevice(connected=(), sysfs_flat=self.flat)
        got = self.run_with(dev, lambda: self.driver().cycle(None, 2))
        self.assertIsNone(got, "nothing was there, so nothing came back")

    def test_power_is_restored_when_the_off_phase_raises(self):
        # A port left dark strands a bench nobody can reach physically. Whatever
        # goes wrong between the two transfers, power must come back.
        dev = FakeDevice(connected=(1,), sysfs_flat=self.flat)
        boom = RuntimeError("interrupted mid-cycle")
        with mock.patch.object(plugable.time, "sleep", side_effect=boom):
            with self.assertRaises(PortStateError):
                self.run_with(dev, lambda: self.driver().cycle(None, 1))
        self.assertTrue(dev.power[1], "PORT_POWER must be restored on failure")
        self.assertEqual(dev.feature_calls()[-1], (0x23, 0x03, 8, 1))

    def test_a_failed_re_power_is_reported_loudly(self):
        dev = FakeDevice(connected=(1,), sysfs_flat=self.flat, fail_repower=True)
        with self.assertRaises(PortStateError) as cm:
            self.run_with(dev, lambda: self.driver().cycle(None, 1))
        self.assertIn("recover", str(cm.exception),
                      "must point at the way out, not just fail")

    def test_off_time_below_the_minimum_is_rejected_before_any_transfer(self):
        dev = FakeDevice(connected=(1,), sysfs_flat=self.flat)
        with self.assertRaises(PortStateError) as cm:
            self.run_with(dev, lambda: self.driver().cycle(None, 1, 0.1))
        self.assertIn("outside the supported range", str(cm.exception))
        self.assertEqual(dev.transfers, [], "nothing may be switched first")

    def test_off_time_above_the_maximum_is_rejected(self):
        dev = FakeDevice(connected=(1,), sysfs_flat=self.flat)
        with self.assertRaises(PortStateError):
            self.run_with(dev, lambda: self.driver().cycle(None, 1, 60))
        self.assertEqual(dev.transfers, [])

    def test_non_numeric_off_time_is_rejected(self):
        dev = FakeDevice(connected=(1,), sysfs_flat=self.flat)
        with self.assertRaises(PortStateError):
            self.run_with(dev, lambda: self.driver().cycle(None, 1, "soon"))
        self.assertEqual(dev.transfers, [])

    def test_whole_cycle_runs_in_one_session(self):
        # Both halves of the cycle must happen under ONE lock: if the port is
        # released while dark, another caller can switch it mid-cycle.
        dev = FakeDevice(connected=(1,), sysfs_flat=self.flat)
        self.run_with(dev, lambda: self.driver().cycle(None, 1))
        self.assertEqual(dev.disposed, 1)
        self.assertEqual(len([t for t in dev.transfers if t[0] == 0xA0]), 1)

    def test_network_port_guard_still_applies(self):
        drv = self.driver()
        with self.assertRaises(PortStateError):
            drv._guard("1-1.4", 3)


class TestRecover(_Base):
    def test_repowers_every_user_port(self):
        dev = FakeDevice(connected=(1, 4), sysfs_flat=self.flat)
        dev.power = {p: False for p in (1, 2, 3, 4)}
        got = self.run_with(dev, lambda: self.driver().recover("usb1", 1))
        self.assertEqual(got, [1, 2, 3, 4])
        self.assertTrue(all(dev.power.values()))
        self.assertEqual([t[1] for t in dev.feature_calls()], [0x03] * 4)

    def test_recover_is_one_session(self):
        dev = FakeDevice(sysfs_flat=self.flat)
        self.run_with(dev, lambda: self.driver().recover("usb1", 1))
        self.assertEqual(dev.disposed, 1)


class TestSuperSpeedCompanion(_Base):
    """UNTESTED against hardware -- the bench dock links at USB 2.0 only.

    These pin the decisions the code makes, which is what stops the SuperSpeed
    path from silently half-working: clearing PORT_POWER on one half of a
    SuperSpeed hub leaves VBUS up.
    """

    def _hub(self, sysfs, speed, bus=1, dev=18):
        return plugable._HubRef(sysfs, bus, dev, "1-1.4", speed)

    def test_high_speed_hub_with_no_companion_proceeds(self):
        # The measured, working topology. Must not regress into a refusal.
        ref = self._hub("1-1.4.4", 480.0)
        self.assertIsNone(plugable.PlugableUSBNet._companion_of(ref, [ref]))

    def test_superspeed_hub_without_a_companion_is_refused(self):
        ref = self._hub("1-1.4.4", 5000.0)
        with self.assertRaises(PortStateError) as cm:
            plugable.PlugableUSBNet._companion_of(ref, [ref])
        self.assertIn("companion", str(cm.exception))

    def test_ambiguous_pairing_is_refused_not_guessed(self):
        ref = self._hub("1-1.4.4", 480.0)
        twins = [self._hub("2-1.4.4", 5000.0, bus=2),
                 self._hub("3-1.4.4", 10000.0, bus=3)]
        with mock.patch.object(plugable, "_companion_candidates",
                               return_value=twins):
            with self.assertRaises(PortStateError) as cm:
                plugable.PlugableUSBNet._companion_of(ref, [ref] + twins)
        self.assertIn("refusing", str(cm.exception))

    def test_both_halves_are_switched_together(self):
        ref = self._hub("1-1.4.4", 480.0)
        twin = self._hub("2-1.4.4", 5000.0, bus=2, dev=4)
        dock = {"root": self._hub("1-1.4", 480.0), "downstream": ref,
                "companion": twin}
        self.assertEqual([r.sysfs for r in plugable.PlugableUSBNet._targets(dock)],
                         ["1-1.4.4", "2-1.4.4"])

    def test_mismatched_port_counts_are_refused(self):
        drv = self.driver()
        hubs = [plugable._Hub(None, 4, 0x00a9, "1-1.4.4"),
                plugable._Hub(None, 2, 0x00a9, "2-1.4.4")]
        usb = types.SimpleNamespace(
            core=types.SimpleNamespace(find=lambda **kw: None),
            util=types.SimpleNamespace(dispose_resources=lambda d: None))
        with mock.patch.object(drv, "_open_one", side_effect=hubs), \
             mock.patch.object(plugable, "_require_pyusb", return_value=usb):
            with self.assertRaises(PortStateError) as cm:
                drv._with_hubs([object(), object()], lambda h: None)
        self.assertIn("different port counts", str(cm.exception))

    def test_a_missing_bus_root_pairs_nothing(self):
        # realpath() normalises a path that is not there, so without an
        # existence check every bus resolves to the same parent directory and
        # two unrelated hubs pair up. The fixture has no usbN root hubs, so this
        # is the real shape of that failure.
        self.assertIsNone(plugable._bus_controller(1))
        ref = self._hub("1-1.4.4", 480.0)
        twin = self._hub("2-1.4.4", 5000.0, bus=2)
        self.assertEqual(plugable._companion_candidates(ref, [ref, twin]), [])

    def test_a_port_is_powered_only_if_every_half_says_so(self):
        class _H:
            def __init__(self, on):
                self._on = on
                self.sysfs = "x"

            def port_status(self, port):
                return 0x0100 if self._on else 0x0000

        powered = plugable.PlugableUSBNet._powered
        self.assertTrue(powered([_H(True), _H(True)], 1))
        self.assertFalse(powered([_H(True), _H(False)], 1))


class TestOffTimeContract(unittest.TestCase):
    def test_default_is_above_the_slowest_measured_cold_boot(self):
        # A J-Link PLUS reasserts CONNECT 323 ms after power returns.
        self.assertGreaterEqual(usb_net.USB_CYCLE_OFF_TIME_S, 0.5)
        self.assertEqual(usb_net.validate_off_time(None),
                         usb_net.USB_CYCLE_OFF_TIME_S)

    def test_range_ends_are_inclusive(self):
        self.assertEqual(usb_net.validate_off_time(usb_net.USB_CYCLE_MIN_OFF_TIME_S),
                         usb_net.USB_CYCLE_MIN_OFF_TIME_S)
        self.assertEqual(usb_net.validate_off_time(usb_net.USB_CYCLE_MAX_OFF_TIME_S),
                         usb_net.USB_CYCLE_MAX_OFF_TIME_S)

    def test_max_off_time_fits_inside_the_hub_deadline(self):
        # Otherwise a legal off-time produces a 504 instead of a power-cycle.
        worst = (usb_net.USB_CYCLE_MAX_OFF_TIME_S + plugable._RECONNECT_WAIT_S)
        self.assertLess(worst, usb_net.HUB_OP_TIMEOUT_S)

    def test_rejects_nan_and_infinity(self):
        for bad in (float("nan"), float("inf"), float("-inf")):
            with self.assertRaises(PortStateError):
                usb_net.validate_off_time(bad)


if __name__ == "__main__":
    unittest.main()
