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

from cli.commands.box._ssh import (
    box_has_control_plane,
    ensure_lager_box_keypair,
    key_installed_on_box,
    box_accepts_a_password,
    box_auth_methods,
    remove_lager_box_key,
    working_identity_args,
)
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


class TestKeyInstalledOnBox(unittest.TestCase):
    """Replaces the old key_auth_works probe, which inferred "the key is
    installed" from "something authenticated" -- true of any identity ssh
    offers, including a `Host *` IdentityFile from ssh_config.

    Note these assert None, not False: "could not ask the box" is a third
    outcome, and collapsing it into "not installed" would make callers
    reinstall a key that is already there."""

    def test_returns_none_when_ssh_missing(self):
        """When ssh is not on PATH, returns None instead of raising."""
        with patch('shutil.which', return_value=None):
            result = key_installed_on_box('user@192.0.2.1')
        self.assertIsNone(result)

    def test_returns_none_on_oserror(self):
        """When subprocess.run raises OSError (e.g. missing DLL), returns None."""
        with patch('shutil.which', return_value='/usr/bin/ssh'):
            with patch('subprocess.run', side_effect=OSError('test error')):
                result = key_installed_on_box('user@192.0.2.1')
        self.assertIsNone(result)


class TestBoxHasControlPlane(unittest.TestCase):
    """The answer decides whether a key gets installed at all, so it has to be
    gettable BEFORE lager_box is on the box -- over whatever identity the
    operator already has. A lone `-i lager_box` would withdraw exactly those
    (ssh replaces its identity list rather than appending) and answer "not
    managed" for every box the key had been purged from."""

    def test_offers_more_than_the_lager_box_key(self):
        seen = {}

        def fake_run(argv, **kwargs):
            seen['argv'] = argv
            class P:
                returncode = 0
            return P()

        with patch('shutil.which', return_value='/usr/bin/ssh'), \
             patch('cli.commands.box._ssh.widened_identity_args',
                   return_value=['-i', '/k/lager_box', '-i', '/k/id_ed25519']), \
             patch('subprocess.run', fake_run):
            self.assertTrue(box_has_control_plane('user@192.0.2.1'))
        self.assertIn('/k/id_ed25519', seen['argv'])

    def test_unreachable_box_is_not_reported_as_managed(self):
        """A network failure must not turn a managed box into an unmanaged one
        -- but it must not invent management either. False here sends the
        caller down the password path, where the question is asked again over
        a connection that works."""
        with patch('shutil.which', return_value='/usr/bin/ssh'):
            with patch('subprocess.run', side_effect=OSError('unreachable')):
                self.assertFalse(box_has_control_plane('user@192.0.2.1'))

    def test_missing_ssh_answers_false(self):
        with patch('shutil.which', return_value=None):
            self.assertFalse(box_has_control_plane('user@192.0.2.1'))


