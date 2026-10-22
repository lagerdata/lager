# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0
"""Unit tests for lager.drivers.manifest: the custom-driver manifest rules.

The manifest is the whole contract the CLI sees, so every rule here is one a
bad bundle would otherwise break at call time on a box.
"""

import copy
import json
import os
import unittest

from lager.drivers import manifest as m

SAMPLE = os.path.join(os.path.dirname(__file__), '..', '..', 'assets', 'drivers',
                      'sample_stepper')

BASE = {
    'type': 'stepper',
    'version': '1.0.0',
    'lager_api': 1,
    'params': {'uart_net': {'type': 'net', 'role': 'uart', 'required': True}},
    'actions': {
        'position': {'help': 'Query position'},
        'jog': {'args': {'seconds': {'type': 'float', 'min': 0.1, 'max': 30,
                                     'required': True}}},
    },
}


def _with(**changes):
    data = copy.deepcopy(BASE)
    data.update(changes)
    return data


class TestParseManifest(unittest.TestCase):
    def test_valid_manifest_is_normalised(self):
        out = m.parse_manifest(BASE)
        self.assertEqual(out['type'], 'stepper')
        self.assertEqual(out['timeout_s'], m.DEFAULT_TIMEOUT_S)
        self.assertEqual(out['actions']['jog']['timeout_s'], m.DEFAULT_TIMEOUT_S)
        self.assertEqual(out['actions']['position']['args'], {})
        self.assertEqual(out['params']['uart_net']['role'], 'uart')
        self.assertFalse(out['actions']['jog']['args']['seconds'].get('default'))

    def test_sample_bundle_manifest_is_valid(self):
        out = m.load_manifest(SAMPLE)
        self.assertEqual(out['type'], 'sample_stepper')
        self.assertIn('echo', out['actions'])

    def test_rejects_bad_type_names(self):
        for bad in ('Stepper', '1stepper', 's', 'a' * 33, 'step per', 'step.per', None):
            with self.subTest(type=bad):
                with self.assertRaises(m.ManifestError):
                    m.parse_manifest(_with(type=bad))

    def test_rejects_reserved_types(self):
        for reserved in ('uart', 'supply', 'nets', 'custom', 'drivers', 'power-supply'):
            with self.subTest(type=reserved):
                with self.assertRaisesRegex(m.ManifestError, 'built-in'):
                    m.parse_manifest(_with(type=reserved))

    def test_rejects_unsupported_lager_api(self):
        with self.assertRaisesRegex(m.ManifestError, 'lager_api'):
            m.parse_manifest(_with(lager_api=2))

    def test_rejects_unknown_top_level_keys(self):
        with self.assertRaisesRegex(m.ManifestError, 'unknown keys'):
            m.parse_manifest(_with(dependencies=['numpy']))

    def test_requirements_names_a_top_level_txt_file(self):
        self.assertIsNone(m.parse_manifest(_with())['requirements'])
        self.assertEqual(m.parse_manifest(_with(requirements='requirements.txt'))['requirements'],
                         'requirements.txt')
        for bad in (['numpy'], 'sub/requirements.txt', '../requirements.txt',
                    'requirements', '.hidden.txt', ''):
            with self.subTest(bad=bad), self.assertRaisesRegex(m.ManifestError, 'requirements'):
                m.parse_manifest(_with(requirements=bad))

    def test_requires_actions(self):
        with self.assertRaises(m.ManifestError):
            m.parse_manifest(_with(actions={}))

    def test_rejects_reserved_action_names(self):
        for name in ('setup', 'teardown', '_private'):
            with self.subTest(action=name):
                with self.assertRaises(m.ManifestError):
                    m.parse_manifest(_with(actions={name: {}}))

    def test_rejects_args_the_cli_already_uses(self):
        for name in ('box', 'json', 'role', 'netname', 'quiet'):
            with self.subTest(arg=name):
                bad = _with(actions={'go': {'args': {name: {'type': 'str'}}}})
                with self.assertRaisesRegex(m.ManifestError, 'reserved'):
                    m.parse_manifest(bad)

    def test_net_param_needs_role(self):
        with self.assertRaisesRegex(m.ManifestError, 'role'):
            m.parse_manifest(_with(params={'p': {'type': 'net'}}))

    def test_args_cannot_be_nets(self):
        bad = _with(actions={'go': {'args': {'n': {'type': 'net', 'role': 'uart'}}}})
        with self.assertRaisesRegex(m.ManifestError, 'type must be one of'):
            m.parse_manifest(bad)

    def test_choice_needs_choices(self):
        bad = _with(actions={'go': {'args': {'mode': {'type': 'choice'}}}})
        with self.assertRaisesRegex(m.ManifestError, 'choice'):
            m.parse_manifest(bad)

    def test_min_above_max_rejected(self):
        bad = _with(actions={'go': {'args': {'n': {'type': 'int', 'min': 5, 'max': 1}}}})
        with self.assertRaisesRegex(m.ManifestError, 'min is above max'):
            m.parse_manifest(bad)

    def test_bounds_only_for_numbers(self):
        bad = _with(actions={'go': {'args': {'s': {'type': 'str', 'min': 1}}}})
        with self.assertRaises(m.ManifestError):
            m.parse_manifest(bad)

    def test_default_is_checked_against_its_spec(self):
        bad = _with(actions={'go': {'args': {'n': {'type': 'int', 'max': 3, 'default': 9}}}})
        with self.assertRaisesRegex(m.ManifestError, 'at most'):
            m.parse_manifest(bad)

    def test_timeout_bounds(self):
        for bad in (0, m.MAX_TIMEOUT_S + 1, 1.5, True):
            with self.subTest(timeout=bad):
                with self.assertRaises(m.ManifestError):
                    m.parse_manifest(_with(timeout_s=bad))
        out = m.parse_manifest(_with(timeout_s=5, actions={'go': {'timeout_s': 9}, 'stop': {}}))
        self.assertEqual(out['actions']['go']['timeout_s'], 9)
        self.assertEqual(out['actions']['stop']['timeout_s'], 5)

    def test_load_manifest_reports_bad_json(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            with self.assertRaisesRegex(m.ManifestError, 'missing'):
                m.load_manifest(d)
            with open(os.path.join(d, m.MANIFEST_FILE), 'w') as f:
                f.write('{not json')
            with self.assertRaisesRegex(m.ManifestError, 'not valid JSON'):
                m.load_manifest(d)
            with open(os.path.join(d, m.MANIFEST_FILE), 'w') as f:
                json.dump(BASE, f)
            self.assertEqual(m.load_manifest(d)['type'], 'stepper')


class TestCoerce(unittest.TestCase):
    def setUp(self):
        self.manifest = m.parse_manifest(_with(actions={
            'go': {'args': {
                'seconds': {'type': 'float', 'min': 0.1, 'max': 30, 'required': True},
                'count': {'type': 'int', 'default': 1, 'min': 1},
                'fast': {'type': 'bool', 'default': False},
                'mode': {'type': 'choice', 'choices': ['a', 'b'], 'default': 'a'},
                'label': {'type': 'str'},
            }},
        }))

    def test_cli_strings_are_typed(self):
        out = m.coerce_args(self.manifest, 'go',
                            {'seconds': '3', 'count': '2', 'fast': 'yes', 'mode': 'b'})
        self.assertEqual(out, {'seconds': 3.0, 'count': 2, 'fast': True, 'mode': 'b'})

    def test_defaults_fill_in_and_optional_omitted(self):
        out = m.coerce_args(self.manifest, 'go', {'seconds': 1})
        self.assertEqual(out, {'seconds': 1.0, 'count': 1, 'fast': False, 'mode': 'a'})
        self.assertNotIn('label', out)

    def test_missing_required(self):
        with self.assertRaisesRegex(m.ManifestError, "missing required arg 'seconds'"):
            m.coerce_args(self.manifest, 'go', {})

    def test_unknown_arg(self):
        with self.assertRaisesRegex(m.ManifestError, 'unknown args: speed'):
            m.coerce_args(self.manifest, 'go', {'seconds': 1, 'speed': 2})

    def test_range_and_type_errors(self):
        cases = [
            {'seconds': 0},
            {'seconds': 31},
            {'seconds': 'soon'},
            {'seconds': 1, 'count': 1.5},
            {'seconds': 1, 'count': True},
            {'seconds': 1, 'fast': 'maybe'},
            {'seconds': 1, 'mode': 'c'},
            {'seconds': float('inf')},
        ]
        for raw in cases:
            with self.subTest(raw=raw):
                with self.assertRaises(m.ManifestError):
                    m.coerce_args(self.manifest, 'go', raw)

    def test_unknown_action_is_key_error(self):
        with self.assertRaises(KeyError):
            m.coerce_args(self.manifest, 'nope', {})

    def test_params(self):
        out = m.coerce_params(self.manifest, {'uart_net': 'UART_1'})
        self.assertEqual(out, {'uart_net': 'UART_1'})
        with self.assertRaisesRegex(m.ManifestError, "missing required param 'uart_net'"):
            m.coerce_params(self.manifest, {})
        with self.assertRaisesRegex(m.ManifestError, 'unknown params'):
            m.coerce_params(self.manifest, {'uart_net': 'U', 'extra': 1})


if __name__ == '__main__':
    unittest.main()
