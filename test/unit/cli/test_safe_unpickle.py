#!/usr/bin/env python3
# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0
"""
Unit tests for ``cli/safe_unpickle.py`` -- the deserialization allowlist.

``restricted_loads`` exists so that untrusted pickle payloads cannot reach
arbitrary constructors. ``pickle`` normally imports and calls whatever module
and name a payload asks for, which is remote code execution by design; the
``RestrictedUnpickler.find_class`` override is the only thing standing between
a payload and that behaviour.

The tests that matter here are the negative ones, and specifically
``test_forbidden_global_is_not_imported``: rejecting a name is not enough if
the module was already imported to discover that, because importing is itself
the side effect an attacker wants. ``find_class`` checks the allowlist BEFORE
calling ``importlib.import_module``, and that ordering is what these pin.
"""

import io
import pickle
import pickletools
import sys
import unittest

from cli.safe_unpickle import RestrictedUnpickler, defaults, restricted_loads


def _payload(module, name, args=()):
    """Hand-roll a pickle that calls ``module.name(*args)``.

    Built with GLOBAL/REDUCE opcodes rather than by pickling a live object, so
    a payload can name something this process has never imported -- which is
    the whole scenario under test.
    """
    parts = [pickle.GLOBAL + f'{module}\n{name}\n'.encode()]
    parts.append(pickle.MARK)
    for a in args:
        parts.append(pickle.UNICODE + f'{a}\n'.encode())
    parts.append(pickle.TUPLE)
    parts.append(pickle.REDUCE)
    parts.append(pickle.STOP)
    return b''.join(parts)


class AllowedGlobalsTests(unittest.TestCase):
    """Every entry in ``defaults`` must actually load."""

    def test_builtins_range(self):
        self.assertEqual(restricted_loads(pickle.dumps(range(3))), range(3))

    def test_builtins_complex(self):
        self.assertEqual(restricted_loads(pickle.dumps(complex(1, 2))), complex(1, 2))

    def test_builtins_set_and_frozenset(self):
        self.assertEqual(restricted_loads(pickle.dumps({1, 2})), {1, 2})
        self.assertEqual(restricted_loads(pickle.dumps(frozenset([1]))), frozenset([1]))

    def test_builtins_slice(self):
        self.assertEqual(restricted_loads(pickle.dumps(slice(1, 5, 2))), slice(1, 5, 2))

    def test_datetime_types(self):
        import datetime
        for value in (datetime.date(2026, 7, 27),
                      datetime.time(13, 45),
                      datetime.datetime(2026, 7, 27, 13, 45)):
            with self.subTest(value=value):
                self.assertEqual(restricted_loads(pickle.dumps(value)), value)

    def test_every_default_entry_is_reachable(self):
        """The allowlist must not name something that cannot be resolved.

        A typo in ``defaults`` would silently narrow the allowlist rather than
        fail, so assert every declared name really exists on its module.
        """
        import importlib
        for module, names in defaults.items():
            mod = importlib.import_module(module)
            for name in names:
                with self.subTest(module=module, name=name):
                    self.assertTrue(hasattr(mod, name),
                                    f'{module}.{name} is allowlisted but does not exist')


