#!/usr/bin/env python3
# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0
"""
Import-surface guards for modules that reach outside the pure-Python stdlib.

Two defects motivated this file, both of which no existing test could catch
because they only fire in an environment CI does not have:

1. ``cli/status.py`` does ``from bson import decode``. That is **pymongo's**
   API -- the unrelated PyPI package literally named ``bson`` exports
   ``loads``/``dumps`` and has no ``decode``. Neither was declared in
   ``install_requires``, so on a clean install ``import cli.status`` raised
   ModuleNotFoundError. It was only reached through a deferred import in
   ``cli/commands/development/debug/__init__.py``, so nothing failed until the
   gdb path ran -- which is also why no test caught it.

2. ``cli/commands/communication/websocket_client.py`` imported ``termios`` and
   ``tty`` unconditionally, making ``lager uart`` interactive mode a hard
   ImportError on Windows. CI is ubuntu-only.

The termios tests below SIMULATE the missing module with a meta_path finder
rather than relying on the platform, so they are meaningful on Linux. That is
the only way to test this until CI gains a windows-latest job.
"""

import importlib
import sys
import unittest
from unittest import mock


class _BlockModules:
    """meta_path finder that makes named modules un-importable.

    Simulates a platform where the module does not exist. Inserted at the FRONT
    of sys.meta_path so it wins over the real finders.
    """

    def __init__(self, *names):
        self.names = set(names)

    def find_module(self, fullname, path=None):  # pragma: no cover - legacy API
        return None

    def find_spec(self, fullname, path=None, target=None):
        if fullname in self.names:
            raise ImportError(f"No module named {fullname!r} (simulated)")
        return None


class _blocked:
    """Context manager: import of `names` fails, and they leave sys.modules."""

    def __init__(self, *names):
        self.names = names
        self.finder = _BlockModules(*names)

    def __enter__(self):
        self.saved = {n: sys.modules.pop(n, None) for n in self.names}
        sys.meta_path.insert(0, self.finder)
        return self

    def __exit__(self, *exc):
        sys.meta_path.remove(self.finder)
        for name, mod in self.saved.items():
            if mod is not None:
                sys.modules[name] = mod
            else:
                sys.modules.pop(name, None)
        return False


class BsonDependencyTests(unittest.TestCase):

    def test_cli_status_imports(self):
        """Fails with ModuleNotFoundError if pymongo is not installed."""
        importlib.import_module('cli.status')

    def test_bson_decode_is_the_pymongo_flavour(self):
        """Guards against 'fixing' the dependency by declaring `bson`.

        The standalone PyPI `bson` package would satisfy `import bson` and then
        fail on `decode`, turning a clean ImportError into an AttributeError
        deep in the gdb path. Assert the API we actually use is present.
        """
        bson = importlib.import_module('bson')
        self.assertTrue(hasattr(bson, 'decode'),
                        'bson.decode missing -- the `bson` PyPI package is '
                        'installed instead of pymongo')

    def test_pymongo_is_declared_in_install_requires(self):
        """A working dev env must not be the only thing making this pass."""
        import pathlib
        setup_py = pathlib.Path(__file__).resolve().parents[3] / 'cli' / 'setup.py'
        self.assertIn('pymongo', setup_py.read_text(),
                      'cli/status.py imports bson (pymongo) but setup.py does '
                      'not declare it')


