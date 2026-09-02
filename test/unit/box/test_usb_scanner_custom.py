# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for custom-device surfacing in the box HTTP scanner.

``lager.http_handlers.usb_scanner.list_instruments`` (served by GET
/instruments/list) mirrors ``cli/impl/query_instruments.py``: live
custom-device assignments are surfaced as synthetic instrument records and
their generic UART cable records suppressed. Fully hermetic — the store path
is redirected, and ``scan_usb`` / ``_by_handshake`` / ``serial_id.resolve_tty``
are monkeypatched (no /sys, no hardware, no tty probing). ``conftest.py``
imports the real ``lager`` package once for the whole suite.
"""

import importlib.util
import json
import os
import shutil
import sys
import tempfile
import types
import unittest
from unittest import mock

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
BOX_DIR = os.path.join(REPO_ROOT, "box")

if BOX_DIR not in sys.path:
    sys.path.insert(0, BOX_DIR)


def _load_module(dotted, filepath):
    if dotted in sys.modules:
        return sys.modules[dotted]
    spec = importlib.util.spec_from_file_location(dotted, filepath)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[dotted] = mod
    spec.loader.exec_module(mod)
    return mod


_lager_devices = importlib.import_module("lager.devices")
importlib.import_module("lager.http_handlers")
catalog = _load_module(
    "lager.devices.catalog", os.path.join(BOX_DIR, "lager", "devices", "catalog.py"))
serial_id = _load_module(
    "lager.devices.serial_id", os.path.join(BOX_DIR, "lager", "devices", "serial_id.py"))
cs = _load_module(
    "lager.devices.custom_store",
    os.path.join(BOX_DIR, "lager", "devices", "custom_store.py"))
_lager_devices.catalog = catalog
_lager_devices.serial_id = serial_id
_lager_devices.custom_store = cs

us = _load_module(
    "lager.http_handlers.usb_scanner",
    os.path.join(BOX_DIR, "lager", "http_handlers", "usb_scanner.py"))


# A real Prolific USB-serial cable identity (the DP711's adapter).
VID, PID, SERIAL = "067b", "23a3", "00000006"
TTY = "/dev/ttyUSB0"
SERIAL_ADDR = f"serial://{VID}:{PID}/serial/{SERIAL}"


def _prolific_entry():
    """A generic uart record as scan_usb would emit for the bare cable."""
    return {
        "name": "Prolific_USB_Serial",
        "vid": VID,
        "pid": PID,
        "serial": SERIAL,
        "address": f"USB0::0x{VID.upper()}::0x{PID.upper()}::{SERIAL}::INSTR",
        "net_type": ["uart"],
        "channels": {"uart": [TTY]},
        "tty_path": TTY,
        "tty_paths": [TTY],
    }


class UsbScannerCustomTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.mkdtemp()
        self._orig_store_path = cs.STORE_PATH
        cs.STORE_PATH = os.path.join(self._tmp, "custom_devices.json")

        self.resolved = {}
        self._orig_resolve_tty = serial_id.resolve_tty

        def fake_resolve_tty(vid, pid, serial=None, port_path=None):
            return self.resolved.get((vid, pid, serial, port_path))

        serial_id.resolve_tty = fake_resolve_tty

        self._orig_scan_usb = us.scan_usb
        us.scan_usb = lambda: []

        self.handshake_excludes = []
        self._orig_by_handshake = us._by_handshake

        def fake_handshake(*, exclude=None):
            self.handshake_excludes.append(set(exclude or ()))
            return []

        us._by_handshake = fake_handshake

        self._orig_framework = (us._catalog, us._custom_store, us._serial_id)

    def tearDown(self):
        us._catalog, us._custom_store, us._serial_id = self._orig_framework
        us._by_handshake = self._orig_by_handshake
        us.scan_usb = self._orig_scan_usb
        serial_id.resolve_tty = self._orig_resolve_tty
        cs.STORE_PATH = self._orig_store_path
        shutil.rmtree(self._tmp, ignore_errors=True)

    # ---- helpers ---------------------------------------------------------

    def _set_scan(self, entries):
        us.scan_usb = lambda: [dict(e) for e in entries]

    def _assign_live_dp711(self):
        cs.add("Rigol_DP711", VID, PID, serial=SERIAL)
        self.resolved[(VID, PID, SERIAL, None)] = TTY

    # ---- tests -----------------------------------------------------------

    def test_assigned_live_cable_surfaces_catalog_instrument(self):
        self._assign_live_dp711()
        self._set_scan([_prolific_entry()])

        out = us.list_instruments()
        rec = next((d for d in out if d["name"] == "Rigol_DP711"), None)
        self.assertIsNotNone(rec)
        self.assertEqual(rec["address"], SERIAL_ADDR)
        self.assertEqual(rec["net_type"], ["power-supply"])
        self.assertEqual(rec["channels"], {"power-supply": ["1"]})
        self.assertEqual(rec["tty_path"], TTY)
        self.assertTrue(rec["custom"])
        # The generic cable record is replaced, not duplicated.
        self.assertNotIn("Prolific_USB_Serial", [d["name"] for d in out])
        # JSON-serializable for the /instruments/list HTTP response.
        json.dumps(out)

    def test_unassigned_cable_passes_through_unchanged(self):
        self._set_scan([_prolific_entry()])

        out = us.list_instruments()
        self.assertEqual([d["name"] for d in out], ["Prolific_USB_Serial"])
        self.assertNotIn("custom", out[0])

    def test_unplugged_assignment_not_surfaced(self):
        cs.add("Rigol_DP711", VID, PID, serial=SERIAL)
        self._set_scan([_prolific_entry()])

        names = [d["name"] for d in us.list_instruments()]
        self.assertNotIn("Rigol_DP711", names)
        self.assertIn("Prolific_USB_Serial", names)

    def test_multi_role_chip_not_suppressed(self):
        # An FTDI debug+uart chip sharing the custom tty keeps its generic
        # record — only UART-only entries are replaced.
        self._assign_live_dp711()
        ftdi = {
            "name": "FTDI_FT2232H",
            "vid": "0403", "pid": "6010", "serial": "FT123",
            "address": "USB0::0x0403::0x6010::FT123::INSTR",
            "net_type": ["spi", "i2c", "gpio", "debug", "uart"],
            "channels": {"uart": [TTY]},
            "tty_path": TTY,
            "tty_paths": [TTY],
        }
        self._set_scan([ftdi])

        names = [d["name"] for d in us.list_instruments()]
        self.assertIn("FTDI_FT2232H", names)
        self.assertIn("Rigol_DP711", names)

    def test_unknown_instrument_in_store_skipped(self):
        # Hand-written store record bypassing add()'s catalog validation.
        with open(cs.STORE_PATH, "w", encoding="utf-8") as f:
            json.dump([{"instrument": "Flux_Capacitor", "vid": VID, "pid": PID,
                        "serial": SERIAL, "port_path": None}], f)
        self.resolved[(VID, PID, SERIAL, None)] = TTY
        self._set_scan([_prolific_entry()])

        names = [d["name"] for d in us.list_instruments()]
        self.assertNotIn("Flux_Capacitor", names)
        # No valid custom record -> no suppression either.
        self.assertIn("Prolific_USB_Serial", names)

    def test_port_path_assignment_surfaced_with_port_address(self):
        port = "1-1.2"
        cs.add("Rigol_DP711", VID, PID, port_path=port)
        self.resolved[(VID, PID, None, port)] = TTY
        self._set_scan([])

        out = us.list_instruments()
        self.assertEqual([d["name"] for d in out], ["Rigol_DP711"])
        self.assertEqual(out[0]["address"], f"serial://{VID}:{PID}/port/{port}")

    def test_custom_tty_joins_handshake_exclusion(self):
        self._assign_live_dp711()
        self._set_scan([])

        us.list_instruments()
        self.assertTrue(self.handshake_excludes)
        self.assertIn(TTY, self.handshake_excludes[-1])

    def test_every_channel_of_a_multi_interface_chip_is_excluded(self):
        # tty_path is only the PRIMARY interface. An FT4232H owns four ttys,
        # and excluding channel A alone left B/C/D open to the G-code write.
        ttys = [f"/dev/ttyUSB{n}" for n in range(4)]
        self._set_scan([{
            "name": "FTDI_FT4232H",
            "vid": "0403", "pid": "6011", "serial": "FT4CHAN",
            "address": "USB0::0x0403::0x6011::FT4CHAN::INSTR",
            "net_type": ["uart"],
            "channels": {"uart": list(ttys)},
            "tty_path": ttys[0],
            "tty_paths": list(ttys),
        }])

        us.list_instruments()
        self.assertTrue(self.handshake_excludes)
        for tty in ttys:
            self.assertIn(tty, self.handshake_excludes[-1])

    def test_saved_uart_net_tty_joins_handshake_exclusion(self):
        # A saved uart net pinned to a raw /dev/tty* with no usb_identity is
        # exactly the record has_durable_identity() refuses, so list_saved()
        # attaches no live_path to it. Its port must still be excluded — and
        # its adapter is unknown to scan_usb, so nothing else can protect it.
        saved_tty = "/dev/ttyUSB7"
        net_mod = types.SimpleNamespace(
            Net=types.SimpleNamespace(list_saved=lambda: [
                {"name": "uart1", "role": "uart", "instrument": "FTDI_FT232R",
                 "address": "USB0::INSTR", "pin": saved_tty},
                # A non-uart net must NOT contribute its pin.
                {"name": "gpi1", "role": "gpio", "instrument": "LabJack_T7",
                 "address": "ANY", "pin": "FIO0"},
            ])
        )
        uart_net_mod = types.SimpleNamespace(live_uart_path=lambda rec: None)
        self._set_scan([])

        with mock.patch.dict(sys.modules, {
            "lager.nets.net": net_mod,
            "lager.protocols.uart.uart_net": uart_net_mod,
        }):
            us.list_instruments()

        self.assertTrue(self.handshake_excludes)
        self.assertIn(saved_tty, self.handshake_excludes[-1])
        self.assertNotIn("FIO0", self.handshake_excludes[-1])

    def test_unreadable_saved_nets_do_not_take_the_scan_down(self):
        # The scan must still answer when saved_nets is unavailable — the
        # VID:PID gate, not this set, is what confines the probe.
        boom = types.SimpleNamespace(
            Net=types.SimpleNamespace(
                list_saved=mock.Mock(side_effect=OSError("unreadable"))))
        self._set_scan([_prolific_entry()])

        with mock.patch.dict(sys.modules, {"lager.nets.net": boom}):
            out = us.list_instruments()

        self.assertEqual([d["name"] for d in out], ["Prolific_USB_Serial"])
        self.assertIn(TTY, self.handshake_excludes[-1])

    def test_degraded_mode_without_custom_framework(self):
        # Partial deployment without lager.devices: import guard left None —
        # the scanner must behave exactly as before custom devices existed.
        self._assign_live_dp711()
        self._set_scan([_prolific_entry()])
        us._catalog = us._custom_store = us._serial_id = None

        out = us.list_instruments()
        self.assertEqual([d["name"] for d in out], ["Prolific_USB_Serial"])


if __name__ == "__main__":
    unittest.main()


class TestSuperSpeedCompanionDedupe(unittest.TestCase):
    """One physical dock must list as ONE instrument.

    UNTESTED against hardware -- a dock linked over USB 3 enumerates as two
    virtual hubs, on different buses of the same controller, at the same port
    path. Listing both would show the dock twice, make `nets add`'s
    address lookup ambiguous, and split the per-hub lock across two halves of
    one device. Pairing requires the bus root hubs to actually exist and share
    a parent: realpath() normalises a path that is not there, so without the
    existence check every bus resolves to the same parent and unrelated hubs
    pair up.
    """

    def setUp(self):
        import pathlib
        import shutil
        import tempfile
        self.tmp = pathlib.Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.flat = self.tmp / "devices"
        self.flat.mkdir(parents=True)
        patcher = mock.patch.object(us, "_SYS_USB_DEVICES", self.flat)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _roots(self, *specs):
        """Create usbN root hubs; specs are (busnum, controller_dir_name)."""
        for busnum, controller in specs:
            ctrl = self.tmp / "pci" / controller
            ctrl.mkdir(parents=True, exist_ok=True)
            target = ctrl / f"usb{busnum}"
            target.mkdir(exist_ok=True)
            os.symlink(target, self.flat / f"usb{busnum}")

    def test_same_controller_same_port_path_is_a_companion(self):
        self._roots((1, "xhci0"), (2, "xhci0"))
        self.assertTrue(us._is_companion_of_seen("2-1.4", ["1-1.4"]))

    def test_different_controller_is_not_a_companion(self):
        self._roots((1, "xhci0"), (2, "xhci1"))
        self.assertFalse(us._is_companion_of_seen("2-1.4", ["1-1.4"]))

    def test_different_port_path_is_not_a_companion(self):
        self._roots((1, "xhci0"), (2, "xhci0"))
        self.assertFalse(us._is_companion_of_seen("2-1.5", ["1-1.4"]))

    def test_same_bus_is_never_its_own_companion(self):
        self._roots((1, "xhci0"))
        self.assertFalse(us._is_companion_of_seen("1-1.4", ["1-1.4"]))

    def test_missing_bus_root_pairs_nothing(self):
        # No usbN symlinks created at all.
        self.assertIsNone(us._bus_controller_path(1))
        self.assertFalse(us._is_companion_of_seen("2-1.4", ["1-1.4"]))

    def test_a_name_without_a_port_path_is_ignored(self):
        self._roots((1, "xhci0"), (2, "xhci0"))
        self.assertFalse(us._is_companion_of_seen("usb2", ["1-1.4"]))


class _FakeSerialException(IOError):
    """Stand-in for pyserial's SerialException (an IOError subclass)."""


