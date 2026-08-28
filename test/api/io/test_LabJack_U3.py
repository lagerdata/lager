# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0
"""
Hardware smoke test for the LabJack UD-series (U3) drivers.

Unlike its T7 sibling, this does NOT need a box or any saved nets: it drives
``lager.io.*.labjack_ud`` directly against a U3 attached over USB. That makes it
runnable two ways:

    # 1. On any machine with a U3 plugged in (no box, no Docker):
    python test/api/io/test_LabJack_U3.py

    # 2. On a box, once the branch is deployed there:
    lager python test/api/io/test_LabJack_U3.py --box <BOX>

Prerequisites on a bare machine (the box image installs both already):

    Exodriver:      git clone https://github.com/labjack/exodriver.git
                    cd exodriver && sudo ./install.sh
    LabJackPython:  pip install 'LabJackPython==2.3.0'

Why this file exists in this shape
----------------------------------
The UD drivers were written against LabJackPython's source and LabJack's
datasheets, with no device available. Several behaviours were therefore taken
on documentation rather than observation, and every one of them is load-bearing
for a driver that otherwise fails *quietly*. The checks below are ordered so
the inferred ones come first and are labelled INFERRED -- if any of them fails,
the driver is wrong in a specific, named way rather than mysteriously.

Optional loopback wiring, enabled per-test by environment variable. Without
them the electrical round trips are skipped, not failed:

    U3_LOOPBACK_DAC_AIN=1   jumper DAC0 -> AIN0
                            AIN0 because it is a high-voltage input on a
                            U3-HV (+/-10.3 V); the DAC reaches 4.95 V, which
                            saturates a low-voltage channel's 2.44 V range.
    U3_LOOPBACK_GPIO=1      jumper FIO4 -> FIO5

WARNING: with the loopback flags set this drives DAC0 and FIO4. Disconnect
anything else on those pins first.
"""

import importlib.abc
import importlib.machinery
import os
import sys
import traceback
import types
from unittest.mock import MagicMock

# Make box/lager importable when running standalone from a worktree. Under
# `lager python` on a box the package is already on the path and this is a
# no-op.
_HERE = os.path.dirname(os.path.abspath(__file__))
_BOX = os.path.abspath(os.path.join(_HERE, "..", "..", "..", "box"))
if os.path.isdir(_BOX) and _BOX not in sys.path:
    sys.path.insert(0, _BOX)


class _StubLoader(importlib.abc.Loader):
    def create_module(self, spec):
        module = types.ModuleType(spec.name)
        module.__getattr__ = lambda attr: MagicMock()
        module.__path__ = []          # let submodules resolve too
        return module

    def exec_module(self, module):
        pass


class _AutoStubFinder(importlib.abc.MetaPathFinder):
    """Last-resort finder that stubs third-party modules the U3 path never uses.

    ``import lager.io`` executes ``lager/__init__``, which reaches
    ``nets/net.py`` and from there the entire instrument stack -- yaml,
    requests, pyvisa, phidget22, joulescope and more. None of it is on the U3
    path, but all of it has to import before the package will load, so on a
    machine with only LabJackPython installed this script would otherwise die
    on ``No module named 'yaml'`` before running a single check. Installing the
    whole box dependency set just to talk to a U3 is the wrong trade.

    Appended to ``sys.meta_path``, so it is consulted only after the real
    finders have failed: anything genuinely installed is imported for real.

    ``lager`` is deliberately excluded -- a missing lager module is a bug in
    this branch, not a gap in the environment, and stubbing it would turn a
    broken import into a passing test. ``u3``/``u6``/``LabJackPython`` are
    excluded because their absence is exactly what the first check reports.
    """

    REAL = {"lager", "u3", "u6", "LabJackPython"}

    def find_spec(self, fullname, path=None, target=None):
        if fullname.split(".")[0] in self.REAL:
            return None
        return importlib.machinery.ModuleSpec(
            fullname, _StubLoader(), is_package=True)


