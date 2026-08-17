# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0
"""
Unit tests for ``load_box_secrets()`` in box/lager/python/executor.py.

This is where a secrets file the runtime cannot read turns into silence. The
function returns ``{}`` on any failure, and the caller injects that as the
complete set of ``LAGER_SECRET_*`` variables — so an unreadable file and a box
with no secrets configured are indistinguishable downstream. A script then
fails on a missing environment variable, nowhere near the cause.

A PermissionError here means something specific and fixable: the file is mode
0600 under an owner that is not this process's uid, which is what happens when
secrets are copied onto a box by hand and then tightened by a boot script
running as the host user. It is now logged at error level with the remediation,
separately from the generic "could not load" path, because that log line is the
only place the real reason ever appears.

``{}`` is still returned — callers must not start crashing.
"""

import json
import logging
import os
import sys
import tempfile
import types
import unittest
from unittest.mock import MagicMock, patch


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


for _dep in ('pyvisa', 'usb', 'usb.core', 'usb.util', 'serial',
             'serial.tools', 'serial.tools.list_ports', 'flask_socketio'):
    _stub(_dep)

_BOX_ROOT = os.path.abspath(
    os.path.join(os.path.dirname(__file__), '..', '..', '..', 'box')
)
if _BOX_ROOT not in sys.path:
    sys.path.insert(0, _BOX_ROOT)

from lager.python.executor import load_box_secrets  # noqa: E402


class LoadBoxSecretsTests(unittest.TestCase):
    """load_box_secrets takes its path as a parameter, so every case here
    runs against a REAL file in a temp dir — no patching of os.path (which
    is process-global) and no redirected open. The only fake left is the
    PermissionError, which cannot be produced reliably from file modes (a
    root-run test would read straight through 0o000)."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = os.path.join(self.tmp.name, 'org_secrets.json')

    def _write(self, payload, mode=0o600):
        with open(self.path, 'w') as fh:
            json.dump(payload, fh)
        os.chmod(self.path, mode)

    def test_production_callers_get_the_box_path(self):
        import inspect
        default = inspect.signature(load_box_secrets).parameters[
            'secrets_file'].default
        self.assertEqual(default, '/etc/lager/org_secrets.json')

    def test_readable_secrets_are_returned(self):
        self._write({'API_KEY': 'shh', 'REGION': 'eu'})
        self.assertEqual(load_box_secrets(self.path),
                         {'API_KEY': 'shh', 'REGION': 'eu'})

    def test_loose_permissions_are_tightened_before_reading(self):
        self._write({'API_KEY': 'shh'}, mode=0o644)
        with self.assertLogs('lager.python.executor', level='WARNING'):
            self.assertEqual(load_box_secrets(self.path), {'API_KEY': 'shh'})
        self.assertEqual(os.stat(self.path).st_mode & 0o777, 0o600)

    def test_permission_error_returns_empty_dict(self):
        """Callers must not start crashing — the empty dict is the contract."""
        self._write({'API_KEY': 'shh'})
        with patch('builtins.open', side_effect=PermissionError(
                13, 'Permission denied')):
            with self.assertLogs('lager.python.executor', level='ERROR'):
                self.assertEqual(load_box_secrets(self.path), {})

    def test_permission_error_logs_at_error_with_remediation(self):
        """The whole point of separating this branch: the consequence is
        invisible downstream, so the log line has to carry the diagnosis."""
        self._write({'API_KEY': 'shh'})
        with patch('builtins.open', side_effect=PermissionError(
                13, 'Permission denied')):
            with self.assertLogs('lager.python.executor', level='ERROR') as caught:
                load_box_secrets(self.path)

        blob = '\n'.join(caught.output)
        self.assertIn(self.path, blob, 'the log must name the unreadable file')
        self.assertIn(str(os.getuid()), blob, 'the uid is the actionable detail')
        self.assertIn('chown', blob)
        self.assertIn('chmod 600', blob)
        self.assertIn('EMPTY', blob, 'must say the secrets silently vanish')

    def test_malformed_json_still_warns_rather_than_errors(self):
        """The generic path is unchanged: a corrupt file is a different problem
        with a different fix, and must not claim to be a permissions issue."""
        with open(self.path, 'w') as fh:
            fh.write('{not json')
        os.chmod(self.path, 0o600)
        with self.assertLogs('lager.python.executor', level='WARNING') as caught:
            self.assertEqual(load_box_secrets(self.path), {})

        blob = '\n'.join(caught.output)
        self.assertNotIn('ERROR', blob.split(':')[0])
        self.assertNotIn('chown', blob)

    def test_missing_file_returns_empty_without_logging(self):
        with patch.object(logging.getLogger('lager.python.executor'),
                          'error') as err:
            self.assertEqual(load_box_secrets(self.path), {})
            err.assert_not_called()


if __name__ == '__main__':
    unittest.main()