class ForbiddenGlobalsTests(unittest.TestCase):

    def test_unlisted_name_in_listed_module_is_refused(self):
        # `builtins` is an allowed module, but `eval` is not an allowed name.
        with self.assertRaises(pickle.UnpicklingError) as ctx:
            restricted_loads(_payload('builtins', 'eval', ('1+1',)))
        self.assertIn('forbidden', str(ctx.exception))

    def test_unlisted_module_is_refused(self):
        with self.assertRaises(pickle.UnpicklingError):
            restricted_loads(_payload('os', 'system', ('true',)))

    def test_error_names_the_rejected_global(self):
        """The message has to say what was refused, or it is undebuggable."""
        with self.assertRaises(pickle.UnpicklingError) as ctx:
            restricted_loads(_payload('subprocess', 'Popen'))
        self.assertIn('subprocess.Popen', str(ctx.exception))

    def test_forbidden_global_is_not_imported(self):
        """Rejection must happen BEFORE the module is imported.

        ``find_class`` checks ``self.safe`` first and only then calls
        ``importlib.import_module``. If that order were reversed the unpickler
        would import attacker-named modules as a side effect of refusing them
        -- executing their module-level code, which is most of what an
        arbitrary-import primitive is worth.
        """
        victim = 'this_module_should_never_be_imported_by_unpickling'
        self.assertNotIn(victim, sys.modules)
        with self.assertRaises(pickle.UnpicklingError):
            restricted_loads(_payload(victim, 'anything'))
        self.assertNotIn(victim, sys.modules)

    def test_reduce_payload_does_not_execute(self):
        """A REDUCE payload naming a forbidden callable must not run it."""
        marker = []
        import builtins
        builtins._lager_unpickle_canary = marker.append  # type: ignore[attr-defined]
        try:
            with self.assertRaises(pickle.UnpicklingError):
                restricted_loads(_payload('builtins', '_lager_unpickle_canary', ('x',)))
            self.assertEqual(marker, [], 'the forbidden callable was invoked')
        finally:
            del builtins._lager_unpickle_canary  # type: ignore[attr-defined]


class CustomAllowlistTests(unittest.TestCase):

    def test_custom_safe_dict_overrides_defaults(self):
        safe = {'builtins': {'set'}}
        self.assertEqual(restricted_loads(pickle.dumps({1}), safe=safe), {1})
        # `range` is in `defaults` but not in this caller's allowlist.
        with self.assertRaises(pickle.UnpicklingError):
            restricted_loads(pickle.dumps(range(3)), safe=safe)

    def test_empty_allowlist_refuses_everything(self):
        with self.assertRaises(pickle.UnpicklingError):
            restricted_loads(pickle.dumps(range(3)), safe={})

    def test_defaults_is_not_mutated_by_a_custom_call(self):
        before = {k: set(v) for k, v in defaults.items()}
        restricted_loads(pickle.dumps({1}), safe={'builtins': {'set'}})
        self.assertEqual({k: set(v) for k, v in defaults.items()}, before)

    def test_plain_data_needs_no_allowlist_entry(self):
        """Primitives use dedicated opcodes and never reach find_class."""
        payload = {'a': [1, 2.5, True, None], 'b': ('x', b'y')}
        self.assertEqual(restricted_loads(pickle.dumps(payload)), payload)


class UnpicklerWiringTests(unittest.TestCase):

    def test_legacy_python2_module_spelling_is_refused(self):
        """``__builtin__`` (the Python-2 name) is not an allowlist key.

        Deliberately NOT claiming this proves ``fix_imports=False`` matters:
        measured both ways, and the flag makes no observable difference here,
        because find_class receives the module string as written either way.
        This pins the reachable behaviour -- the old spelling is refused --
        and nothing more.
        """
        with self.assertRaises(pickle.UnpicklingError):
            restricted_loads(_payload('__builtin__', 'set'))

    def test_find_class_is_callable_directly(self):
        u = RestrictedUnpickler({'builtins': {'set'}}, io.BytesIO(b''))
        self.assertIs(u.find_class('builtins', 'set'), set)
        with self.assertRaises(pickle.UnpicklingError):
            u.find_class('builtins', 'eval')

    def test_handcrafted_payloads_are_well_formed(self):
        """Guard the test helper itself.

        If ``_payload`` emitted malformed pickles, the negative tests above
        would pass for the wrong reason -- a parse failure rather than the
        allowlist. pickletools validates the opcode stream without executing.
        """
        pickletools.dis(_payload('builtins', 'eval', ('1+1',)), out=io.StringIO())


if __name__ == '__main__':
    unittest.main()