sys.meta_path.append(_AutoStubFinder())


_results = []


def check(name, inferred=False):
    """Decorator: run a check, record PASS/FAIL/SKIP, never raise."""
    def wrap(fn):
        label = ("INFERRED " if inferred else "") + name
        try:
            outcome = fn()
        except _Skip as s:
            _results.append(("SKIP", label, str(s)))
        except Exception as e:
            _results.append(("FAIL", label, f"{type(e).__name__}: {e}"))
            if os.environ.get("U3_TRACEBACK"):
                traceback.print_exc()
        else:
            _results.append(("PASS", label, outcome or ""))
        return fn
    return wrap


class _Skip(Exception):
    pass


def _report():
    """Print the collected results. Returns a process exit code."""
    if not _results:
        return 1
    width = max(len(label) for _, label, _ in _results)
    print()
    for status, label, detail in _results:
        print(f"{status:5s}  {label:<{width}}  {detail}")
    passed = sum(1 for s, _, _ in _results if s == "PASS")
    failed = sum(1 for s, _, _ in _results if s == "FAIL")
    skipped = sum(1 for s, _, _ in _results if s == "SKIP")
    print(f"\n{passed} passed, {failed} failed, {skipped} skipped")
    if failed:
        print("Re-run with U3_TRACEBACK=1 for full tracebacks.")
    return 1 if failed else 0


