#!/usr/bin/env python3
# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0
"""
Unit tests for cli/commands/box/_ssh.py helper functions.

Tests run locally — do NOT upload via `lager python`.
"""

import os
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(__file__)))))

from cli.commands.box._ssh import ensure_lager_box_keypair, key_auth_works
from cli.errors import LagerError


class TestEnsureLagerBoxKeypair(unittest.TestCase):

    def test_raises_lager_error_when_ssh_keygen_missing(self):
        """When ssh-keygen is not on PATH, must raise LagerError (not FileNotFoundError)."""
        with tempfile.TemporaryDirectory() as d:
            key_path = os.path.join(d, 'lager_box')
            with patch('shutil.which', return_value=None):
                with self.assertRaises(LagerError):
                    ensure_lager_box_keypair(key_path)

    def test_returns_false_when_key_already_exists(self):
        """If the key file already exists, returns False without calling ssh-keygen."""
        with tempfile.TemporaryDirectory() as d:
            key_path = os.path.join(d, 'lager_box')
            open(key_path, 'w').close()  # create the file
            result = ensure_lager_box_keypair(key_path)
            self.assertFalse(result)


class TestKeyAuthWorks(unittest.TestCase):

    def test_returns_false_when_ssh_missing(self):
        """When ssh is not on PATH, returns False instead of raising."""
        with patch('shutil.which', return_value=None):
            result = key_auth_works('user@192.0.2.1')
        self.assertFalse(result)

    def test_returns_false_on_oserror(self):
        """When subprocess.run raises OSError (e.g. missing DLL), returns False."""
        with patch('shutil.which', return_value='/usr/bin/ssh'):
            with patch('subprocess.run', side_effect=OSError('test error')):
                result = key_auth_works('user@192.0.2.1')
        self.assertFalse(result)


if __name__ == '__main__':
    unittest.main()
