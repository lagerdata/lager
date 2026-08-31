# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0
"""The debug layer's runtime root is one value, spelled the same everywhere.

`debug/jlink.py` cannot import it. Three tests load that module standalone via
importlib, with no parent package, so the box suite does not have to pull in
pyvisa and the hardware drivers just to check argv assembly -- which means the
module cannot do `from .probes import ...`, and cannot do `from lager...`
either, since that executes `lager/__init__.py`.

So it carries its own copy. This pins the copy to the original: if the root
ever moves off `/tmp`, one of these fails rather than a script path silently
being refused on one code path and accepted on another.
"""

import importlib.util
import os
import sys
import types
import unittest
from unittest.mock import MagicMock


HERE = os.path.dirname(__file__)
DEBUG_DIR = os.path.normpath(
    os.path.join(HERE, '..', '..', '..', 'box', 'lager', 'debug')
)


def _load_standalone(name, filename):
    """Load a debug module by path, the way the jlink tests do."""
    spec = importlib.util.spec_from_file_location(
        name, os.path.join(DEBUG_DIR, filename))
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def _make_module(name):
    mod = types.ModuleType(name)
    mod.__getattr__ = lambda attr: MagicMock()  # type: ignore[method-assign]
    return mod


def _stub(dotted):
    parts = dotted.split('.')
    for i in range(1, len(parts) + 1):
        key = '.'.join(parts[:i])
        if key not in sys.modules:
            sys.modules[key] = _make_module(key)


for _dep in [
    'pyvisa', 'pyvisa.constants', 'pyvisa_py',
    'usb', 'usb.util', 'usb.core', 'pigpio',
    'labjack', 'labjack.ljm', 'nidaqmx',
    'phidget22', 'phidget22.Phidget', 'phidget22.Net',
    'bleak', 'picoscope',
    'serial', 'serial.tools', 'serial.tools.list_ports',
    'spidev', 'smbus', 'smbus2', 'RPi', 'RPi.GPIO', 'gpiod',
    'pexpect', 'pexpect.replwrap', 'pexpect.exceptions',
    'pygdbmi', 'pygdbmi.gdbcontroller', 'pygdbmi.constants',
]:
    _stub(_dep)

_BOX_ROOT = os.path.abspath(os.path.join(HERE, '..', '..', '..', 'box'))
if _BOX_ROOT not in sys.path:
    sys.path.insert(0, _BOX_ROOT)

from lager.debug import probes as probes_mod  # noqa: E402


class RuntimeRootIsOneValue(unittest.TestCase):
    def test_jlink_copy_matches_probes(self):
        jlink = _load_standalone('jlink_root_probe', 'jlink.py')
        self.assertEqual(
            jlink._RUNTIME_DIR, probes_mod.RUNTIME_DIR,
            msg='jlink.py carries its own copy of the runtime root because it '
                'must stay standalone-importable; the two have drifted',
        )

    def test_jlink_stays_standalone_importable(self):
        # The reason the copy exists. If someone adds a package-relative or
        # lager.* import to jlink.py, this fails here rather than as three
        # confusing collection errors elsewhere in the suite.
        for mod in list(sys.modules):
            if mod == 'jlink_standalone_check':
                del sys.modules[mod]
        jlink = _load_standalone('jlink_standalone_check', 'jlink.py')
        self.assertTrue(hasattr(jlink, 'commander'))


if __name__ == '__main__':
    unittest.main()
