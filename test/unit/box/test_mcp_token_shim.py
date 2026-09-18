# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0
"""
The box-side `mcp-token-*` verbs: a secret that box-config must not treat
like the rest of its settings.

`box_config.json` is the wrong home for a secret -- `show` prints it, `export`
and `copy` carry it, and `env-set` writes its whole payload to the audit log.
So the MCP bearer token is a file of its own, and these tests pin the three
things that keep it one: the verbs never touch box_config.json, the audit
record names the verb and never the value, and no verb can read the value
back once `enable` or `rotate` has returned it.

The REAL `_audit` runs here, pointed at a temp file. Stubbing it out -- as the
other shim tests do, because they do not care what it writes -- would make
"the token is not in the audit log" pass without checking anything.

Drives box/lager/box_config/box_config_cli.py handlers directly. No box, no
/etc/lager.
"""

import importlib.util
import inspect
import json
import os
import re
import stat
import sys
import tempfile
import types
import unittest

_REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..', '..'))
_PKG_DIR = os.path.join(_REPO, 'box', 'lager', 'box_config')
_CONSTANTS = os.path.join(_REPO, 'box', 'lager', 'constants.py')

# Its own synthetic package name: sharing one with another test module would
# share the module objects this file patches.
_PKG = "boxcfg_mcptoken"


def _load_shim_package():
    """config.py + box_config_cli.py under a synthetic package, so the shim's
    relative imports resolve without the real box package (which needs on-box
    dependencies). `mcp_token` is found through the package's __path__."""
    pkg = types.ModuleType(_PKG)
    pkg.__path__ = [_PKG_DIR]
    sys.modules[_PKG] = pkg
    loaded = []
    for name in ("config", "box_config_cli"):
        spec = importlib.util.spec_from_file_location(
            f"{_PKG}.{name}", os.path.join(_PKG_DIR, f"{name}.py"))
        mod = importlib.util.module_from_spec(spec)
        sys.modules[f"{_PKG}.{name}"] = mod
        spec.loader.exec_module(mod)
        loaded.append(mod)
    return loaded


_cfgmod, _shim = _load_shim_package()
_token_mod = sys.modules[f"{_PKG}.mcp_token"]

# secrets.token_urlsafe(32): 43 characters from the URL-safe base64 alphabet.
_TOKEN_SHAPE = re.compile(r"^[A-Za-z0-9_-]{43}$")


