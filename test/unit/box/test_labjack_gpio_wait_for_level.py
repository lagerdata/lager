# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

"""LabJackGPIO.wait_for_level: the timeout covers the stream setup (#770)."""
import time
import unittest
from unittest.mock import MagicMock, patch

from lager.io.gpio.labjack_t7 import LabJackGPIO


def _fake_ljm(start_delay: float, sample: float = 0.0) -> MagicMock:
    """An ljm whose eStreamStart takes *start_delay* s and whose reads return *sample*."""
    ljm = MagicMock()
    ljm.namesToAddresses.return_value = ([1000], [0])

    def stream_start(*_args):
        time.sleep(start_delay)
        return 20_000

    def stream_read(_handle):
        time.sleep(0.001)
        return ([sample, sample], 0, 0)

    ljm.eStreamStart.side_effect = stream_start
    ljm.eStreamRead.side_effect = stream_read
    return ljm


class WaitForLevelTimeoutTests(unittest.TestCase):
    def _gpio(self, ljm):
        gpio = LabJackGPIO("gpio1", 0)
        patches = [
            patch.object(LabJackGPIO, "_get_ljm", return_value=ljm),
            patch.object(LabJackGPIO, "_get_handle", return_value=1),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        return gpio

    def test_slow_stream_start_counts_against_the_timeout(self):
        ljm = _fake_ljm(start_delay=0.3)
        gpio = self._gpio(ljm)
        called = time.monotonic()
        with self.assertRaises(TimeoutError):
            gpio.wait_for_level(1, timeout=0.4)
        took = time.monotonic() - called
        # Before the fix the 0.4 s ran from after the 0.3 s start: ~0.7 s.
        self.assertGreaterEqual(took, 0.4)
        self.assertLess(took, 0.55)
        ljm.eStreamStop.assert_called_once()

    def test_does_not_return_before_the_timeout(self):
        gpio = self._gpio(_fake_ljm(start_delay=0.0))
        called = time.monotonic()
        with self.assertRaises(TimeoutError):
            gpio.wait_for_level(1, timeout=0.2)
        self.assertGreaterEqual(time.monotonic() - called, 0.2)

    def test_elapsed_is_measured_from_the_stream_start(self):
        gpio = self._gpio(_fake_ljm(start_delay=0.2, sample=3.3))
        elapsed = gpio.wait_for_level(1, timeout=2)
        # Detection is immediate once streaming; the setup is not in elapsed.
        self.assertLess(elapsed, 0.1)


if __name__ == "__main__":
    unittest.main()