def _fake_serial_ns(open_behavior=None, reply=b"ok T:25.0\n"):
    """A stand-in for the pyserial module that records what was opened.

    ``_by_handshake`` does ``from serial import Serial`` INSIDE the function,
    so the name resolves out of ``sys.modules`` at call time — patching an
    attribute on the scanner module would not be seen. Shape mirrors
    test_uart_bridge_reconnect.py's ``_fake_serial_ns``, adapted for the
    construct-unopened-then-.open() flow the probe now uses.
    """
    ns = types.SimpleNamespace(SerialException=_FakeSerialException)
    ns.constructed = 0        # every Serial(...) call
    ns.opened_ports = []      # ports .open() was actually called for
    ns.writes = []            # (port, bytes) actually written

    class _FakeSerial:
        def __init__(self, port=None, baudrate=None, timeout=None, exclusive=None):
            ns.constructed += 1
            self.port = port
            self.baudrate = baudrate
            self.timeout = timeout
            self.exclusive = exclusive
            self.dtr = True
            self.rts = True
            self.is_open = False

        def open(self):
            # DTR must be ASSERTED before opening: a CDC-ACM device gates its
            # transmitter on it, and the Dexarm is measurably silent without
            # it. RTS is not needed and stays low. What keeps the probe off a
            # board wired for auto-reset is the vid:pid gate, not the line
            # state -- in auto mode a non-Dexarm port is never opened at all.
            assert self.dtr is True, "DTR not asserted at open(); arm cannot reply"
            assert self.rts is False, "RTS asserted at open()"
            assert self.exclusive is True, "port opened without an exclusive flock"
            ns.opened_ports.append(self.port)
            if open_behavior is not None:
                open_behavior(self.port)
            self.is_open = True

        def reset_input_buffer(self):
            pass

        def reset_output_buffer(self):
            pass

        def write(self, data):
            ns.writes.append((self.port, data))

        def read_until(self, expected=b"\n"):
            return reply

        def read_all(self):
            return reply

        def close(self):
            self.is_open = False

    ns.Serial = _FakeSerial
    return ns


