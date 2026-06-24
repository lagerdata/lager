# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for ``lager.debug.teardown_registry`` (GAP 1b).

The registry records debug nets connected by the current process and, when
``LAGER_DEBUG_AUTOTEARDOWN`` is enabled, disconnects them on exit/abort so an
orphaned ``-stayrunning`` JLinkGDBServer can't keep holding the probe.

The module is pure stdlib (no relative imports), so we load it standalone via
``spec_from_file_location`` — no ``lager/__init__`` needed. ``atexit``/``signal``
registration is mocked so the test process is never actually mutated.
"""

import importlib.util
import os
import signal
import unittest
from unittest.mock import patch, MagicMock

HERE = os.path.dirname(__file__)
TR_PATH = os.path.normpath(
    os.path.join(HERE, "..", "..", "..", "box", "lager", "debug", "teardown_registry.py")
)
_spec = importlib.util.spec_from_file_location("teardown_registry_under_test", TR_PATH)
tr = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(tr)


class _FakeNet:
    def __init__(self, name="dbg", raises=False):
        self.name = name
        self.disconnect_calls = 0
        self._raises = raises

    def disconnect(self):
        self.disconnect_calls += 1
        if self._raises:
            raise RuntimeError("boom")


class TeardownRegistryTests(unittest.TestCase):
    def setUp(self):
        # Reset module globals between tests.
        tr._registered.clear()
        tr._prev_handlers.clear()
        tr._handlers_installed = False
        os.environ.pop("LAGER_DEBUG_AUTOTEARDOWN", None)

    def tearDown(self):
        os.environ.pop("LAGER_DEBUG_AUTOTEARDOWN", None)

    # --- gate off (default) ------------------------------------------------ #
    def test_disabled_by_default(self):
        self.assertFalse(tr._enabled())
        net = _FakeNet()
        tr.register(net)
        self.assertEqual(len(tr._registered), 0, "must not record when disabled")

    def test_install_handlers_noop_when_disabled(self):
        with patch.object(tr.signal, "signal") as sig, \
                patch.object(tr.atexit, "register") as areg:
            tr.install_handlers()
        sig.assert_not_called()
        areg.assert_not_called()
        self.assertFalse(tr._handlers_installed)

    # --- gate on ----------------------------------------------------------- #
    def test_enabled_on_values(self):
        for val in ("1", "true", "YES", "On"):
            os.environ["LAGER_DEBUG_AUTOTEARDOWN"] = val
            self.assertTrue(tr._enabled(), val)
        for val in ("0", "off", "no", ""):
            os.environ["LAGER_DEBUG_AUTOTEARDOWN"] = val
            self.assertFalse(tr._enabled(), val)

    def test_install_handlers_idempotent(self):
        os.environ["LAGER_DEBUG_AUTOTEARDOWN"] = "1"
        with patch.object(tr.signal, "signal") as sig, \
                patch.object(tr.signal, "getsignal", return_value=signal.SIG_DFL), \
                patch.object(tr.atexit, "register") as areg:
            tr.install_handlers()
            tr.install_handlers()  # second call must be a no-op
        self.assertEqual(areg.call_count, 1)
        signals = {c.args[0] for c in sig.call_args_list}
        self.assertEqual(signals, {signal.SIGTERM, signal.SIGINT})

    def test_register_and_teardown_disconnects_once(self):
        os.environ["LAGER_DEBUG_AUTOTEARDOWN"] = "1"
        net = _FakeNet()
        tr.register(net)
        tr.teardown_all()
        self.assertEqual(net.disconnect_calls, 1)
        # registry cleared -> a second teardown does nothing
        tr.teardown_all()
        self.assertEqual(net.disconnect_calls, 1)

    def test_teardown_swallows_disconnect_errors(self):
        os.environ["LAGER_DEBUG_AUTOTEARDOWN"] = "1"
        bad, good = _FakeNet("bad", raises=True), _FakeNet("good")
        tr.register(bad)
        tr.register(good)
        tr.teardown_all()  # must not raise
        self.assertEqual(good.disconnect_calls, 1)

    def test_unregister_prevents_teardown(self):
        os.environ["LAGER_DEBUG_AUTOTEARDOWN"] = "1"
        net = _FakeNet()
        tr.register(net)
        tr.unregister(net)
        tr.teardown_all()
        self.assertEqual(net.disconnect_calls, 0)

    def test_signal_handler_chains_to_previous(self):
        os.environ["LAGER_DEBUG_AUTOTEARDOWN"] = "1"
        sentinel = MagicMock()
        net = _FakeNet()
        with patch.object(tr.signal, "signal"), \
                patch.object(tr.signal, "getsignal", return_value=sentinel), \
                patch.object(tr.atexit, "register"):
            tr.install_handlers()
        tr.register(net)
        # Previous handler is callable -> handler must run teardown then chain
        # to it (no os.kill / re-raise path).
        tr._signal_handler(signal.SIGTERM, None)
        self.assertEqual(net.disconnect_calls, 1)
        sentinel.assert_called_once_with(signal.SIGTERM, None)


if __name__ == "__main__":
    unittest.main()
