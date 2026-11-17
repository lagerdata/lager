# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0
"""Per-call I2C overrides apply to one call only.

An ``overrides`` dict passed to a bus call used to be written into the net's
cached driver and stay there, so every later call in the same script ran at the
overridden frequency. A ``pull_ups``-only override also reset the clock to
100 kHz, because every driver's ``config()`` defaults ``frequency_hz``.

A fake driver sits in the dispatcher's cache, so nothing here opens a device.
"""
import unittest

from lager.protocols.i2c import dispatcher as disp
from lager.protocols.i2c.i2c_net import I2CNet


class FakeI2C:
    """Records the frequency/pull-ups in force for each bus operation."""

    def __init__(self, frequency_hz=100_000, pull_ups=False):
        self._frequency_hz = frequency_hz
        self._pull_ups = pull_ups
        self.seen = []

    def config(self, frequency_hz=100_000, pull_ups=None):
        self._frequency_hz = frequency_hz
        if pull_ups is not None:
            self._pull_ups = pull_ups

    def _record(self):
        self.seen.append((self._frequency_hz, self._pull_ups))

    def scan(self, start_addr=0x08, end_addr=0x77):
        self._record()
        return [0x48]

    def read(self, address, num_bytes):
        self._record()
        return [0] * num_bytes

    def write(self, address, data):
        self._record()

    def write_read(self, address, data, num_bytes):
        self._record()
        return [0] * num_bytes


NET = "i2c-override-test"


class PerCallOverrideTests(unittest.TestCase):
    def setUp(self):
        self.drv = FakeI2C(frequency_hz=100_000, pull_ups=False)
        disp._driver_cache[NET] = self.drv
        self.addCleanup(disp._driver_cache.pop, NET, None)

    def test_override_applies_to_its_call_only(self):
        disp.read(NET, 0x48, 2, overrides={"frequency_hz": 400_000})
        disp.read(NET, 0x48, 2)
        self.assertEqual(self.drv.seen, [(400_000, False), (100_000, False)])

    def test_every_action_restores(self):
        ov = {"frequency_hz": 1_000_000}
        disp.scan(NET, overrides=ov)
        disp.write(NET, 0x48, [1], overrides=ov)
        disp.transfer(NET, 0x48, 1, data=[0], overrides=ov)
        self.assertEqual([s[0] for s in self.drv.seen], [1_000_000] * 3)
        self.assertEqual(self.drv._frequency_hz, 100_000)

    def test_pull_ups_only_override_keeps_frequency(self):
        self.drv._frequency_hz = 400_000
        disp.read(NET, 0x48, 1, overrides={"pull_ups": True})
        self.assertEqual(self.drv.seen, [(400_000, True)])
        self.assertEqual((self.drv._frequency_hz, self.drv._pull_ups),
                         (400_000, False))

    def test_restores_when_the_io_raises(self):
        def boom(address, num_bytes):
            raise RuntimeError("bus error")
        self.drv.read = boom
        with self.assertRaises(RuntimeError):
            disp.read(NET, 0x48, 1, overrides={"frequency_hz": 400_000})
        self.assertEqual(self.drv._frequency_hz, 100_000)

    def test_none_values_change_nothing(self):
        calls = []
        self.drv.config = lambda **kw: calls.append(kw)
        disp.read(NET, 0x48, 1, overrides={"frequency_hz": None})
        self.assertEqual(calls, [])

    def test_i2c_net_overrides_are_per_call(self):
        net = I2CNet(NET, {"name": NET, "params": {}})
        net.read(0x48, 1, overrides={"frequency_hz": 400_000})
        net.write(0x48, [1], overrides={"frequency_hz": 400_000})
        net.write_read(0x48, [0], 1, overrides={"frequency_hz": 400_000})
        net.read(0x48, 1)
        self.assertEqual([s[0] for s in self.drv.seen],
                         [400_000, 400_000, 400_000, 100_000])

    def test_hardware_adapter_overrides_are_per_call(self):
        from lager.i2c_hs import I2CHardwareAdapter
        hs = I2CHardwareAdapter(NET)
        hs.read(0x48, 1, overrides={"frequency_hz": 400_000})
        hs.scan(overrides={"frequency_hz": 400_000})
        hs.write(0x48, [1], overrides={"frequency_hz": 400_000})
        hs.write_read(0x48, [0], 1, overrides={"frequency_hz": 400_000})
        hs.read(0x48, 1)
        self.assertEqual([s[0] for s in self.drv.seen],
                         [400_000] * 4 + [100_000])


class PersistentPathTests(unittest.TestCase):
    """config() still changes the cached driver for good."""

    def setUp(self):
        self.drv = FakeI2C(frequency_hz=400_000, pull_ups=False)
        disp._driver_cache[NET] = self.drv
        self.addCleanup(disp._driver_cache.pop, NET, None)

    def test_resolve_with_pull_ups_only_keeps_live_frequency(self):
        disp._resolve_net_and_driver(NET, {"pull_ups": True})
        self.assertEqual((self.drv._frequency_hz, self.drv._pull_ups),
                         (400_000, True))

    def test_hardware_adapter_config_persists(self):
        from lager.i2c_hs import I2CHardwareAdapter
        I2CHardwareAdapter(NET).config(1_000_000, True)
        disp.read(NET, 0x48, 1)
        self.assertEqual(self.drv.seen, [(1_000_000, True)])


if __name__ == "__main__":
    unittest.main()
