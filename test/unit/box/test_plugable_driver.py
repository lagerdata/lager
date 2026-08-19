# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0
"""
Unit tests for box/lager/automation/usb_hub/plugable.py.

The Plugable dock has no vendor SDK, so the driver drives the STANDARD USB hub
class over pyusb. That makes the interesting failure modes different from the
other hub drivers, and these tests pin the ones that would otherwise be silent:

  * a hub that advertises GANGED switching must be refused, not obeyed --
    obeying it would cut every port while the caller asked for one;
  * a hub that accepts CLEAR_FEATURE but leaves VBUS up (no power FET) must
    raise, not report success;
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
                 lies=False, phantom=False, sysfs_flat=None):
        self.sysfs_flat = sysfs_flat
        self.nports = nports
        self.characteristics = characteristics
        self.power = {p: True for p in range(1, nports + 1)}
        self.connected = set(connected)
        self.lies = lies          # CLEAR_FEATURE accepted, power bit stays set
        self.phantom = phantom    # bit clears, but the device never leaves
        self.transfers = []
        self.disposed = 0

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
            w = 0
            if windex in self.connected:
                w |= 0x0001
            if self.power.get(windex):
                w |= 0x0100
            return bytes([w & 0xFF, w >> 8, 0, 0])
        if bm == 0x23 and req in (0x01, 0x03):
            on = req == 0x03
            if not (on or self.lies):
                self.power[windex] = False
                if not self.phantom:
                    self.connected.discard(windex)
                    if self.sysfs_flat is not None:
                        link = self.sysfs_flat / f"1-1.4.4.{windex}"
                        if link.is_symlink():
                            link.unlink()
            elif on:
                self.power[windex] = True
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

    def test_phantom_switch_detected_via_sysfs(self):
        # Power bit clears, but the device never leaves the bus: the FET is
        # absent. Only the sysfs check catches this.
        dev = FakeDevice(phantom=True, connected=(1,), sysfs_flat=self.flat)
        with self.assertRaises(PortStateError) as cm:
            self.run_with(dev, lambda: self.driver().disable(None, 1))
        self.assertIn("still enumerated", str(cm.exception))


class TestSafety(_Base):
    def test_refuses_port_carrying_a_network_device(self):
        drv = self.driver()
        with self.assertRaises(PortStateError) as cm:
            drv._guard("1-1.4", 3)          # Hub A port 3 = the dock's NIC
        self.assertIn("network device", str(cm.exception))

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


if __name__ == "__main__":
    unittest.main()
