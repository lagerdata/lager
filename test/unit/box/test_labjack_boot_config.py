# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

"""LabJack power-up (boot) levels: lager.io.gpio.labjack_boot_config.

A LabJack powers up with every DIO as an input with a pull-up, so a load on a
gpio net sees HIGH from power-on until lager drives the pin. These tests pin
how a T7's and a U3's boot configuration is checked and written from the
levels their nets declare.

The fakes model the two behaviors that make this easy to get wrong:
  * a T7 saves its whole *current* config, and cannot read stored DIO levels
    back -- so the fake's save copies the live registers into a stored set;
  * LabJackPython's configU3 sends 0 for every field of a written group that
    the call leaves out -- so the fake zeroes omitted fields, and a write that
    passes only the changed fields visibly clobbers the rest.
"""
import os
import tempfile
import unittest
from unittest.mock import MagicMock

from lager.io.gpio import labjack_boot_config as bc


class FakeLJM:
    """A T7 behind LJM's eReadName/eWriteName, with a stored boot config."""

    def __init__(self, direction=0, state=0x7FFFFF, factory=True, serial=470000001,
                 reject=()):
        self.live = {"DIO_DIRECTION": direction, "DIO_STATE": state}
        self.factory = factory
        self.serial = serial
        self.reject = set(reject)
        self.saved = None
        self.writes = []

    def eReadName(self, _h, name):
        if name == "SERIAL_NUMBER":
            return float(self.serial)
        if name == "IO_CONFIG_CHECK_FOR_FACTORY":
            return 1.0 if self.factory else 0.0
        return float(self.live[name])

    def eWriteName(self, _h, name, value):
        self.writes.append((name, value))
        if name in self.reject:
            raise RuntimeError(f"LJME_INVALID_NAME {name}")
        if name in bc.T7_SAVE_REGISTERS:
            self.saved = dict(self.live)
            self.factory = False
            return
        raise AssertionError(f"unexpected write {name}")

    def drive(self, dio, level):
        """What LabJackGPIO.output does to the live registers."""
        bit = 1 << dio
        self.live["DIO_DIRECTION"] |= bit
        if level == "high":
            self.live["DIO_STATE"] |= bit
        else:
            self.live["DIO_STATE"] &= ~bit
        self.writes.append((bc.t7_dio_name(dio), level))


class T7Tests(unittest.TestCase):
    def setUp(self):
        fd, self.record = tempfile.mkstemp(suffix=".json")
        os.close(fd)
        os.unlink(self.record)
        self.addCleanup(lambda: os.path.exists(self.record) and os.unlink(self.record))

    def _apply(self, ljm, declared, **kw):
        return bc.t7_apply(ljm, 1, declared, ljm.drive, record_path=self.record, **kw)

    def test_factory_t7_reports_every_declared_pin_as_a_pulled_up_input(self):
        plan = bc.t7_plan(FakeLJM(), 1, {3: "low", 2: "low"}, self.record)
        self.assertTrue(plan["factory"])
        self.assertFalse(plan["in_sync"])
        self.assertEqual({p["stored"] for p in plan["pins"].values()}, {"input"})

    def test_apply_drives_the_declared_pins_before_the_save(self):
        ljm = FakeLJM()
        report = self._apply(ljm, {3: "low", 1: "low", 2: "low"})
        names = [w[0] for w in ljm.writes]
        self.assertEqual(names[:3], ["FIO1", "FIO2", "FIO3"])
        self.assertEqual(names[3], "IO_CONFIG_SET_DEFAULT_TO_CURRENT")
        self.assertTrue(report["written"])
        self.assertTrue(report["in_sync"])
        self.assertEqual(ljm.saved["DIO_DIRECTION"], 0x0E)
        self.assertEqual(ljm.saved["DIO_STATE"] & 0x0E, 0)

    def test_second_apply_writes_no_flash(self):
        ljm = FakeLJM()
        self._apply(ljm, {3: "low"})
        ljm.writes.clear()
        report = self._apply(ljm, {3: "low"})
        self.assertFalse(report["written"])
        self.assertEqual(ljm.writes, [])

    def test_an_undeclared_pin_driven_later_is_not_drift(self):
        # Ordinary `lager gpo` on a net with no power-up level makes its pin an
        # output. That must not turn --check red: the declared levels are intact.
        ljm = FakeLJM()
        self._apply(ljm, {3: "low"})
        ljm.drive(8, "high")
        plan = bc.t7_plan(ljm, 1, {3: "low"}, self.record)
        self.assertTrue(plan["in_sync"])
        self.assertEqual(plan["undeclared_outputs"], ["EIO0"])

    def test_changing_a_level_writes_again(self):
        ljm = FakeLJM()
        self._apply(ljm, {3: "low"})
        report = self._apply(ljm, {3: "high"})
        self.assertTrue(report["written"])
        self.assertEqual(report["pins"][3]["stored"], "high")

    def test_refuses_while_an_undeclared_pin_is_an_output(self):
        ljm = FakeLJM(direction=1 << 8)  # EIO0 is an output no net declares
        with self.assertRaises(bc.BootConfigError) as cm:
            self._apply(ljm, {3: "low"})
        self.assertIn("EIO0", str(cm.exception))
        self.assertIsNone(ljm.saved)
        self.assertEqual(ljm.writes, [])  # not even the declared pin was driven

    def test_allow_undeclared_saves_them_as_they_are(self):
        ljm = FakeLJM(direction=1 << 8, state=0)
        report = self._apply(ljm, {3: "low"}, allow_undeclared=True)
        self.assertTrue(report["written"])
        self.assertEqual(ljm.saved["DIO_DIRECTION"], (1 << 8) | (1 << 3))

    def test_falls_back_to_the_bootup_register_name(self):
        ljm = FakeLJM(reject=("IO_CONFIG_SET_DEFAULT_TO_CURRENT",))
        self._apply(ljm, {3: "low"})
        self.assertIn(("IO_CONFIG_SET_BOOTUP_TO_CURRENT", 1), ljm.writes)

    def test_raises_when_no_save_register_is_accepted(self):
        ljm = FakeLJM(reject=bc.T7_SAVE_REGISTERS)
        with self.assertRaises(bc.BootConfigError) as cm:
            self._apply(ljm, {3: "low"})
        for name in bc.T7_SAVE_REGISTERS:
            self.assertIn(name, str(cm.exception))

    def test_custom_config_not_saved_by_lager_is_unknown_not_in_sync(self):
        plan = bc.t7_plan(FakeLJM(factory=False), 1, {3: "low"}, self.record)
        self.assertFalse(plan["stored_known"])
        self.assertEqual(plan["pins"][3]["stored"], "unknown")
        self.assertFalse(plan["in_sync"])

    def test_record_is_per_serial(self):
        ljm = FakeLJM(serial=111)
        self._apply(ljm, {3: "low"})
        other = bc.t7_plan(FakeLJM(serial=222, factory=False), 1, {3: "low"}, self.record)
        self.assertEqual(other["pins"][3]["stored"], "unknown")


