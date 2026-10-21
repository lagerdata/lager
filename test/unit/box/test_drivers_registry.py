# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0
"""Unit tests for lager.drivers.registry: installing custom drivers on disk.

Uses a real temp directory as DRIVERS_DIR (no os.path patching).
"""

import io
import json
import os
import shutil
import stat
import tempfile
import unittest
import zipfile

from lager.drivers import registry
from lager.drivers.errors import DriverUnavailable

MANIFEST = {
    'type': 'stepper',
    'version': '1.0.0',
    'lager_api': 1,
    'actions': {'ping': {}},
}
DRIVER = (
    'from lager.drivers import CustomDriver, action\n'
    'class Driver(CustomDriver):\n'
    '    @action\n'
    '    def ping(self):\n'
    '        return {"message": "pong"}\n'
)


def make_bundle(files=None, prefix='', manifest=None):
    if files is None:
        files = {'lager_driver.json': json.dumps(manifest or MANIFEST),
                 'driver.py': DRIVER}
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, 'w') as z:
        for name, content in files.items():
            z.writestr(prefix + name, content)
    return buf.getvalue()


class RegistryCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self._orig = registry.DRIVERS_DIR
        registry.DRIVERS_DIR = os.path.join(self.tmp, 'drivers')

    def tearDown(self):
        registry.DRIVERS_DIR = self._orig
        shutil.rmtree(self.tmp, ignore_errors=True)


class TestInstall(RegistryCase):
    def test_install_then_list_and_get(self):
        result = registry.install('stepper', make_bundle())
        self.assertEqual(result['version'], '1.0.0')
        self.assertFalse(result['replaced'])
        self.assertEqual(len(result['sha256']), 64)
        self.assertTrue(registry.has('stepper'))

        listed = registry.list_drivers()
        self.assertEqual([d['type'] for d in listed], ['stepper'])
        self.assertEqual(listed[0]['status'], 'ok')
        self.assertEqual(listed[0]['sha256'], result['sha256'])
        self.assertEqual(registry.get_driver('stepper')['manifest']['actions'].keys(),
                         {'ping'})
        # Nothing left behind in staging.
        self.assertEqual(os.listdir(os.path.join(registry.DRIVERS_DIR, '.staging')), [])

    def test_bundle_zipped_with_its_folder_is_accepted(self):
        registry.install('stepper', make_bundle(prefix='stepper/'))
        self.assertTrue(os.path.isfile(os.path.join(registry.driver_dir('stepper'), 'driver.py')))

    def test_replace_swaps_whole_directory(self):
        registry.install('stepper', make_bundle(files={
            'lager_driver.json': json.dumps(MANIFEST), 'driver.py': DRIVER, 'old.py': ''}))
        manifest2 = dict(MANIFEST, version='2.0.0')
        result = registry.install('stepper', make_bundle(manifest=manifest2))
        self.assertTrue(result['replaced'])
        self.assertEqual(registry.get_driver('stepper')['version'], '2.0.0')
        self.assertFalse(os.path.exists(os.path.join(registry.driver_dir('stepper'), 'old.py')))

    def test_failed_install_keeps_previous_version(self):
        registry.install('stepper', make_bundle())
        with self.assertRaises(registry.DriverStoreError):
            registry.install('stepper', make_bundle(files={'lager_driver.json': '{}'}))
        self.assertEqual(registry.get_driver('stepper')['version'], '1.0.0')

    def test_type_must_match_url(self):
        with self.assertRaisesRegex(registry.DriverStoreError, "declares type 'stepper'"):
            registry.install('other', make_bundle())
        self.assertFalse(registry.has('other'))

    def test_rejections(self):
        cases = {
            'not a zip': (b'hello', 400),
            'empty': (b'', 400),
            'no manifest': (make_bundle(files={'driver.py': DRIVER}), 400),
            'no driver': (make_bundle(files={'lager_driver.json': json.dumps(MANIFEST)}), 400),
            'bad manifest': (make_bundle(files={'lager_driver.json': '[]',
                                                'driver.py': DRIVER}), 400),
            'traversal': (make_bundle(files={'lager_driver.json': json.dumps(MANIFEST),
                                             'driver.py': DRIVER,
                                             '../../evil.py': ''}), 400),
            'absolute': (make_bundle(files={'lager_driver.json': json.dumps(MANIFEST),
                                            'driver.py': DRIVER, '/etc/x': ''}), 400),
        }
        for label, (data, status) in cases.items():
            with self.subTest(case=label):
                with self.assertRaises(registry.DriverStoreError) as cm:
                    registry.install('stepper', data)
                self.assertEqual(cm.exception.status, status)
        self.assertFalse(registry.has('stepper'))
        self.assertFalse(os.path.exists(os.path.join(self.tmp, 'evil.py')))

    def test_symlink_entry_rejected(self):
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, 'w') as z:
            z.writestr('lager_driver.json', json.dumps(MANIFEST))
            z.writestr('driver.py', DRIVER)
            link = zipfile.ZipInfo('link')
            link.external_attr = (stat.S_IFLNK | 0o777) << 16
            z.writestr(link, '/etc/passwd')
        with self.assertRaisesRegex(registry.DriverStoreError, 'symlink'):
            registry.install('stepper', buf.getvalue())

    def test_size_limit(self):
        orig = registry.MAX_BUNDLE_BYTES
        registry.MAX_BUNDLE_BYTES = 10
        try:
            with self.assertRaises(registry.DriverStoreError) as cm:
                registry.install('stepper', make_bundle())
            self.assertEqual(cm.exception.status, 413)
        finally:
            registry.MAX_BUNDLE_BYTES = orig

    def test_reserved_and_invalid_types(self):
        for dtype in ('uart', 'supply', 'Bad', '../x', ''):
            with self.subTest(dtype=dtype):
                with self.assertRaises(registry.DriverStoreError) as cm:
                    registry.install(dtype, make_bundle())
                self.assertEqual(cm.exception.status, 400)

    def test_paths_never_leave_the_drivers_dir(self):
        for dtype in ('..', '../x', '/etc'):
            with self.subTest(dtype=dtype):
                with self.assertRaises(registry.DriverStoreError):
                    registry.driver_dir(dtype)
                self.assertFalse(registry.has(dtype))