def main():
    from lager.io import labjack_ud_handle as udh
    from lager.io.adc.labjack_ud import LabJackUDADC
    from lager.io.dac.labjack_ud import LabJackUDDAC, LabJackUDDACError
    from lager.io.gpio.labjack_ud import LabJackUDGPIO

    # ---- connectivity -------------------------------------------------
    @check("Exodriver and LabJackPython import")
    def _():
        mod = udh.load_ud_module("u3")
        return f"u3 module at {mod.__file__}"

    @check("device opens (first found)")
    def _():
        dev = udh.get_ud_device("u3", None)
        return f"serial={getattr(dev, 'serialNumber', '?')}"

    # Everything below needs a live device. Stop here with an actionable
    # message rather than a traceback repeated by every later check.
    try:
        device = udh.get_ud_device("u3", None)
    except Exception as e:
        _report()
        print(f"\nCannot reach a U3: {type(e).__name__}: {e}\n")
        if "No module named" in str(e):
            print("LabJackPython is not installed. On this machine:\n"
                  "    pip install 'LabJackPython==2.3.0'\n"
                  "and install the Exodriver it needs:\n"
                  "    git clone https://github.com/labjack/exodriver.git\n"
                  "    cd exodriver && sudo ./install.sh")
        else:
            print("LabJackPython imported, so this is the device rather than\n"
                  "the install: check the U3 is plugged in, and on Linux that\n"
                  "the udev rule granting access has been applied.")
        return 1

    # ---- identity: the inferences ------------------------------------
    @check("device reports isHV / deviceName", inferred=True)
    def _():
        # Taken from u3.py's configU3: versionInfo 18 -> '-HV', isHV True.
        # If this attribute is missing, the FIO0-3 guard silently stops
        # protecting anything, because getattr(..., False) defaults to LV.
        if not hasattr(device, "isHV"):
            raise AssertionError(
                "u3.U3 has no isHV attribute -- the U3-HV FIO0-3 guard in "
                "labjack_ud_handle.set_channel_mode is inert")
        return f"deviceName={device.deviceName!r} isHV={device.isHV}"

    @check("this really is a U3-HV")
    def _():
        if not getattr(device, "isHV", False):
            raise _Skip("attached device is not an HV variant; "
                        "the HV-specific checks below will not apply")
        return "confirmed HV"

    @check("configU3 exposes the analog bitmasks", inferred=True)
    def _():
        cfg = device.configU3()
        for key in ("FIOAnalog", "EIOAnalog"):
            if key not in cfg:
                raise AssertionError(
                    f"configU3() has no {key} -- the pin mux cannot work")
        return (f"FIOAnalog=0b{cfg['FIOAnalog']:08b} "
                f"EIOAnalog=0b{cfg['EIOAnalog']:08b}")

    @check("U3-HV boots with FIO0-3 analog", inferred=True)
    def _():
        if not getattr(device, "isHV", False):
            raise _Skip("not an HV device")
        mask = device.configU3()["FIOAnalog"]
        if mask & 0x0F != 0x0F:
            raise AssertionError(
                f"expected FIO0-3 analog on a U3-HV, got 0b{mask:08b}")
        return f"FIOAnalog=0b{mask:08b}"

    @check("serial_from_address round-trips the real serial")
    def _():
        serial = str(device.serialNumber)
        addr = f"USB0::0x0CD5::0x0003::{serial}::INSTR"
        got = udh.serial_from_address(addr)
        if got != serial:
            raise AssertionError(f"expected {serial}, got {got}")
        return serial

    @check("device opens by explicit serial", inferred=True)
    def _():
        serial = str(device.serialNumber)
        udh.close_all_ud_devices()
        dev = udh.get_ud_device("u3", serial)
        if str(dev.serialNumber) != serial:
            raise AssertionError("opened a different device than requested")
        return f"reopened {serial}"

    # ---- pin mux ------------------------------------------------------
    @check("set_channel_mode flips a flexible line both ways", inferred=True)
    def _():
        dev = udh.get_ud_device("u3", None)
        udh.set_channel_mode(dev, 5, analog=True)
        after_analog = dev.configU3()["FIOAnalog"]
        if not after_analog & 0x20:
            raise AssertionError(
                f"FIO5 analog bit not set: 0b{after_analog:08b}")
        udh.set_channel_mode(dev, 5, analog=False)
        after_digital = dev.configU3()["FIOAnalog"]
        if after_digital & 0x20:
            raise AssertionError(
                f"FIO5 analog bit not cleared: 0b{after_digital:08b}")
        return f"0b{after_analog:08b} -> 0b{after_digital:08b}"

    @check("EIO mask bit is rebased by 8", inferred=True)
    def _():
        dev = udh.get_ud_device("u3", None)
        udh.set_channel_mode(dev, 8, analog=True)   # EIO0
        mask = dev.configU3()["EIOAnalog"]
        udh.set_channel_mode(dev, 8, analog=False)
        if not mask & 0x01:
            raise AssertionError(
                f"EIO0 should be bit 0 of EIOAnalog, got 0b{mask:08b}")
        return f"EIOAnalog=0b{mask:08b}"

    @check("U3-HV refuses FIO0 as digital")
    def _():
        if not getattr(device, "isHV", False):
            raise _Skip("not an HV device")
        dev = udh.get_ud_device("u3", None)
        try:
            udh.set_channel_mode(dev, 0, analog=False)
        except ValueError as e:
            return str(e)[:60]
        raise AssertionError("expected ValueError for FIO0 on a U3-HV")

    # ---- ADC ----------------------------------------------------------
    @check("read AIN0 (high-voltage channel)")
    def _():
        return f"{LabJackUDADC('u3-ain0', 'AIN0').input():.4f} V"

    @check("read AIN5 (flexible, switched to analog)")
    def _():
        return f"{LabJackUDADC('u3-ain5', 'AIN5').input():.4f} V"

    @check("out-of-range ADC channel is rejected")
    def _():
        try:
            LabJackUDADC("u3-bad", "AIN16").input()
        except ValueError:
            return "AIN16 rejected"
        raise AssertionError("expected ValueError for AIN16")

    # ---- DAC ----------------------------------------------------------
    @check("DAC16 write is accepted", inferred=True)
    def _():
        LabJackUDDAC("u3-dac0", "DAC0").output(2.5)
        return "wrote 2.5 V to DAC0"

    @check("DAC range bound rejects out-of-range values")
    def _():
        dac = LabJackUDDAC("u3-dac0", "DAC0")
        for bad in (0.0, 5.0):
            try:
                dac.output(bad)
            except ValueError:
                continue
            raise AssertionError(f"{bad} V should have been rejected")
        return "0.0 V and 5.0 V both rejected"

    @check("DAC readback without a write raises rather than inventing 0 V")
    def _():
        try:
            LabJackUDDAC("u3-dac1", "DAC1").get_voltage()
        except LabJackUDDACError:
            return "raised as designed"
        raise AssertionError("expected LabJackUDDACError")

    @check("DAC -> AIN loopback tracks across the range")
    def _():
        if os.environ.get("U3_LOOPBACK_DAC_AIN") != "1":
            raise _Skip("set U3_LOOPBACK_DAC_AIN=1 with DAC0 jumpered to AIN0")
        import time
        dac = LabJackUDDAC("u3-dac0", "DAC0")
        adc = LabJackUDADC("u3-ain0", "AIN0")
        worst, report = 0.0, []
        for target in (0.5, 1.5, 2.5, 3.5, 4.5):
            dac.output(target)
            time.sleep(0.05)
            measured = adc.input()
            worst = max(worst, abs(measured - target))
            report.append(f"{target}->{measured:.3f}")
        # 200 mV: the HV divider plus a 12-bit ADC over a 20 V span is ~5 mV
        # per count, so this is loose enough to pass on a correct board and
        # tight enough to catch a wrong DAC bit width or a wrong channel.
        if worst > 0.2:
            raise AssertionError(
                f"worst error {worst:.3f} V > 0.2 V: {', '.join(report)}")
        return f"worst {worst:.3f} V ({', '.join(report)})"

    # ---- GPIO ---------------------------------------------------------
    @check("GPIO write then read on one pin", inferred=True)
    def _():
        # BitStateWrite is documented to force the line to output, and
        # BitStateRead to leave direction alone. If either is wrong, this
        # is where it shows.
        gpio = LabJackUDGPIO("u3-fio4", "FIO4")
        gpio.output(1)
        high = gpio.input()
        gpio.output(0)
        low = gpio.input()
        if (high, low) != (1, 0):
            raise AssertionError(f"expected (1, 0), read ({high}, {low})")
        return "1 then 0 read back correctly"

    @check("GPIO loopback FIO4 -> FIO5")
    def _():
        if os.environ.get("U3_LOOPBACK_GPIO") != "1":
            raise _Skip("set U3_LOOPBACK_GPIO=1 with FIO4 jumpered to FIO5")
        out = LabJackUDGPIO("u3-fio4", "FIO4")
        inp = LabJackUDGPIO("u3-fio5", "FIO5")
        for level in (1, 0, 1):
            out.output(level)
            got = inp.input()
            if got != level:
                raise AssertionError(f"drove {level}, read {got}")
        return "levels propagated across the jumper"

    @check("U3-HV rejects FIO0 as a GPIO net")
    def _():
        if not getattr(device, "isHV", False):
            raise _Skip("not an HV device")
        try:
            LabJackUDGPIO("u3-fio0", "FIO0").input()
        except ValueError as e:
            return str(e)[:60]
        raise AssertionError("expected ValueError for FIO0 on a U3-HV")

    @check("wait_for_level polls and times out")
    def _():
        gpio = LabJackUDGPIO("u3-fio4", "FIO4")
        gpio.output(0)
        try:
            gpio.wait_for_level(1, timeout=0.3)
        except TimeoutError:
            return "TimeoutError raised, polling path reached"
        return "level already high (inconclusive but not a failure)"

    # ---- teardown -----------------------------------------------------
    @check("close_all_ud_devices releases the claim")
    def _():
        n = udh.close_all_ud_devices()
        return f"closed {n} device(s)"

    # ---- report -------------------------------------------------------
    return _report()


if __name__ == "__main__":
    sys.exit(main())