class TestRemoveLagerBoxKey(unittest.TestCase):

    def test_no_public_key_means_nothing_to_remove(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertFalse(
                remove_lager_box_key('user@192.0.2.1',
                                     key_path=os.path.join(d, 'lager_box'))
            )

    def test_matches_on_the_blob_not_the_comment(self):
        """The line ssh-copy-id wrote and the line a key manager re-rendered
        differ in comment and agree in blob."""
        seen = {}

        def fake_run(argv, **kwargs):
            seen['cmd'] = argv[-1]
            class P:
                returncode = 0
            return P()

        with tempfile.TemporaryDirectory() as d:
            key_path = os.path.join(d, 'lager_box')
            with open(f'{key_path}.pub', 'w') as fh:
                fh.write('ssh-ed25519 AAAABLOB lager-box-access\n')
            with patch('shutil.which', return_value='/usr/bin/ssh'), \
                 patch('subprocess.run', fake_run):
                self.assertTrue(
                    remove_lager_box_key('user@192.0.2.1', key_path=key_path)
                )
        self.assertIn("grep -vF 'AAAABLOB'", seen['cmd'])
        self.assertNotIn('lager-box-access', seen['cmd'])

    def test_unreachable_box_reports_failure(self):
        """Never report a removal that did not happen: the caller warns the
        operator the key is still there, and a silent False would hide it."""
        with tempfile.TemporaryDirectory() as d:
            key_path = os.path.join(d, 'lager_box')
            with open(f'{key_path}.pub', 'w') as fh:
                fh.write('ssh-ed25519 AAAABLOB lager-box-access\n')
            with patch('shutil.which', return_value='/usr/bin/ssh'), \
                 patch('subprocess.run', side_effect=OSError('unreachable')):
                self.assertFalse(
                    remove_lager_box_key('user@192.0.2.1', key_path=key_path)
                )


class TestWorkingIdentityArgs(unittest.TestCase):
    """Passing no -i is not the same as offering ssh's defaults.

    An IdentityFile in ssh_config replaces ssh's built-in list exactly as -i
    does. Found on hardware: `ssh -G` for a managed box resolved to id_rsa and
    stout_ed25519 while its authorized_keys held id_ed25519, so a connection
    passing no identity was refused for a box the probe had just reached --
    the probe named the defaults, the connection let the config drop them."""

    def test_names_the_defaults_even_with_no_lager_box(self):
        """widened_identity_args returns [] here; this one must not, because
        [] is what lets the config narrow the list."""
        with patch('cli.commands.box._ssh.lager_box_key_if_present',
                   return_value=None), \
             patch('cli.commands.box._ssh.default_identities_if_present',
                   return_value=['/k/id_ed25519', '/k/id_rsa']):
            args = working_identity_args()
        self.assertEqual(args, ['-i', '/k/id_ed25519', '-i', '/k/id_rsa'])

    def test_lager_box_keeps_its_precedence(self):
        with patch('cli.commands.box._ssh.lager_box_key_if_present',
                   return_value='/k/lager_box'), \
             patch('cli.commands.box._ssh.default_identities_if_present',
                   return_value=['/k/id_ed25519']):
            args = working_identity_args()
        self.assertEqual(args, ['-i', '/k/lager_box', '-i', '/k/id_ed25519'])

    def test_nothing_to_name_leaves_ssh_alone(self):
        with patch('cli.commands.box._ssh.lager_box_key_if_present',
                   return_value=None), \
             patch('cli.commands.box._ssh.default_identities_if_present',
                   return_value=[]):
            self.assertEqual(working_identity_args(), [])


class TestBoxAuthMethods(unittest.TestCase):
    """PreferredAuthentications=none offers nothing, so sshd refuses at once
    and names what it would have taken. It is the only way to ask what a box
    accepts without first satisfying it."""

    def _stderr(self, text):
        class P:
            returncode = 255
            stderr = text
        with patch('shutil.which', return_value='/usr/bin/ssh'), \
             patch('subprocess.run', return_value=P()):
            return box_auth_methods('user@192.0.2.1')

    def test_parses_the_offered_methods(self):
        methods = self._stderr(
            'user@host: Permission denied (publickey,password,'
            'keyboard-interactive).\n')
        self.assertEqual(methods,
                         frozenset({'publickey', 'password',
                                    'keyboard-interactive'}))

    def test_a_hardened_box_offers_only_publickey(self):
        self.assertEqual(self._stderr('Permission denied (publickey).\n'),
                         frozenset({'publickey'}))

    def test_unrecognised_output_is_not_an_answer(self):
        """None, not an empty set: "could not ask" and "offers nothing" lead
        to different things being said to the operator."""
        self.assertIsNone(self._stderr('ssh: connect to host ... timed out\n'))

    def test_unreachable_box_is_not_an_answer_either(self):
        with patch('shutil.which', return_value='/usr/bin/ssh'), \
             patch('subprocess.run', side_effect=OSError('down')):
            self.assertIsNone(box_auth_methods('user@192.0.2.1'))


class TestBoxAcceptsAPassword(unittest.TestCase):

    def test_password_method_means_yes(self):
        with patch('cli.commands.box._ssh.box_auth_methods',
                   return_value=frozenset({'publickey', 'password'})):
            self.assertIs(box_accepts_a_password('user@192.0.2.1'), True)

    def test_keyboard_interactive_counts_too(self):
        """A box can take a password through kbd-interactive alone; treating
        that as "no password" would refuse a bootstrap that would work."""
        with patch('cli.commands.box._ssh.box_auth_methods',
                   return_value=frozenset({'publickey',
                                           'keyboard-interactive'})):
            self.assertIs(box_accepts_a_password('user@192.0.2.1'), True)

    def test_publickey_only_means_no(self):
        with patch('cli.commands.box._ssh.box_auth_methods',
                   return_value=frozenset({'publickey'})):
            self.assertIs(box_accepts_a_password('user@192.0.2.1'), False)

    def test_unknown_stays_unknown(self):
        with patch('cli.commands.box._ssh.box_auth_methods',
                   return_value=None):
            self.assertIsNone(box_accepts_a_password('user@192.0.2.1'))


if __name__ == '__main__':
    unittest.main()