DEXARM_TTY = "/dev/ttyACM0"
FOREIGN_TTY = "/dev/ttyUSB3"


class TestArmProbeGating(unittest.TestCase):
    """What ``_by_handshake`` is allowed to write G-code to.

    This is the only scan step that writes to hardware. The regression it
    guards: a `lager uart` session receiving M105 from a concurrent
    /instruments/list, because the probe opened every tty it could glob.
    """

    def setUp(self):
        # Candidate list: one real Dexarm VCP, one unrelated USB-serial cable
        # of the kind SUPPORTED_USB does not list (so scan_usb never sees it
        # and it can never reach the exclusion set).
        self.ports = [DEXARM_TTY, FOREIGN_TTY]
        patcher = mock.patch.object(
            us, "glob", types.SimpleNamespace(glob=lambda pat: [
                p for p in self.ports
                if p.startswith(pat.rstrip("*"))
            ]))
        patcher.start()
        self.addCleanup(patcher.stop)

        cables = [
            {"vid": "0483", "pid": "5740", "serial": "DEX1",
             "port_path": "1-1.1", "tty": DEXARM_TTY},
            # CH340: a real cable, absent from SUPPORTED_USB.
            {"vid": "1a86", "pid": "7523", "serial": None,
             "port_path": "1-1.2", "tty": FOREIGN_TTY},
        ]
        sid = mock.patch.object(
            us, "_serial_id", types.SimpleNamespace(list_cables=lambda: cables))
        sid.start()
        self.addCleanup(sid.stop)

        ser = mock.patch.object(us, "_get_serial_by_port", lambda port: "DEX1")
        ser.start()
        self.addCleanup(ser.stop)

    def _run(self, ns, exclude=None, env=None):
        with mock.patch.dict(sys.modules, {"serial": ns}), \
                mock.patch.dict(os.environ, env or {}, clear=False):
            return us._by_handshake(exclude=exclude or set())

    def test_dexarm_is_still_found(self):
        # The gate must not filter out the hardware it exists to detect.
        ns = _fake_serial_ns()
        out = self._run(ns)

        self.assertEqual([d["name"] for d in out], ["Rotrix_Dexarm"])
        self.assertEqual(out[0]["address"],
                         "USB0::0x0483::0x5740::DEX1::INSTR")
        self.assertEqual(ns.writes, [(DEXARM_TTY, b"M105\n")])

    def test_foreign_vidpid_is_never_opened(self):
        ns = _fake_serial_ns()
        self._run(ns)

        self.assertNotIn(FOREIGN_TTY, ns.opened_ports)
        self.assertNotIn(FOREIGN_TTY, [port for port, _ in ns.writes])

    def test_unresolvable_tty_fails_closed(self):
        # A tty list_cables cannot account for is not a candidate. Absence of
        # evidence must not become permission to write.
        with mock.patch.object(
                us, "_serial_id", types.SimpleNamespace(list_cables=lambda: [])):
            ns = _fake_serial_ns()
            out = self._run(ns)

        self.assertEqual(out, [])
        self.assertEqual(ns.constructed, 0)

    def test_busy_port_is_skipped_not_probed(self):
        # A live `lager uart` session holds the port with pyserial's flock.
        # The probe must lose that race, not write through it.
        def refuse(port):
            raise _FakeSerialException(11, f"Could not exclusively lock port {port}")

        ns = _fake_serial_ns(open_behavior=refuse)
        out = self._run(ns)

        self.assertEqual(out, [])
        self.assertEqual(ns.writes, [])

    def test_excluded_port_is_never_constructed(self):
        ns = _fake_serial_ns()
        out = self._run(ns, exclude={DEXARM_TTY})

        self.assertEqual(out, [])
        self.assertEqual(ns.constructed, 0)

    def test_probe_off_opens_nothing(self):
        ns = _fake_serial_ns()
        out = self._run(ns, env={"LAGER_ARM_PROBE": "off"})

        self.assertEqual(out, [])
        self.assertEqual(ns.constructed, 0)
        self.assertEqual(ns.opened_ports, [])

    def test_force_widens_the_gate_but_keeps_every_other_guard(self):
        # force exists to answer "is my arm being filtered out?". It drops the
        # VID:PID gate ONLY — the exclusion set, the exclusive open and the
        # deasserted modem lines still apply (the latter two are asserted
        # inside the fake's open()).
        ns = _fake_serial_ns()
        self._run(ns, env={"LAGER_ARM_PROBE": "force"})
        self.assertIn(FOREIGN_TTY, ns.opened_ports)

        ns2 = _fake_serial_ns()
        self._run(ns2, exclude={FOREIGN_TTY}, env={"LAGER_ARM_PROBE": "force"})
        self.assertNotIn(FOREIGN_TTY, ns2.opened_ports)

    def test_unrecognized_mode_falls_back_to_auto(self):
        ns = _fake_serial_ns()
        self._run(ns, env={"LAGER_ARM_PROBE": "yes-please"})
        self.assertEqual(ns.opened_ports, [DEXARM_TTY])

    def test_serial_falls_back_to_sysfs_when_udevadm_is_blind(self):
        # _get_serial_by_port shells out to `udevadm info`, which reads the
        # udev runtime database. A container without /run/udev mounted has no
        # such database: udevadm still answers, with the device's P:/M: lines
        # and no E: properties at all, so ID_SERIAL_SHORT is simply absent.
        # An arm that had just answered the handshake was discarded here.
        # sysfs carries the same serial and list_cables() has already read it.
        with mock.patch.object(us, "_get_serial_by_port", lambda port: None):
            ns = _fake_serial_ns()
            out = self._run(ns)

        self.assertEqual([d["name"] for d in out], ["Rotrix_Dexarm"])
        self.assertEqual(out[0]["address"],
                         "USB0::0x0483::0x5740::DEX1::INSTR")

    def test_no_serial_from_either_source_is_not_reported(self):
        cables = [{"vid": "0483", "pid": "5740", "serial": None,
                   "port_path": "1-1.1", "tty": DEXARM_TTY}]
        with mock.patch.object(us, "_get_serial_by_port", lambda port: None), \
                mock.patch.object(
                    us, "_serial_id",
                    types.SimpleNamespace(list_cables=lambda: cables)):
            ns = _fake_serial_ns()
            out = self._run(ns)

        self.assertEqual(out, [])

    def test_reply_slower_than_a_fixed_sleep_is_still_read(self):
        # The arm answers in more than the 10ms the probe used to sleep before
        # a non-blocking read_all(), so sleep-then-read raced it and concluded
        # "no reply" against hardware that was about to answer. The probe now
        # blocks on read_until, bounded by the port's own timeout.
        ns = _fake_serial_ns()
        slow = {"n": 0}

        class _Slow(ns.Serial):
            def read_all(self):          # what the old code called
                return b""
            def read_until(self, expected=b"\n"):
                slow["n"] += 1
                return b"ok T:-15.00 /0.00 @:0\n"

        ns.Serial = _Slow
        out = self._run(ns)

        self.assertEqual(slow["n"], 1)
        self.assertEqual([d["name"] for d in out], ["Rotrix_Dexarm"])
