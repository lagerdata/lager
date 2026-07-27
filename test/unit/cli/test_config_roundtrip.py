#!/usr/bin/env python3
# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0
"""
Unit tests for ``cli/config.py`` beyond the caching layer.

``.lager`` files are JSON on disk but are handed to callers as a ConfigParser,
so every read and write crosses ``_json_to_configparser`` /
``_configparser_to_json``. That pair carries the legacy-key migration
(``LAGER`` -> ``DEFAULTS``, lowercase ``devenv``/``debug`` -> uppercase) and the
preserve-unknown-fields guarantee that stops a write from destroying keys the
CLI does not know about. Only the cache had coverage.

Everything here is hermetic: config paths are redirected into a temp dir via
``LAGER_CONFIG_FILE_DIR`` or passed explicitly, and the module-level caches are
cleared between tests so a stale entry cannot make a test pass.
"""

import configparser
import json
import os
import shutil
import tempfile
import unittest
from unittest import mock

from cli import config as cfg
from cli.errors import LagerError


class ConfigTestCase(unittest.TestCase):
    """Temp dir + a clean module cache for every test."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        cfg._config_cache.clear()
        cfg._config_cache_mtime.clear()
        self.addCleanup(cfg._config_cache.clear)
        self.addCleanup(cfg._config_cache_mtime.clear)

    def write_json(self, data, name='.lager'):
        path = os.path.join(self.tmp, name)
        with open(path, 'w') as f:
            json.dump(data, f)
        return path

    def path(self, name='.lager'):
        return os.path.join(self.tmp, name)


class JsonToConfigParserTests(ConfigTestCase):

    def test_lager_section_always_exists(self):
        """Callers do config.get('LAGER', ...) unguarded."""
        self.assertTrue(cfg._json_to_configparser({}).has_section('LAGER'))

    def test_defaults_becomes_the_lager_section(self):
        c = cfg._json_to_configparser({'DEFAULTS': {'box': 'bench-1'}})
        self.assertEqual(c.get('LAGER', 'box'), 'bench-1')

    def test_legacy_lager_key_is_read_as_defaults(self):
        """Old files stored the block under 'LAGER'."""
        c = cfg._json_to_configparser({'LAGER': {'box': 'bench-1'}})
        self.assertEqual(c.get('LAGER', 'box'), 'bench-1')

    def test_defaults_wins_when_both_are_present(self):
        c = cfg._json_to_configparser({
            'DEFAULTS': {'box': 'new'}, 'LAGER': {'box': 'old'}})
        self.assertEqual(c.get('LAGER', 'box'), 'new')

    def test_devenv_and_debug_sections(self):
        c = cfg._json_to_configparser({
            'DEVENV': {'image': 'img'}, 'DEBUG': {'swd': './x.JLinkScript'}})
        self.assertEqual(c.get(cfg.DEVENV_SECTION_NAME, 'image'), 'img')
        self.assertEqual(c.get(cfg.DEBUG_SECTION_NAME, 'swd'), './x.JLinkScript')

    def test_lowercase_legacy_section_names(self):
        c = cfg._json_to_configparser({
            'devenv': {'image': 'img'}, 'debug': {'swd': 's'}})
        self.assertEqual(c.get(cfg.DEVENV_SECTION_NAME, 'image'), 'img')
        self.assertEqual(c.get(cfg.DEBUG_SECTION_NAME, 'swd'), 's')

    def test_values_are_stringified(self):
        """ConfigParser only stores strings; ints and bools must survive."""
        c = cfg._json_to_configparser({'DEFAULTS': {'port': 9000, 'on': True}})
        self.assertEqual(c.get('LAGER', 'port'), '9000')
        self.assertEqual(c.get('LAGER', 'on'), 'True')

    def test_non_dict_defaults_is_ignored_not_fatal(self):
        c = cfg._json_to_configparser({'DEFAULTS': 'not-a-dict'})
        self.assertEqual(dict(c.items('LAGER')), {})

    def test_absent_optional_sections_are_not_created(self):
        c = cfg._json_to_configparser({'DEFAULTS': {'box': 'b'}})
        self.assertFalse(c.has_section(cfg.DEVENV_SECTION_NAME))
        self.assertFalse(c.has_section(cfg.DEBUG_SECTION_NAME))


class ConfigParserToJsonTests(ConfigTestCase):

    def _parser(self, sections):
        c = configparser.ConfigParser()
        for name, items in sections.items():
            c.add_section(name)
            for k, v in items.items():
                c.set(name, k, v)
        return c

    def test_lager_section_becomes_defaults(self):
        out = cfg._configparser_to_json(self._parser({'LAGER': {'box': 'b'}}))
        self.assertEqual(out['DEFAULTS'], {'box': 'b'})

    def test_unknown_top_level_keys_are_preserved(self):
        """The whole reason existing_json is threaded through.

        A write must not drop fields the CLI does not model, or an upgrade
        silently destroys user data.
        """
        out = cfg._configparser_to_json(
            self._parser({'LAGER': {'box': 'b'}}),
            existing_json={'CUSTOM': {'keep': 'me'}, 'other': [1, 2]})
        self.assertEqual(out['CUSTOM'], {'keep': 'me'})
        self.assertEqual(out['other'], [1, 2])

    def test_existing_json_is_not_mutated(self):
        existing = {'CUSTOM': {'keep': 'me'}}
        cfg._configparser_to_json(self._parser({'LAGER': {'box': 'b'}}), existing)
        self.assertEqual(existing, {'CUSTOM': {'keep': 'me'}})

    def test_empty_lager_section_removes_defaults(self):
        out = cfg._configparser_to_json(
            self._parser({'LAGER': {}}), existing_json={'DEFAULTS': {'box': 'b'}})
        self.assertNotIn('DEFAULTS', out)

    def test_legacy_lager_key_is_dropped_on_write(self):
        out = cfg._configparser_to_json(
            self._parser({'LAGER': {'box': 'b'}}), existing_json={'LAGER': {'box': 'old'}})
        self.assertNotIn('LAGER', out)
        self.assertEqual(out['DEFAULTS'], {'box': 'b'})

    def test_legacy_lowercase_keys_are_dropped_when_upgraded(self):
        out = cfg._configparser_to_json(
            self._parser({cfg.DEVENV_SECTION_NAME: {'image': 'img'}}),
            existing_json={'devenv': {'image': 'old'}})
        self.assertNotIn('devenv', out)
        self.assertEqual(out['DEVENV'], {'image': 'img'})

    def test_configparser_lowercases_keys(self):
        """ConfigParser normalises option names; pinned so it is not a surprise."""
        out = cfg._configparser_to_json(self._parser({'LAGER': {'MyKey': 'v'}}))
        self.assertEqual(out['DEFAULTS'], {'mykey': 'v'})


class RoundTripTests(ConfigTestCase):

    def test_json_to_parser_and_back_is_stable(self):
        original = {
            'DEFAULTS': {'box': 'bench-1'},
            'DEVENV': {'image': 'img'},
            'DEBUG': {'swd': './x.JLinkScript'},
        }
        out = cfg._configparser_to_json(cfg._json_to_configparser(original), dict(original))
        self.assertEqual(out, original)

    def test_round_trip_upgrades_legacy_layout(self):
        legacy = {'LAGER': {'box': 'b'}, 'devenv': {'image': 'i'}}
        out = cfg._configparser_to_json(cfg._json_to_configparser(legacy), dict(legacy))
        self.assertEqual(out['DEFAULTS'], {'box': 'b'})
        self.assertEqual(out['DEVENV'], {'image': 'i'})
        self.assertNotIn('LAGER', out)
        self.assertNotIn('devenv', out)

    def test_round_trip_is_idempotent(self):
        original = {'DEFAULTS': {'box': 'b'}, 'DEVENV': {'image': 'i'}}
        once = cfg._configparser_to_json(cfg._json_to_configparser(original), dict(original))
        twice = cfg._configparser_to_json(cfg._json_to_configparser(once), dict(once))
        self.assertEqual(once, twice)


class ReadLagerJsonTests(ConfigTestCase):

    def test_reads_a_file(self):
        p = self.write_json({'DEFAULTS': {'box': 'b'}})
        self.assertEqual(cfg.read_lager_json(p), {'DEFAULTS': {'box': 'b'}})

    def test_missing_file_is_an_empty_dict(self):
        self.assertEqual(cfg.read_lager_json(self.path('nope')), {})

    def test_invalid_json_is_an_empty_dict_not_a_raise(self):
        """Contrast with read_config_file, which raises a LagerError."""
        p = self.path()
        with open(p, 'w') as f:
            f.write('{not json')
        self.assertEqual(cfg.read_lager_json(p), {})

    def test_none_path_with_no_project_config_is_empty(self):
        with mock.patch.object(cfg, 'find_devenv_config_path', return_value=None):
            self.assertEqual(cfg.read_lager_json(), {})


class WriteLagerJsonTests(ConfigTestCase):

    def test_writes_and_reads_back(self):
        p = self.path()
        cfg.write_lager_json({'DEFAULTS': {'box': 'b'}}, p)
        self.assertEqual(cfg.read_lager_json(p), {'DEFAULTS': {'box': 'b'}})

    def test_write_invalidates_the_cache(self):
        """A stale ConfigParser after a write would serve the old values."""
        p = self.write_json({'DEFAULTS': {'box': 'old'}})
        self.assertEqual(cfg.read_config_file(p).get('LAGER', 'box'), 'old')
        self.assertIn(p, cfg._config_cache)
        cfg.write_lager_json({'DEFAULTS': {'box': 'new'}}, p)
        self.assertNotIn(p, cfg._config_cache)
        self.assertEqual(cfg.read_config_file(p).get('LAGER', 'box'), 'new')

    def test_no_path_and_no_project_config_raises(self):
        with mock.patch.object(cfg, 'find_devenv_config_path', return_value=None):
            with self.assertRaises(ValueError):
                cfg.write_lager_json({'a': 1})

    def test_write_config_file_preserves_unknown_fields_on_disk(self):
        p = self.write_json({'DEFAULTS': {'box': 'b'}, 'CUSTOM': {'k': 'v'}})
        parser = cfg.read_config_file(p)
        parser.set('LAGER', 'box', 'changed')
        cfg.write_config_file(parser, p)
        on_disk = cfg.read_lager_json(p)
        self.assertEqual(on_disk['DEFAULTS']['box'], 'changed')
        self.assertEqual(on_disk['CUSTOM'], {'k': 'v'})


class ReadConfigFileTests(ConfigTestCase):

    def test_missing_file_gives_an_empty_lager_section(self):
        c = cfg.read_config_file(self.path('nope'))
        self.assertTrue(c.has_section('LAGER'))
        self.assertEqual(dict(c.items('LAGER')), {})

    def test_invalid_json_raises_lager_error(self):
        p = self.path()
        with open(p, 'w') as f:
            f.write('{not json')
        with self.assertRaises(LagerError):
            cfg.read_config_file(p)

    def test_result_is_cached_until_mtime_changes(self):
        p = self.write_json({'DEFAULTS': {'box': 'b'}})
        first = cfg.read_config_file(p)
        self.assertIs(cfg.read_config_file(p), first)

    def test_cache_is_keyed_per_path(self):
        a = self.write_json({'DEFAULTS': {'box': 'a'}}, '.lager')
        b = self.write_json({'DEFAULTS': {'box': 'b'}}, '.lager2')
        self.assertEqual(cfg.read_config_file(a).get('LAGER', 'box'), 'a')
        self.assertEqual(cfg.read_config_file(b).get('LAGER', 'box'), 'b')


class ExpandDevenvPathTests(ConfigTestCase):

    def test_project_root_token_braced_and_bare(self):
        for token in ('${PROJECT_ROOT}', '$PROJECT_ROOT',
                      '${LAGER_PROJECT_ROOT}', '$LAGER_PROJECT_ROOT'):
            with self.subTest(token=token):
                self.assertEqual(cfg.expand_devenv_path(f'{token}/src', '/proj'),
                                 '/proj/src')

    def test_tilde_expands_to_home(self):
        self.assertEqual(cfg.expand_devenv_path('~/x', '/proj'),
                         os.path.join(os.path.expanduser('~'), 'x'))

    def test_environment_variables_expand(self):
        with mock.patch.dict(os.environ, {'MY_DIR': '/opt/thing'}):
            self.assertEqual(cfg.expand_devenv_path('$MY_DIR/sub', '/proj'),
                             '/opt/thing/sub')

    def test_volume_spec_with_a_colon_keeps_its_container_side(self):
        """Specs are `host:container`; only the host side should expand."""
        self.assertEqual(cfg.expand_devenv_path('${PROJECT_ROOT}/src:/work', '/proj'),
                         '/proj/src:/work')

    def test_empty_and_none_pass_through(self):
        self.assertEqual(cfg.expand_devenv_path('', '/proj'), '')
        self.assertIsNone(cfg.expand_devenv_path(None, '/proj'))

    def test_unset_variable_is_left_alone(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertEqual(cfg.expand_devenv_path('$NOT_SET/x', '/proj'),
                             '$NOT_SET/x')

    def test_plain_absolute_path_is_unchanged(self):
        self.assertEqual(cfg.expand_devenv_path('/already/abs', '/proj'), '/already/abs')


class DevenvConfigListTests(ConfigTestCase):

    def test_list_input(self):
        self.assertEqual(cfg.devenv_config_list(['a', 'b']), ['a', 'b'])

    def test_newline_separated_string(self):
        self.assertEqual(cfg.devenv_config_list('a\nb'), ['a', 'b'])

    def test_blank_entries_are_dropped_and_values_stripped(self):
        self.assertEqual(cfg.devenv_config_list(' a \n\n  \nb '), ['a', 'b'])

    def test_empty_inputs(self):
        for value in (None, '', []):
            with self.subTest(value=value):
                self.assertEqual(cfg.devenv_config_list(value), [])

    def test_non_string_items_are_stringified(self):
        self.assertEqual(cfg.devenv_config_list([1, 2]), ['1', '2'])


class GetDebugScriptForNetTests(ConfigTestCase):
    """`lager nets set-script` writes these; `lager debug` reads them."""

    def setUp(self):
        super().setUp()
        self.script = os.path.join(self.tmp, 'dev.JLinkScript')
        with open(self.script, 'w') as f:
            f.write('// script')

    def _with_config(self, debug_section):
        p = self.write_json({'DEBUG': debug_section})
        return mock.patch.object(cfg, 'find_devenv_config_path', return_value=p)

    def test_absolute_path_is_returned(self):
        with self._with_config({'swd': self.script}):
            self.assertEqual(cfg.get_debug_script_for_net('swd'), self.script)

    def test_relative_path_resolves_against_the_config_file(self):
        """Not against cwd -- that is the point of the feature."""
        with self._with_config({'swd': './dev.JLinkScript'}):
            with mock.patch.object(os, 'getcwd', return_value='/somewhere/else'):
                self.assertEqual(cfg.get_debug_script_for_net('swd'), self.script)

    def test_missing_file_returns_none(self):
        with self._with_config({'swd': '/nope/absent.JLinkScript'}):
            self.assertIsNone(cfg.get_debug_script_for_net('swd'))

    def test_unknown_net_returns_none(self):
        with self._with_config({'swd': self.script}):
            self.assertIsNone(cfg.get_debug_script_for_net('other'))

    def test_no_debug_section_returns_none(self):
        p = self.write_json({'DEFAULTS': {'box': 'b'}})
        with mock.patch.object(cfg, 'find_devenv_config_path', return_value=p):
            self.assertIsNone(cfg.get_debug_script_for_net('swd'))

    def test_no_config_file_returns_none(self):
        with mock.patch.object(cfg, 'find_devenv_config_path', return_value=None):
            self.assertIsNone(cfg.get_debug_script_for_net('swd'))

    def test_net_name_lookup_is_case_insensitive(self):
        """ConfigParser lowercases option names, so a net saved as 'SWD'
        is retrievable as 'swd'. Pinned because net names elsewhere in the
        CLI are case-sensitive, and this asymmetry is easy to break."""
        with self._with_config({'SWD': self.script}):
            self.assertEqual(cfg.get_debug_script_for_net('swd'), self.script)


class MakeConfigPathTests(ConfigTestCase):

    def test_uses_the_default_filename(self):
        self.assertEqual(cfg.make_config_path('/d'),
                         os.path.join('/d', cfg.LAGER_CONFIG_FILE_NAME))

    def test_explicit_filename_overrides(self):
        self.assertEqual(cfg.make_config_path('/d', '.other'), '/d/.other')

    def test_global_path_honours_the_dir_override(self):
        with mock.patch.dict(os.environ, {'LAGER_CONFIG_FILE_DIR': self.tmp}):
            self.assertEqual(cfg.get_global_config_file_path(),
                             os.path.join(self.tmp, cfg.LAGER_CONFIG_FILE_NAME))

    def test_global_path_defaults_to_home(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertEqual(cfg.get_global_config_file_path(),
                             os.path.join(os.path.expanduser('~'),
                                          cfg.LAGER_CONFIG_FILE_NAME))


if __name__ == '__main__':
    unittest.main()