class TestListingAndRemove(RegistryCase):
    def test_broken_bundle_is_listed_as_error_and_isolated(self):
        registry.install('stepper', make_bundle())
        broken = registry.driver_dir('broken')
        os.makedirs(broken)
        with open(os.path.join(broken, 'lager_driver.json'), 'w') as f:
            f.write('{oops')
        listed = {d['type']: d for d in registry.list_drivers()}
        self.assertEqual(listed['stepper']['status'], 'ok')
        self.assertEqual(listed['broken']['status'], 'error')
        self.assertIn('not valid JSON', listed['broken']['error'])
        with self.assertRaisesRegex(DriverUnavailable, 'failed to load'):
            registry.get_driver('broken')

    def test_get_missing_driver(self):
        with self.assertRaisesRegex(DriverUnavailable, 'No custom driver'):
            registry.get_driver('stepper')
        self.assertEqual(registry.list_drivers(), [])

    def test_has_rejects_odd_names_without_touching_disk(self):
        for value in (None, 5, '../etc', 'uart/x'):
            self.assertFalse(registry.has(value))

    def test_remove_refuses_while_nets_use_it(self):
        registry.install('stepper', make_bundle())
        with self.assertRaises(registry.DriverStoreError) as cm:
            registry.remove('stepper', ['RIG_1'])
        self.assertEqual(cm.exception.status, 409)
        self.assertIn('RIG_1', cm.exception.message)
        self.assertTrue(registry.has('stepper'))

        result = registry.remove('stepper', ['RIG_1'], force=True)
        self.assertEqual(result['orphaned_nets'], ['RIG_1'])
        self.assertFalse(registry.has('stepper'))

    def test_remove_missing(self):
        with self.assertRaises(registry.DriverStoreError) as cm:
            registry.remove('stepper')
        self.assertEqual(cm.exception.status, 404)


if __name__ == '__main__':
    unittest.main()
