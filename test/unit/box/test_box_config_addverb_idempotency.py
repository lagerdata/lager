# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0
"""
Rerun-safety pinning for the box-side add verbs.

Users re-run provisioning scripts (run.sh) after partial failures, so
mount-add / apt-add / udev-add must upsert rather than duplicate-error.
The upsert behavior exists by design (mount keyed by container path, apt
by lowercased name, udev by vid:pid) — these tests pin it so a refactor
can't silently turn reruns into validation failures.

Drives box/lager/box_config/box_config_cli.py handlers directly with the
config redirected to a temp file. No box, no /etc/lager.
"""

import importlib.util
import json
import os
import sys
import tempfile
import types
import unittest

_PKG_DIR = os.path.abspath(os.path.join(
    os.path.dirname(__file__),
    '..', '..', '..', 'box', 'lager', 'box_config',
))


def _load_shim_package():
    """Load config.py + box_config_cli.py under a synthetic package so the
    shim's `from . import config as cfg` resolves without the real box
    package (which needs on-box deps)."""
    pkg = types.ModuleType("boxcfg_idem")
    pkg.__path__ = [_PKG_DIR]
    sys.modules["boxcfg_idem"] = pkg

    spec = importlib.util.spec_from_file_location(
        "boxcfg_idem.config", os.path.join(_PKG_DIR, "config.py"))
    cfgmod = importlib.util.module_from_spec(spec)
    sys.modules["boxcfg_idem.config"] = cfgmod
    spec.loader.exec_module(cfgmod)

    spec2 = importlib.util.spec_from_file_location(
        "boxcfg_idem.box_config_cli", os.path.join(_PKG_DIR, "box_config_cli.py"))
    shim = importlib.util.module_from_spec(spec2)
    sys.modules["boxcfg_idem.box_config_cli"] = shim
    spec2.loader.exec_module(shim)
    return cfgmod, shim


_cfgmod, _shim = _load_shim_package()


class _ShimCase(unittest.TestCase):
    """Redirect config persistence to a temp file; capture JSON responses."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.cfg_path = os.path.join(self.tmp.name, "box_config.json")

        self._orig_path = _cfgmod.BOX_CONFIG_PATH
        self._orig_save = _cfgmod.save
        self._orig_audit = _shim._audit
        self._orig_stdout = _shim._stdout_json
        _cfgmod.BOX_CONFIG_PATH = self.cfg_path
        _cfgmod.save = lambda c, path=None: self._orig_save(c, path or self.cfg_path)
        _shim._audit = lambda verb, args: None
        self.responses = []
        _shim._stdout_json = self.responses.append

        def _restore():
            _cfgmod.BOX_CONFIG_PATH = self._orig_path
            _cfgmod.save = self._orig_save
            _shim._audit = self._orig_audit
            _shim._stdout_json = self._orig_stdout
        self.addCleanup(_restore)

    def _saved(self):
        with open(self.cfg_path, encoding="utf-8") as f:
            return json.load(f)

    def _last(self):
        return self.responses[-1]


class MountAddIdempotency(_ShimCase):
    def test_identical_mount_added_twice_is_single_entry(self):
        payload = json.dumps({"host": "/Hyphen", "container": "/Hyphen", "readonly": True})
        _shim._cmd_mount_add(payload)
        _shim._cmd_mount_add(payload)
        self.assertTrue(self._last()["ok"], msg=self._last())
        mounts = self._saved()["mounts"]
        self.assertEqual(len(mounts), 1)
        self.assertEqual(mounts[0]["host"], "/Hyphen")

    def test_same_container_new_host_replaces(self):
        # Pin R5: upsert keyed by container path replaces the host side.
        _shim._cmd_mount_add(json.dumps({"host": "/old", "container": "/dest"}))
        _shim._cmd_mount_add(json.dumps({"host": "/new", "container": "/dest"}))
        self.assertTrue(self._last()["ok"])
        mounts = self._saved()["mounts"]
        self.assertEqual(len(mounts), 1)
        self.assertEqual(mounts[0]["host"], "/new")

    def test_readonly_flip_on_rerun_takes_effect(self):
        _shim._cmd_mount_add(json.dumps({"host": "/d", "container": "/d", "readonly": False}))
        _shim._cmd_mount_add(json.dumps({"host": "/d", "container": "/d", "readonly": True}))
        mounts = self._saved()["mounts"]
        self.assertEqual(len(mounts), 1)
        self.assertTrue(mounts[0]["readonly"])


class AptAddIdempotency(_ShimCase):
    def test_duplicate_package_single_entry(self):
        payload = json.dumps({"packages": ["dfu-util"]})
        _shim._cmd_apt_add(payload)
        _shim._cmd_apt_add(payload)
        self.assertTrue(self._last()["ok"], msg=self._last())
        self.assertEqual(self._saved()["apt_packages"], ["dfu-util"])

    def test_case_variant_upserts_not_duplicates(self):
        _shim._cmd_apt_add(json.dumps({"packages": ["dfu-util"]}))
        _shim._cmd_apt_add(json.dumps({"packages": ["DFU-Util"]}))
        pkgs = self._saved()["apt_packages"]
        self.assertEqual(len(pkgs), 1)


class UdevAddIdempotency(_ShimCase):
    def test_same_vid_pid_twice_single_rule(self):
        payload = json.dumps({"rules": [{"vid": "1209", "pid": "0001"}]})
        _shim._cmd_udev_add(payload)
        _shim._cmd_udev_add(payload)
        self.assertTrue(self._last()["ok"], msg=self._last())
        self.assertEqual(len(self._saved()["udev_rules"]), 1)

    def test_usbtmc_flip_upserts(self):
        _shim._cmd_udev_add(json.dumps({"rules": [{"vid": "1209", "pid": "0001"}]}))
        _shim._cmd_udev_add(json.dumps({"rules": [{"vid": "1209", "pid": "0001", "usbtmc": True}]}))
        rules = self._saved()["udev_rules"]
        self.assertEqual(len(rules), 1)
        self.assertTrue(rules[0]["usbtmc"])


if __name__ == "__main__":
    unittest.main()
