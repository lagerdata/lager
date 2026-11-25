# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0
"""
How a T7 is opened (box/lager/io/labjack_handle.py).

With connection "ANY", LJM falls back to network discovery when no device
answers on USB. On a Mac with VPN interfaces up that discovery ran for more
than 15 minutes with the T7 unplugged, holding the hardware service's lock,
and every later call failed "device busy" until the agent was restarted. The
handle now opens over USB unless LAGER_LABJACK_CONNECTION says otherwise, and
a device that is not found on a named connection fails after
NOT_FOUND_ATTEMPTS tries, well inside the service's 8 s lock deadline.
"""

import os
import unittest
from unittest.mock import patch

from lager.io import labjack_handle


class _FakeLJM:
    """openS fails with LJM's not-found error until ``present`` is set."""

    def __init__(self, present=False, error="LJM error code 1227 LJME_DEVICE_NOT_FOUND"):
        self.present = present
        self.error = error
        self.opens = []

    def writeLibraryConfigS(self, name, value):
        pass

    def openS(self, device_type, connection, identifier):
        self.opens.append((device_type, connection, identifier))
        if not self.present:
            raise Exception(self.error)
        return 7

    def eReadName(self, handle, name):
        return 1.0

    def close(self, handle):
        pass


class _Fixture(unittest.TestCase):

    def setUp(self):
        self.manager = object.__new__(labjack_handle.LabJackHandleManager)
        self.manager._handle = None
        self.manager._handle_lock = labjack_handle.threading.Lock()
        self.manager._ref_count = 0
        self.manager._initialized = False
        self.sleeps = []
        patcher = patch.object(labjack_handle.time, "sleep", side_effect=self.sleeps.append)
        patcher.start()
        self.addCleanup(patcher.stop)
        env = {k: v for k, v in os.environ.items() if k != "LAGER_LABJACK_CONNECTION"}
        envpatch = patch.dict(os.environ, env, clear=True)
        envpatch.start()
        self.addCleanup(envpatch.stop)

    def use(self, fake):
        patcher = patch.object(labjack_handle, "ljm", fake)
        patcher.start()
        self.addCleanup(patcher.stop)
        return fake


class DefaultConnectionTest(_Fixture):

    def test_usb_is_the_default(self):
        self.assertEqual(labjack_handle.default_connection(), "USB")

    def test_the_variable_overrides_it(self):
        with patch.dict(os.environ, {"LAGER_LABJACK_CONNECTION": "any"}):
            self.assertEqual(labjack_handle.default_connection(), "ANY")

    def test_an_empty_variable_means_the_default(self):
        with patch.dict(os.environ, {"LAGER_LABJACK_CONNECTION": " "}):
            self.assertEqual(labjack_handle.default_connection(), "USB")

    def test_the_handle_is_opened_over_usb_by_default(self):
        ljm = self.use(_FakeLJM(present=True))
        self.assertEqual(self.manager.get_handle(), 7)
        self.assertEqual(ljm.opens, [("T7", "USB", "ANY")])

    def test_an_explicit_connection_is_kept(self):
        ljm = self.use(_FakeLJM(present=True))
        self.manager.get_handle("T7", "ETHERNET", "10.0.0.5")
        self.assertEqual(ljm.opens, [("T7", "ETHERNET", "10.0.0.5")])


class NotFoundTest(_Fixture):

    def test_a_missing_usb_device_fails_after_two_tries(self):
        ljm = self.use(_FakeLJM())
        with self.assertRaises(RuntimeError) as caught:
            self.manager.get_handle()
        self.assertEqual(len(ljm.opens), labjack_handle.NOT_FOUND_ATTEMPTS)
        self.assertIn("not found on USB", str(caught.exception))
        self.assertIn("plugged in", str(caught.exception))
        self.assertIn("1227", str(caught.exception), "LJM's own error is kept")

    def test_the_tries_fit_inside_the_service_lock_deadline(self):
        self.use(_FakeLJM())
        with self.assertRaises(RuntimeError):
            self.manager.get_handle()
        # Opens are bounded by LJM_OPEN_USB_DEVICE_TIMEOUT_MS (2 s) each.
        worst = sum(self.sleeps) + 2.0 * labjack_handle.NOT_FOUND_ATTEMPTS
        self.assertLess(worst, 8.0, f"sleeps {self.sleeps} plus opens reach the 8 s deadline")

    def test_any_keeps_every_try(self):
        ljm = self.use(_FakeLJM())
        with self.assertRaises(Exception):
            self.manager.get_handle("T7", "ANY", "ANY", max_retries=5)
        self.assertEqual(len(ljm.opens), 5)

    def test_a_replugged_device_opens_on_the_next_call(self):
        ljm = self.use(_FakeLJM())
        with self.assertRaises(RuntimeError):
            self.manager.get_handle()
        ljm.present = True
        self.assertEqual(self.manager.get_handle(), 7)

    def test_other_recoverable_errors_still_retry(self):
        ljm = self.use(_FakeLJM(error="LJM error 1230 LJME_DEVICE_ALREADY_OPEN"))
        with self.assertRaises(Exception):
            self.manager.get_handle(max_retries=4)
        self.assertEqual(len(ljm.opens), 4)


if __name__ == "__main__":
    unittest.main()
