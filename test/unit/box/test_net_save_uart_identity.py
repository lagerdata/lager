# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for usb_identity_for_net_record (save-time durable identity).

When a UART net is saved, Net.save_local_net attaches a durable USB identity
snapshot (vid/pid + serial or physical port + interface) computed by this
helper — the raw /dev/tty* number stored in `pin` does not survive USB
re-enumeration. The helper must never raise (an offline device just saves
without the field) and must leave the record's pin untouched.

uart_net.py is loaded standalone with the ``serial`` package stubbed and a
fake lager.devices.serial_id injected (pattern from the other uart tests).
"""

import importlib.util
import os
import sys
import types
import unittest

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
BOX_DIR = os.path.join(REPO_ROOT, "box")

if BOX_DIR not in sys.path:
    sys.path.insert(0, BOX_DIR)

if 'serial' not in sys.modules:
    sys.modules['serial'] = types.ModuleType('serial')


def _ensure_package(dotted, *parts):
    if dotted in sys.modules:
        return sys.modules[dotted]
    mod = types.ModuleType(dotted)
    mod.__path__ = [os.path.join(BOX_DIR, *parts)]
    mod.__package__ = dotted
    sys.modules[dotted] = mod
    return mod


def _load_module(dotted, filepath):
    if dotted in sys.modules:
        return sys.modules[dotted]
    spec = importlib.util.spec_from_file_location(dotted, filepath)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[dotted] = mod
    spec.loader.exec_module(mod)
    return mod


_ensure_package("lager", "lager")
_ensure_package("lager.devices", "lager", "devices")
uart_net = _load_module(
    "uart_net_save_identity_ut",
    os.path.join(BOX_DIR, "lager", "protocols", "uart", "uart_net.py"),
)

IDENT_TTYUSB0 = {"vid": "0403", "pid": "6011", "serial": None,
                 "port_path": "1-1.2", "interface": 0}
IDENT_CP210X_IF0 = {"vid": "10c4", "pid": "ea60", "serial": "CPSER",
                    "port_path": "1-1.1", "interface": 0}


class UsbIdentityForNetRecordTests(unittest.TestCase):
    def setUp(self):
        self._devices_pkg = sys.modules["lager.devices"]
        self._had_attr = hasattr(self._devices_pkg, "serial_id")
        self._old_attr = getattr(self._devices_pkg, "serial_id", None)
        self.fake = types.SimpleNamespace(
            identity_for_tty=lambda tty: None,
            list_cables=lambda: [],
        )
        self._devices_pkg.serial_id = self.fake

    def tearDown(self):
        if self._had_attr:
            self._devices_pkg.serial_id = self._old_attr
        else:
            del self._devices_pkg.serial_id

    def test_raw_path_pin_snapshots_that_node(self):
        seen = []

        def identity_for_tty(tty):
            seen.append(tty)
            return dict(IDENT_TTYUSB0)
        self.fake.identity_for_tty = identity_for_tty

        record = {"name": "uart1", "role": "uart", "pin": "/dev/ttyUSB2"}
        ident = uart_net.usb_identity_for_net_record(record)
        self.assertEqual(ident, IDENT_TTYUSB0)
        self.assertEqual(seen, ["/dev/ttyUSB2"])
        self.assertEqual(record["pin"], "/dev/ttyUSB2")  # pin untouched

    def test_device_path_preferred_over_pin(self):
        seen = []
        self.fake.identity_for_tty = lambda tty: (seen.append(tty), None)[1]
        record = {"role": "uart", "pin": "SER123",
                  "device_path": "/dev/serial/by-id/usb-X-if00"}
        uart_net.usb_identity_for_net_record(record)
        self.assertEqual(seen, ["/dev/serial/by-id/usb-X-if00"])

    def test_serial_pin_matches_cable_channel(self):
        # Two ttys on the same cable serial (multi-interface): the record's
        # channel picks the right interface.
        cables = [
            {"serial": "CPSER", "tty": "/dev/ttyUSB0"},
            {"serial": "CPSER", "tty": "/dev/ttyUSB1"},
        ]
        idents = {
            "/dev/ttyUSB0": dict(IDENT_CP210X_IF0),
            "/dev/ttyUSB1": dict(IDENT_CP210X_IF0, interface=1),
        }
        self.fake.list_cables = lambda: cables
        self.fake.identity_for_tty = lambda tty: idents.get(tty)

        record = {"role": "uart", "pin": "CPSER", "channel": "1"}
        ident = uart_net.usb_identity_for_net_record(record)
        self.assertEqual(ident["interface"], 1)

    def test_serial_pin_default_channel_zero(self):
        self.fake.list_cables = lambda: [{"serial": "CPSER", "tty": "/dev/ttyUSB4"}]
        self.fake.identity_for_tty = lambda tty: dict(IDENT_CP210X_IF0)
        record = {"role": "uart", "pin": "CPSER"}
        self.assertEqual(uart_net.usb_identity_for_net_record(record),
                         IDENT_CP210X_IF0)

    def test_unplugged_device_returns_none(self):
        record = {"role": "uart", "pin": "/dev/ttyUSB2"}
        self.assertIsNone(uart_net.usb_identity_for_net_record(record))
        record = {"role": "uart", "pin": "NOSUCHSERIAL"}
        self.assertIsNone(uart_net.usb_identity_for_net_record(record))

    def test_never_raises(self):
        def boom(*a, **kw):
            raise RuntimeError("sysfs exploded")
        self.fake.identity_for_tty = boom
        self.fake.list_cables = boom
        self.assertIsNone(uart_net.usb_identity_for_net_record(
            {"role": "uart", "pin": "/dev/ttyUSB0"}))
        self.assertIsNone(uart_net.usb_identity_for_net_record(
            {"role": "uart", "pin": "SER"}))
        self.assertIsNone(uart_net.usb_identity_for_net_record({}))
        self.assertIsNone(uart_net.usb_identity_for_net_record({"pin": None}))

    def test_non_dev_garbage_pin(self):
        record = {"role": "uart", "pin": 0}
        self.assertIsNone(uart_net.usb_identity_for_net_record(record))


class LiveUartPathTests(unittest.TestCase):
    """live_uart_path: display-time resolution of where a net's device is now."""

    def setUp(self):
        self._devices_pkg = sys.modules["lager.devices"]
        self._had_attr = hasattr(self._devices_pkg, "serial_id")
        self._old_attr = getattr(self._devices_pkg, "serial_id", None)
        self.fake = types.SimpleNamespace(
            resolve_identity=lambda ident: None,
            identity_for_tty=lambda tty: None,
            list_cables=lambda: [],
        )
        self._devices_pkg.serial_id = self.fake

    def tearDown(self):
        if self._had_attr:
            self._devices_pkg.serial_id = self._old_attr
        else:
            del self._devices_pkg.serial_id

    def test_identity_resolves_to_live_node(self):
        self.fake.resolve_identity = lambda ident: "/dev/ttyUSB1"
        rec = {"role": "uart", "pin": "/dev/ttyUSB4",
               "usb_identity": {"vid": "10c4", "pid": "ea60"}}
        self.assertEqual(uart_net.live_uart_path(rec), "/dev/ttyUSB1")

    def test_absent_device_returns_none(self):
        rec = {"role": "uart", "pin": "/dev/ttyUSB4",
               "usb_identity": {"vid": "10c4", "pid": "ea60"}}
        self.assertIsNone(uart_net.live_uart_path(rec))

    def test_by_id_pin_resolves_via_symlink(self):
        import tempfile
        with tempfile.NamedTemporaryFile() as target:
            link_dir = tempfile.mkdtemp()
            link = os.path.join(link_dir, "by-id", "usb-X-if00")
            os.makedirs(os.path.dirname(link))
            os.symlink(target.name, link)
            old_prefix = uart_net._STABLE_PIN_PREFIX
            uart_net._STABLE_PIN_PREFIX = link_dir
            try:
                rec = {"role": "uart", "pin": link}
                self.assertEqual(uart_net.live_uart_path(rec),
                                 os.path.realpath(target.name))
                self.assertTrue(uart_net.has_durable_identity(rec))
                # Broken symlink (device absent) -> None
                os.unlink(target.name)
                # NamedTemporaryFile cleanup will fail on the missing file;
                # recreate on exit
                self.assertIsNone(uart_net.live_uart_path(rec))
            finally:
                uart_net._STABLE_PIN_PREFIX = old_prefix
                open(target.name, "w").close()

    def test_raw_pin_without_identity_is_unknowable(self):
        rec = {"role": "uart", "pin": "/dev/ttyUSB4"}
        self.assertIsNone(uart_net.live_uart_path(rec))
        self.assertFalse(uart_net.has_durable_identity(rec))
        self.assertTrue(uart_net.has_durable_identity(
            {"role": "uart", "pin": "/dev/ttyUSB4", "usb_identity": {"vid": "x"}}))

    def test_never_raises(self):
        def boom(*a, **kw):
            raise RuntimeError("sysfs exploded")
        self.fake.resolve_identity = boom
        self.assertIsNone(uart_net.live_uart_path(
            {"role": "uart", "pin": "/dev/ttyUSB4", "usb_identity": {"vid": "x"}}))
        self.assertIsNone(uart_net.live_uart_path({}))