class _TokenCase(unittest.TestCase):
    """Every path the verbs can touch is redirected into one temp dir."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = self.tmp.name
        self.token_path = os.path.join(self.dir, "mcp_token")
        self.cfg_path = os.path.join(self.dir, "box_config.json")
        self.audit_path = os.path.join(self.dir, "box_config.audit.log")

        saved = (_shim._mcp_token_path, _shim._stdout_json,
                 _shim._BOX_CONFIG_AUDIT_PATH, _cfgmod.BOX_CONFIG_PATH)
        _shim._mcp_token_path = lambda: self.token_path
        self.responses = []
        _shim._stdout_json = self.responses.append
        _shim._BOX_CONFIG_AUDIT_PATH = self.audit_path
        _cfgmod.BOX_CONFIG_PATH = self.cfg_path

        def _restore():
            (_shim._mcp_token_path, _shim._stdout_json,
             _shim._BOX_CONFIG_AUDIT_PATH, _cfgmod.BOX_CONFIG_PATH) = saved
        self.addCleanup(_restore)

    def _do(self, verb):
        _shim._dispatch([verb])
        return self.responses[-1]

    def _file(self):
        with open(self.token_path, encoding="ascii") as f:
            return f.read()

    def _mode(self, path=None):
        return stat.S_IMODE(os.stat(path or self.token_path).st_mode)

    def _audit_text(self):
        try:
            with open(self.audit_path, encoding="utf-8") as f:
                return f.read()
        except FileNotFoundError:
            return ""

    def _audit_records(self):
        return [json.loads(line) for line in self._audit_text().splitlines()]


class Enable(_TokenCase):
    def test_a_fresh_box_is_disabled(self):
        self.assertEqual(self._do("mcp-token-status"), {"ok": True, "state": "disabled"})

    def test_enable_creates_a_private_file_and_returns_its_value_once(self):
        reply = self._do("mcp-token-enable")
        self.assertTrue(reply["ok"], msg=reply)
        self.assertRegex(reply["token"], _TOKEN_SHAPE)
        self.assertEqual(reply["previous"], "disabled")
        self.assertEqual(self._file(), reply["token"] + "\n")
        self.assertEqual(self._mode(), 0o600)
        self.assertEqual(self._do("mcp-token-status")["state"], "enabled")

    def test_the_mode_does_not_depend_on_the_umask(self):
        # A umask that strips every bit leaves open()'s mode argument at 000.
        old = os.umask(0o777)
        try:
            reply = self._do("mcp-token-enable")
        finally:
            os.umask(old)
        self.assertTrue(reply["ok"], msg=reply)
        self.assertEqual(self._mode(), 0o600)

    def test_a_second_enable_refuses_and_leaves_the_first_token_alone(self):
        first = self._do("mcp-token-enable")["token"]
        reply = self._do("mcp-token-enable")
        self.assertFalse(reply["ok"])
        self.assertEqual(reply["code"], "already-enabled")
        self.assertIn("rotate", " ".join(reply["errors"]))
        self.assertNotIn("token", reply)
        self.assertEqual(self._file(), first + "\n")

    def test_two_enables_never_mint_the_same_value(self):
        first = self._do("mcp-token-enable")["token"]
        self._do("mcp-token-disable")
        self.assertNotEqual(self._do("mcp-token-enable")["token"], first)

    def test_a_directory_that_cannot_be_written_is_reported_not_raised(self):
        self.token_path = os.path.join(self.dir, "no-such-dir", "mcp_token")
        reply = self._do("mcp-token-enable")
        self.assertFalse(reply["ok"])
        self.assertEqual(reply["code"], "write-failed")
        self.assertNotIn("token", reply)
        self.assertEqual(self._audit_text(), "")


class Rotate(_TokenCase):
    def test_rotate_refuses_on_a_box_with_no_token_and_creates_nothing(self):
        reply = self._do("mcp-token-rotate")
        self.assertFalse(reply["ok"])
        self.assertEqual(reply["code"], "not-enabled")
        self.assertIn("enable", " ".join(reply["errors"]))
        self.assertFalse(os.path.exists(self.token_path))
        self.assertEqual(os.listdir(self.dir), [])

    def test_rotate_replaces_the_value_and_leaves_no_temp_file(self):
        first = self._do("mcp-token-enable")["token"]
        reply = self._do("mcp-token-rotate")
        self.assertTrue(reply["ok"], msg=reply)
        self.assertRegex(reply["token"], _TOKEN_SHAPE)
        self.assertNotEqual(reply["token"], first)
        self.assertEqual(self._file(), reply["token"] + "\n")
        self.assertEqual(self._mode(), 0o600)
        leftovers = [n for n in os.listdir(self.dir) if n.endswith(".tmp")]
        self.assertEqual(leftovers, [])

    def test_rotate_is_a_new_file_so_a_reader_keyed_on_the_inode_sees_it(self):
        self._do("mcp-token-enable")
        before = os.stat(self.token_path).st_ino
        self._do("mcp-token-rotate")
        self.assertNotEqual(os.stat(self.token_path).st_ino, before)

    def test_rotate_repairs_an_empty_file(self):
        open(self.token_path, "w").close()
        self.assertEqual(self._do("mcp-token-status")["state"], "empty")
        reply = self._do("mcp-token-rotate")
        self.assertTrue(reply["ok"], msg=reply)
        self.assertEqual(reply["previous"], "empty")
        self.assertEqual(self._do("mcp-token-status")["state"], "enabled")

    @unittest.skipIf(os.geteuid() == 0, "root reads a mode-000 file")
    def test_rotate_repairs_a_file_this_user_cannot_read(self):
        self._do("mcp-token-enable")
        os.chmod(self.token_path, 0o000)
        self.assertEqual(self._do("mcp-token-status")["state"], "unreadable")
        reply = self._do("mcp-token-rotate")
        self.assertTrue(reply["ok"], msg=reply)
        self.assertEqual(self._mode(), 0o600)
        self.assertEqual(self._do("mcp-token-status")["state"], "enabled")


class Disable(_TokenCase):
    def test_disable_removes_the_file(self):
        self._do("mcp-token-enable")
        reply = self._do("mcp-token-disable")
        self.assertEqual(reply, {"ok": True, "state": "disabled", "previous": "enabled"})
        self.assertFalse(os.path.exists(self.token_path))

    def test_disable_twice_is_not_an_error_and_is_audited_once(self):
        self._do("mcp-token-enable")
        self._do("mcp-token-disable")
        reply = self._do("mcp-token-disable")
        self.assertEqual(reply, {"ok": True, "state": "disabled", "previous": "disabled"})
        verbs = [r["verb"] for r in self._audit_records()]
        self.assertEqual(verbs.count("mcp-token-disable"), 1)


class TheValueGoesNowhereElse(_TokenCase):
    def test_status_has_no_value_in_any_state(self):
        states = {}
        states["disabled"] = self._do("mcp-token-status")
        self._do("mcp-token-enable")
        states["enabled"] = self._do("mcp-token-status")
        with open(self.token_path, "w"):
            pass
        states["empty"] = self._do("mcp-token-status")
        os.unlink(self.token_path)
        os.mkdir(self.token_path)
        states["unreadable"] = self._do("mcp-token-status")
        for expected, reply in states.items():
            self.assertEqual(reply, {"ok": True, "state": expected})

    def test_the_audit_log_names_the_verbs_and_never_a_value(self):
        first = self._do("mcp-token-enable")["token"]
        second = self._do("mcp-token-rotate")["token"]
        self._do("mcp-token-disable")
        records = self._audit_records()
        self.assertEqual(
            [r["verb"] for r in records],
            ["mcp-token-enable", "mcp-token-rotate", "mcp-token-disable"],
        )
        for record in records:
            self.assertEqual(record["args"], {})
        text = self._audit_text()
        self.assertNotIn(first, text)
        self.assertNotIn(second, text)

    def test_a_refusal_is_not_audited(self):
        self._do("mcp-token-rotate")
        self._do("mcp-token-enable")
        self._do("mcp-token-enable")
        self.assertEqual([r["verb"] for r in self._audit_records()], ["mcp-token-enable"])

    def test_box_config_json_is_never_created_or_read(self):
        for verb in ("mcp-token-status", "mcp-token-enable", "mcp-token-rotate",
                     "mcp-token-status", "mcp-token-disable"):
            self._do(verb)
        self.assertFalse(os.path.exists(self.cfg_path))

    def test_no_verb_exists_that_could_read_the_value_back(self):
        verbs = sorted(v for v in _shim._DISPATCH if v.startswith("mcp-token-"))
        self.assertEqual(verbs, ["mcp-token-disable", "mcp-token-enable",
                                 "mcp-token-rotate", "mcp-token-status"])
        self.assertEqual(inspect.signature(_token_mod.state).return_annotation, "str")


class ThePathHasOneBoxSideHome(unittest.TestCase):
    def _constants(self):
        spec = importlib.util.spec_from_file_location("_lager_constants_for_test", _CONSTANTS)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod

    def test_the_constant_is_the_documented_path_and_is_exported(self):
        constants = self._constants()
        self.assertEqual(constants.MCP_TOKEN_PATH, "/etc/lager/mcp_token")
        self.assertIn("MCP_TOKEN_PATH", constants.__all__)
        self.assertTrue(constants.MCP_TOKEN_PATH.startswith(constants.LAGER_CONFIG_DIR + "/"))

    def test_the_shim_takes_the_path_from_the_constant_not_a_second_literal(self):
        # The test class above replaces _mcp_token_path, so the real one is
        # read as source. A second "/etc/lager/mcp_token" literal in the shim
        # package is what this exists to stop.
        source = inspect.getsource(sys.modules[f"{_PKG}.box_config_cli"])
        self.assertIn("from lager.constants import MCP_TOKEN_PATH", source)
        for name in ("box_config_cli", "mcp_token"):
            text = inspect.getsource(sys.modules[f"{_PKG}.{name}"])
            self.assertNotIn("/etc/lager/mcp_token", text, name)


if __name__ == "__main__":
    unittest.main()