class T7PinTests(unittest.TestCase):
    def test_named_and_numeric_pins(self):
        self.assertEqual(bc.t7_pin_to_dio("FIO3"), 3)
        self.assertEqual(bc.t7_pin_to_dio("eio2"), 10)
        self.assertEqual(bc.t7_pin_to_dio("CIO3"), 19)
        self.assertEqual(bc.t7_pin_to_dio("MIO2"), 22)
        self.assertEqual(bc.t7_pin_to_dio(5), 5)

    def test_rejects_what_the_t7_does_not_have(self):
        for pin in ("FIO8", "CIO4", "MIO3", 8, "AIN0", True):
            with self.assertRaises(ValueError, msg=pin):
                bc.t7_pin_to_dio(pin)


def _u3_cfg(**over):
    cfg = {"FIOAnalog": 0x0F, "FIODirection": 0, "FIOState": 0, "EIOAnalog": 0,
           "EIODirection": 0, "EIOState": 0, "CIODirection": 0, "CIOState": 0,
           "TimerCounterConfig": 0x40, "SerialNumber": 320000001}
    cfg.update(over)
    return cfg


class FakeU3:
    """configU3() reads; configU3(**fields) writes, zeroing omitted group fields."""

    _GROUPS = (("FIOAnalog", "EIOAnalog"),
               ("FIODirection", "FIOState", "EIODirection", "EIOState",
                "CIODirection", "CIOState"),
               ("TimerCounterConfig",))

    def __init__(self, cfg):
        self.cfg = dict(cfg)
        self.writes = []

    def configU3(self, **fields):
        if fields:
            self.writes.append(dict(fields))
            for group in self._GROUPS:
                if any(f in fields for f in group):
                    for f in group:
                        self.cfg[f] = int(fields.get(f, 0))
        return dict(self.cfg)