class SaveLocalNetWiringTests(unittest.TestCase):
    """Structural check that Net.save_local_net (box/lager/nets/net.py) is
    actually wired to the helper. net.py's import graph is too heavy to load
    hermetically here, so verify the real source via AST: the uart-role guard
    exists, it calls usb_identity_for_net_record, it respects an existing
    usb_identity, and the helper is imported from protocols.uart.uart_net."""

    NET_PY = os.path.join(BOX_DIR, "lager", "nets", "net.py")

    @classmethod
    def setUpClass(cls):
        import ast
        cls.tree = ast.parse(open(cls.NET_PY).read())
        cls.save_src = None
        for node in ast.walk(cls.tree):
            if isinstance(node, ast.FunctionDef) and node.name == "save_local_net":
                cls.save_src = ast.unparse(node)
        assert cls.save_src is not None, "save_local_net not found in net.py"

    def test_helper_imported_from_uart_net(self):
        import ast
        imported = False
        for node in ast.walk(self.tree):
            if isinstance(node, ast.ImportFrom) and node.module and \
                    node.module.endswith("uart_net"):
                names = [a.name for a in node.names]
                imported = "usb_identity_for_net_record" in names
                if imported:
                    break
        self.assertTrue(imported,
                        "net.py must import usb_identity_for_net_record")

    def test_save_local_net_enriches_uart_records(self):
        self.assertIn("usb_identity_for_net_record", self.save_src)
        self.assertIn("'uart'", self.save_src)
        # Existing snapshots must be respected (no unconditional overwrite).
        self.assertIn("not data.get('usb_identity')", self.save_src)

    def test_save_local_net_strips_display_annotation(self):
        # live_path is display-only (added by list_saved); a client
        # round-tripping a listed record must not persist it.
        self.assertIn("data.pop('live_path', None)", self.save_src)

    def test_list_saved_annotates_live_path(self):
        import ast
        list_src = None
        for node in ast.walk(self.tree):
            if isinstance(node, ast.FunctionDef) and node.name == "list_saved":
                list_src = ast.unparse(node)
        self.assertIsNotNone(list_src, "list_saved not found in net.py")
        self.assertIn("live_uart_path", list_src)
        self.assertIn("has_durable_identity", list_src)
        # Must annotate copies, never mutate the cached records in place.
        self.assertIn("{**n", list_src)


if __name__ == "__main__":
    unittest.main()
