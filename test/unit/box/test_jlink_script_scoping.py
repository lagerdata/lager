# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0
"""
Unit tests for J-Link script scoping (issue #195).

The box used to keep ONE J-Link script at `/tmp/lager_jlink_script.JLinkScript`
and hand it to any debug operation that did not bring its own. Only `connect`
and `flash` send a script; `reset`, `erase`, `memrd` and the gdbserver path send
nothing, so they silently inherited whichever script was written last -- by a
different net, by a previous session, or by a test suite that had since
finished.

That is harmful on its own (net A's script running under net B's operations),
and it is what took benches out of service: a configured script plus a
just-erased target makes the debugger unable to attach, so one scripted flash
left a bench failing every later attach until somebody deleted the file by hand.
The error named neither the script nor the file.

The attach failure itself was later measured properly and is NOT what these
tests cover -- see ``test_jlink_script_attach_retry.py``. In short: a script
that defines ``InitTarget()`` replaces J-Link's built-in per-device
``InitTarget()``, and on an nRF5340 that built-in is what brings up a blank
part. The replacement is per function, so a script defining no ``InitTarget()``
is harmless. An earlier version of this docstring claimed the opposite -- that
an inert script was enough and therefore only persistence mattered -- on the
strength of one CI observation. Three trials per cell on hardware say
otherwise; contents are exactly what matters.

The persistence is still a real and separate defect, and it is what these tests
pin: a script reaching operations that never asked for one.

What is pinned here:

  * a script is written per net, so two nets cannot collide;
  * an operation with no net gets NO script (never an ambient one);
  * a net only ever resolves its OWN script, even when another net's exists;
  * a session's script is removed when the session ends;
  * net names are sanitised before reaching a filesystem path.
"""

import os
import sys
import types
import unittest
from unittest.mock import MagicMock


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


for _dep in ['pyvisa', 'pyvisa.constants', 'usb', 'usb.util', 'usb.core', 'pigpio',
             'labjack', 'labjack.ljm', 'nidaqmx', 'bleak', 'serial',
             'serial.tools', 'serial.tools.list_ports', 'pygdbmi']:
    _stub(_dep)

_BOX_ROOT = os.path.abspath(
    os.path.join(os.path.dirname(__file__), '..', '..', '..', 'box'))
if _BOX_ROOT not in sys.path:
    sys.path.insert(0, _BOX_ROOT)

from lager.debug import api  # noqa: E402


class ScriptPathScopingTests(unittest.TestCase):
    """One script per net, and never a shared one."""

    def test_two_nets_get_two_paths(self):
        a = api.script_path_for_net('debug1')
        b = api.script_path_for_net('debug2')
        self.assertNotEqual(a, b)

    def test_path_is_not_the_legacy_shared_file(self):
        """The regression itself: a per-net path must not collapse onto the one
        every operation used to read."""
        for net in ('debug1', 'debug2', 'swd'):
            self.assertNotEqual(api.script_path_for_net(net),
                                api.JLINK_SCRIPT_TEMP_PATH)

    def test_no_net_means_no_path(self):
        for empty in (None, '', '   '):
            self.assertIsNone(api.script_path_for_net(empty))

    def test_net_names_are_sanitised_before_hitting_the_filesystem(self):
        """Net names come from user config. A name with a slash must not be
        able to steer the write out of /tmp."""
        path = api.script_path_for_net('../../etc/evil')
        self.assertIsNotNone(path)
        self.assertTrue(path.startswith('/tmp/'))
        self.assertNotIn('/', path[len('/tmp/'):])


class ResolutionTests(unittest.TestCase):
    """What an operation gets when it did not ask for a script."""

    def setUp(self):
        self._written = []

    def tearDown(self):
        for p in self._written:
            try:
                os.remove(p)
            except OSError:
                pass

    def _write_for(self, net):
        path = api.script_path_for_net(net)
        with open(path, 'w') as f:
            f.write('int InitTarget(void) { return 0; }\n')
        self._written.append(path)
        return path

    def test_operation_with_no_net_gets_nothing(self):
        """THE defect. A script existed on disk and every net-less operation
        picked it up. Now: no net, no script."""
        self._write_for('debug1')
        self.assertIsNone(api._get_script_file(None))

    def test_net_does_not_inherit_another_nets_script(self):
        """Cross-net poisoning: flashing net A ran net B's script."""
        self._write_for('debug1')
        self.assertIsNone(api._get_script_file('debug2'))

    def test_net_resolves_its_own_script(self):
        expected = self._write_for('debug1')
        self.assertEqual(api._get_script_file('debug1'), expected)

    def test_absent_script_resolves_to_none(self):
        self.assertIsNone(api._get_script_file('never-configured-net'))


class LifetimeTests(unittest.TestCase):
    """A script belongs to a session, not to the box."""

    def test_clear_removes_only_that_nets_script(self):
        a = api.script_path_for_net('debug1')
        b = api.script_path_for_net('debug2')
        for p in (a, b):
            with open(p, 'w') as f:
                f.write('x')
        try:
            self.assertTrue(api.clear_script_file('debug1'))
            self.assertFalse(os.path.exists(a))
            self.assertTrue(os.path.exists(b), "cleared the wrong net's script")
        finally:
            for p in (a, b):
                try:
                    os.remove(p)
                except OSError:
                    pass

    def test_clear_is_safe_when_nothing_is_there(self):
        self.assertFalse(api.clear_script_file('no-such-net'))

    def test_clear_is_safe_with_no_net(self):
        self.assertFalse(api.clear_script_file(None))

    def test_after_clear_the_net_resolves_nothing(self):
        """The property that matters: the next operation on this net, however
        much later, does not silently run under the old session's script."""
        path = api.script_path_for_net('debug1')
        with open(path, 'w') as f:
            f.write('x')
        api.clear_script_file('debug1')
        self.assertIsNone(api._get_script_file('debug1'))


if __name__ == '__main__':
    unittest.main()