class U3Tests(unittest.TestCase):
    def test_writes_every_field_with_only_the_declared_bits_changed(self):
        dev = FakeU3(_u3_cfg())
        driven = []
        report = bc.u3_apply(dev, {10: "low", 11: "low"},
                             lambda dio, lv: driven.append((dio, lv)))
        self.assertEqual(driven, [(10, "low"), (11, "low")])
        self.assertTrue(report["written"])
        self.assertEqual(set(dev.writes[0]), set(bc.U3_FIELDS))
        # Unrelated fields survive the write, which a partial write would zero.
        self.assertEqual(dev.cfg["FIOAnalog"], 0x0F)
        self.assertEqual(dev.cfg["TimerCounterConfig"], 0x40)
        self.assertEqual(dev.cfg["EIODirection"], 0x0C)
        self.assertEqual(dev.cfg["EIOState"], 0)

    def test_a_declared_pin_stored_analog_is_switched_to_digital(self):
        dev = FakeU3(_u3_cfg(FIOAnalog=0xFF))
        bc.u3_apply(dev, {4: "low"}, lambda *_: None)
        self.assertEqual(dev.cfg["FIOAnalog"], 0xEF)
        self.assertEqual(dev.cfg["FIODirection"] & 0x10, 0x10)

    def test_stored_levels_are_read_back(self):
        plan = bc.u3_plan(FakeU3(_u3_cfg(EIODirection=0x04, EIOState=0x04,
                                         FIOAnalog=0x1F)),
                          {10: "high", 11: "low", 4: "low"})
        stored = {dio: p["stored"] for dio, p in plan["pins"].items()}
        self.assertEqual(stored, {4: "analog", 10: "high", 11: "input"})

    def test_in_sync_writes_nothing_but_still_drives(self):
        dev = FakeU3(_u3_cfg(EIODirection=0x0C))
        driven = []
        report = bc.u3_apply(dev, {10: "low", 11: "low"},
                             lambda dio, lv: driven.append(dio))
        self.assertFalse(report["written"])
        self.assertEqual(dev.writes, [])
        self.assertEqual(driven, [10, 11])

    def test_refuses_when_the_read_is_missing_a_field(self):
        cfg = _u3_cfg()
        del cfg["TimerCounterConfig"]
        with self.assertRaises(bc.BootConfigError):
            bc.u3_plan(FakeU3(cfg), {10: "low"})

    def test_a_readback_mismatch_is_an_error(self):
        dev = FakeU3(_u3_cfg())
        real = dev.configU3

        def stuck(**fields):
            if fields:
                return dict(dev.cfg)  # the write never lands
            return real()
        dev.configU3 = stuck
        with self.assertRaises(bc.BootConfigError):
            bc.u3_apply(dev, {10: "low"}, lambda *_: None)


class HelperTests(unittest.TestCase):
    def test_normalize_level_has_no_default(self):
        self.assertEqual(bc.normalize_level("LOW"), "low")
        self.assertIsNone(bc.normalize_level(None))
        for bad in ("", "0", "on", "input", 1):
            with self.assertRaises(ValueError, msg=bad):
                bc.normalize_level(bad)

    def test_two_nets_on_one_pin_must_agree(self):
        declared, names = bc.iter_declared([(3, "low", "A"), (3, "low", "B")])
        self.assertEqual(declared, {3: "low"})
        with self.assertRaises(bc.BootConfigError):
            bc.iter_declared([(3, "low", "A"), (3, "high", "B")])

    def test_public_report_keys_pins_by_net(self):
        plan = {"model": "T7", "in_sync": True, "_target": {},
                "pins": {3: {"dio_name": "FIO3", "declared": "low", "stored": "low"}}}
        out = bc.public(plan, {3: "PUMP"})
        self.assertNotIn("_target", out)
        self.assertEqual(out["pins"], [{"dio_name": "FIO3", "declared": "low",
                                        "stored": "low", "net": "PUMP", "dio": 3}])


class AdapterTests(unittest.TestCase):
    """labjack_boot_config_hs: nets resolve through the GPIO dispatcher."""

    def _adapter(self, resolved):
        import sys
        import types
        from unittest.mock import patch
        from lager import labjack_boot_config_hs as hs
        disp = types.ModuleType("lager.io.gpio.dispatcher")
        disp._dispatcher = MagicMock()
        disp._dispatcher._resolve_net_and_driver.side_effect = lambda n: resolved[n]
        for p in (patch.dict(sys.modules, {"lager.io.gpio.dispatcher": disp}),):
            p.start()
            self.addCleanup(p.stop)
        import lager.io.gpio as pkg
        q = patch.object(pkg, "dispatcher", disp, create=True)
        q.start()
        self.addCleanup(q.stop)
        return hs.LabJackBootConfigAdapter("PUMP")

    def test_t7_plan_uses_the_driver_handle(self):
        from lager.io.gpio.labjack_t7 import LabJackGPIO
        ljm = FakeLJM()
        drv = MagicMock(spec=LabJackGPIO)
        drv._get_ljm.return_value = ljm
        drv._get_handle.return_value = 1
        adapter = self._adapter({"PUMP": (drv, "FIO3")})
        report = adapter.run("plan", [{"name": "PUMP", "power_up": "low"},
                                      {"name": "OTHER", "power_up": None}])
        self.assertEqual(report["model"], "T7")
        self.assertEqual(report["pins"][0]["net"], "PUMP")
        self.assertEqual(report["pins"][0]["dio_name"], "FIO3")
        drv.output.assert_not_called()

    def test_a_non_labjack_driver_is_refused(self):
        adapter = self._adapter({"PUMP": (MagicMock(), 3)})
        with self.assertRaises(bc.BootConfigError):
            adapter.run("plan", [{"name": "PUMP", "power_up": "low"}])

    def test_unknown_mode_is_refused(self):
        with self.assertRaises(ValueError):
            self._adapter({}).run("write", [])


if __name__ == "__main__":
    unittest.main()
