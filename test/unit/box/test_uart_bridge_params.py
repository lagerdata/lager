# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0
"""UARTBridge serial parameters: the read timeout and the parity value.

`UARTNet.connect(timeout=1.0)` used to be swallowed by `**kwargs`, and the port
always opened with a 0.1 s read timeout. A parity outside the five names,
pyserial's own `'N'`/`'E'`/`'O'` included, silently opened with no parity.

The drivers the dispatcher builds for the websocket session and the monitor
streams keep the 0.1 s timeout on purpose: those loops notice a stop only when
a read returns.

The bridge module is loaded standalone with ``serial`` stubbed, as
test_uart_bridge_reconnect.py does, so no hardware dependency is needed.
"""

import importlib
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


def _load_module(dotted, filepath):
    if dotted in sys.modules:
        return sys.modules[dotted]
    spec = importlib.util.spec_from_file_location(dotted, filepath)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[dotted] = mod
    spec.loader.exec_module(mod)
    return mod


importlib.import_module("lager.devices")
uart_bridge = _load_module(
    "uart_bridge_params_ut",
    os.path.join(BOX_DIR, "lager", "protocols", "uart", "uart_bridge.py"),
)
UARTBridge = uart_bridge.UARTBridge


class FakeConn:
    def reset_input_buffer(self):
        pass

    def reset_output_buffer(self):
        pass


def _fake_serial():
    """A pyserial stand-in that records how each port was opened.

    It deliberately has no PARITY_* constants: the bridge maps parity itself.
    """
    ns = types.SimpleNamespace(
        STOPBITS_ONE=1, STOPBITS_ONE_POINT_FIVE=1.5, STOPBITS_TWO=2,
        opened=[],
    )

    def Serial(**kwargs):
        ns.opened.append(kwargs)
        return FakeConn()

    ns.Serial = Serial
    return ns


class _BridgeTest(unittest.TestCase):
    def setUp(self):
        self._devices_pkg = sys.modules["lager.devices"]
        self._had_serial_id = hasattr(self._devices_pkg, "serial_id")
        self._old_serial_id = getattr(self._devices_pkg, "serial_id", None)
        self._devices_pkg.serial_id = types.SimpleNamespace(
            resolve_identity=lambda ident: None,
            identity_for_tty=lambda tty: None,
        )
        self._old_serial = uart_bridge.serial
        self.serial = _fake_serial()
        uart_bridge.serial = self.serial

    def tearDown(self):
        if self._had_serial_id:
            self._devices_pkg.serial_id = self._old_serial_id
        else:
            del self._devices_pkg.serial_id
        uart_bridge.serial = self._old_serial

    def bridge(self, **kwargs):
        kwargs.setdefault("device_path", "/dev/ttyFAKE0")
        return UARTBridge("", "0", **kwargs)

    def open_kwargs(self, **kwargs):
        self.bridge(**kwargs)._connect()
        return self.serial.opened[-1]


class ReadTimeout(_BridgeTest):
    def test_the_default_read_timeout_is_a_tenth_of_a_second(self):
        self.assertEqual(self.open_kwargs()["timeout"], 0.1)

    def test_a_timeout_reaches_the_opened_port(self):
        self.assertEqual(self.open_kwargs(timeout=1.0)["timeout"], 1.0)

    def test_none_blocks_until_data_arrives(self):
        self.assertIsNone(self.open_kwargs(timeout=None)["timeout"])


class Parity(_BridgeTest):
    ACCEPTED = [
        ("none", "N"), ("even", "E"), ("odd", "O"), ("mark", "M"), ("space", "S"),
        ("N", "N"), ("E", "E"), ("O", "O"), ("M", "M"), ("S", "S"),
        ("Even", "E"), ("ODD", "O"),
    ]

    def test_names_and_pyserial_letters_open_with_that_parity(self):
        for value, expected in self.ACCEPTED:
            with self.subTest(parity=value):
                self.assertEqual(self.open_kwargs(parity=value)["parity"], expected)

    def test_an_unknown_parity_raises_before_any_port_opens(self):
        for value in ("evn", "X", ""):
            with self.subTest(parity=value):
                with self.assertRaisesRegex(ValueError, "Unsupported UART parity"):
                    self.bridge(parity=value)
        self.assertEqual(self.serial.opened, [])


class SessionDriversKeepTheShortTimeout(_BridgeTest):
    """The dispatcher builds the drivers the websocket session and monitors use."""

    def setUp(self):
        super().setUp()
        self.dispatcher = importlib.import_module("lager.protocols.uart.dispatcher")

    def _driver(self, params=None, overrides=None):
        rec = {"name": "uart1", "role": "uart", "pin": "/dev/ttyFAKE0",
               "params": params or {}}
        return self.dispatcher._make_driver(rec, overrides or {})

    def test_a_stored_or_sent_timeout_does_not_reach_a_session_driver(self):
        driver = self._driver(params={"timeout": None}, overrides={"timeout": 5})
        self.assertEqual(driver.timeout, 0.1)

    def test_an_unknown_stored_parity_is_a_backend_error(self):
        from lager.exceptions import UARTBackendError
        with self.assertRaisesRegex(UARTBackendError, "Unsupported UART parity"):
            self._driver(params={"parity": "evn"})


if __name__ == "__main__":
    unittest.main()
