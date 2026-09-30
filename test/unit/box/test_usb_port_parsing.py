# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0
"""
Which hub port a saved USB net names.

``pin`` wins over ``channel``, but only when it is set. The previous
``row.get("pin") or row.get("channel")`` treated an integer pin of 0 as missing,
so a net on port 0 either read another port (when ``channel`` was also set) or
was dropped as having no port. The HTTP path (the dispatcher) and the Python
API (USBNetWrapper) must agree, so both are pinned here.
"""

import json
import os
import tempfile
import unittest
from unittest.mock import patch

from lager.automation.usb_hub import dispatcher as dispatcher_mod
from lager.automation.usb_hub.usb_net import net_port
from lager.automation.usb_hub.usb_net_wrapper import USBNetWrapper


# (label, net record fields, expected port)
CASES = [
    ("integer pin 0, no channel", {"pin": 0}, 0),
    ("integer pin 0 wins over channel", {"pin": 0, "channel": 2}, 0),
    ("string pin 0", {"pin": "0"}, 0),
    ("integer pin", {"pin": 5}, 5),
    ("channel only", {"channel": 3}, 3),
    ("channel 0 only", {"channel": 0}, 0),
    ("empty pin falls through to channel", {"pin": "", "channel": 1}, 1),
    ("null pin falls through to channel", {"pin": None, "channel": 6}, 6),
    ("neither set", {}, None),
    ("both empty", {"pin": "", "channel": None}, None),
]


def _row(name, fields):
    return {"name": name, "role": "usb", "instrument": "Acroname_8Port",
            "address": "USB0::0x24FF::0x0013::BFABDDC4::INSTR", **fields}


class NetPortTests(unittest.TestCase):
    def test_net_port(self):
        for label, fields, expected in CASES:
            with self.subTest(label):
                self.assertEqual(net_port(fields), expected)


class DispatcherPortTests(unittest.TestCase):
    def setUp(self):
        rows = [_row(f"usb{i}", fields) for i, (_l, fields, _e) in enumerate(CASES)]
        fd, self.path = tempfile.mkstemp(suffix=".json")
        with os.fdopen(fd, "w") as fh:
            json.dump(rows, fh)
        self.addCleanup(os.remove, self.path)

    def test_load_net_definitions(self):
        with patch.object(dispatcher_mod, "NET_DEFS_PATH", self.path):
            nets = dispatcher_mod._load_net_definitions()
        for i, (label, _fields, expected) in enumerate(CASES):
            with self.subTest(label):
                if expected is None:
                    self.assertNotIn(f"usb{i}", nets)
                else:
                    self.assertEqual(nets[f"usb{i}"]["port"], expected)


class WrapperPortTests(unittest.TestCase):
    def test_wrapper_agrees_with_dispatcher(self):
        for i, (label, fields, expected) in enumerate(CASES):
            with self.subTest(label):
                wrapper = USBNetWrapper(f"usb{i}", _row(f"usb{i}", fields))
                self.assertEqual(wrapper.port, expected)


if __name__ == "__main__":
    unittest.main()