class TermiosGuardTests(unittest.TestCase):
    """`lager uart` interactive mode on a platform without termios."""

    MODULE = 'cli.commands.communication.websocket_client'

    def _reimport_without_termios(self):
        sys.modules.pop(self.MODULE, None)
        with _blocked('termios', 'tty'):
            return importlib.import_module(self.MODULE)

    def tearDown(self):
        # Restore the normally-imported module for every other test.
        sys.modules.pop(self.MODULE, None)
        importlib.import_module(self.MODULE)

    def test_module_imports_when_termios_is_unavailable(self):
        """The regression: this used to raise ImportError at import time."""
        mod = self._reimport_without_termios()
        self.assertTrue(mod._TERMIOS_IMPORT_FAILED)

    def test_sentinel_is_false_when_termios_is_available(self):
        mod = importlib.import_module(self.MODULE)
        self.assertFalse(mod._TERMIOS_IMPORT_FAILED)

    def test_sentinel_carries_the_original_exception(self):
        mod = self._reimport_without_termios()
        self.assertIsInstance(mod._TERMIOS_IMPORT_FAILED, ImportError)

    def test_interactive_setup_raises_an_actionable_error(self):
        """Must be a LagerError, not a NameError.

        Both termios call sites sit inside `except Exception`, so without the
        explicit check the missing name would be swallowed and the session
        would silently run line-buffered -- which reads to the user as a broken
        UART, not an unsupported platform.
        """
        from cli.errors import LagerError
        mod = self._reimport_without_termios()
        client = mod.UARTWebSocketClient.__new__(mod.UARTWebSocketClient)
        client.interactive = True
        client.old_tty_settings = None

        with mock.patch.object(mod.sys, 'stdin') as stdin:
            stdin.isatty.return_value = True
            with self.assertRaises(LagerError) as ctx:
                client._setup_terminal()
        self.assertIn('POSIX terminal', str(ctx.exception))

    def test_non_interactive_does_not_raise_without_termios(self):
        """Piped `lager uart` must keep working on any platform."""
        mod = self._reimport_without_termios()
        client = mod.UARTWebSocketClient.__new__(mod.UARTWebSocketClient)
        client.interactive = False
        client.old_tty_settings = None
        client._setup_terminal()          # must not raise

    def test_non_tty_stdin_does_not_raise_without_termios(self):
        mod = self._reimport_without_termios()
        client = mod.UARTWebSocketClient.__new__(mod.UARTWebSocketClient)
        client.interactive = True
        client.old_tty_settings = None
        with mock.patch.object(mod.sys, 'stdin') as stdin:
            stdin.isatty.return_value = False
            client._setup_terminal()      # must not raise

    def test_restore_terminal_is_safe_without_termios(self):
        mod = self._reimport_without_termios()
        client = mod.UARTWebSocketClient.__new__(mod.UARTWebSocketClient)
        client.old_tty_settings = None
        client._restore_terminal()        # must not raise


class GuardConsistencyTests(unittest.TestCase):

    def test_status_py_guards_termios_too(self):
        """The pattern this file's fix was modelled on; keep them in step."""
        mod = importlib.import_module('cli.status')
        self.assertTrue(hasattr(mod, '_TERMIOS_IMPORT_FAILED'))

    def test_no_other_cli_module_imports_termios_unguarded(self):
        """Catches the next module that reintroduces the bug.

        Scans cli/ for a top-level `import termios` that is not inside a
        try block. Cheap, and it fails on the source rather than waiting for
        someone to run Windows.
        """
        import pathlib
        import re
        root = pathlib.Path(__file__).resolve().parents[3] / 'cli'
        offenders = []
        for path in root.rglob('*.py'):
            if 'elftools' in path.parts or 'vendor' in path.parts:
                continue
            lines = path.read_text(errors='replace').splitlines()
            for i, line in enumerate(lines):
                if not re.match(r'^import (termios|tty)\b', line):
                    continue
                # Guarded if a `try:` opens within the preceding few lines.
                window = lines[max(0, i - 4):i]
                if not any(w.strip() == 'try:' for w in window):
                    offenders.append(f'{path.relative_to(root.parent)}:{i + 1}')
        self.assertEqual(offenders, [],
                         'unguarded termios/tty import(s); wrap in try/except '
                         'like cli/status.py and cli/commands/communication/'
                         'websocket_client.py do')


if __name__ == '__main__':
    unittest.main()
