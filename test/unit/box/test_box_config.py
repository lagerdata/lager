# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0
"""
Unit tests for box/lager/box_config/config.py.

Pure stdlib — no hardware deps to stub. Covers every validation rule
enumerated in the box_config v1 schema, plus the idempotency hash.
"""

import importlib.util
import json
import os
import sys
import tempfile
import unittest

_CONFIG_PY = os.path.abspath(
    os.path.join(
        os.path.dirname(__file__),
        '..', '..', '..', 'box', 'lager', 'box_config', 'config.py',
    )
)


def _load_config_module():
    name = "box_config_under_test"
    spec = importlib.util.spec_from_file_location(name, _CONFIG_PY)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


cfg = _load_config_module()


def _v(extra=None):
    base = {"version": 1}
    if extra:
        base.update(extra)
    return base


class ValidateStructure(unittest.TestCase):
    def test_top_level_must_be_object(self):
        self.assertIn("Top-level", cfg.validate([])[0])
        self.assertIn("Top-level", cfg.validate("foo")[0])

    def test_missing_version(self):
        errors = cfg.validate({})
        self.assertTrue(any("version" in e for e in errors))

    def test_unsupported_version(self):
        errors = cfg.validate({"version": 999})
        self.assertTrue(any("Unsupported config version" in e for e in errors))

    def test_minimal_valid(self):
        self.assertEqual(cfg.validate(_v()), [])


class ValidateMounts(unittest.TestCase):
    def test_mounts_must_be_array(self):
        errors = cfg.validate(_v({"mounts": "nope"}))
        self.assertTrue(any("'mounts' must be an array" in e for e in errors))

    def test_missing_required_keys(self):
        errors = cfg.validate(_v({"mounts": [{}]}))
        self.assertTrue(any("'host'" in e for e in errors))
        self.assertTrue(any("'container'" in e for e in errors))

    def test_host_absolute(self):
        errors = cfg.validate(_v({"mounts": [{"host": "rel", "container": "/c"}]}))
        self.assertTrue(any("must be an absolute path" in e for e in errors))

    def test_container_absolute(self):
        errors = cfg.validate(_v({"mounts": [{"host": "/h", "container": "rel"}]}))
        self.assertTrue(any("container must be an absolute path" in e for e in errors))

    def test_host_root_rejected(self):
        errors = cfg.validate(_v({"mounts": [{"host": "/", "container": "/c"}]}))
        self.assertTrue(any("cannot be '/'" in e and "host" in e for e in errors))

    def test_container_root_rejected(self):
        errors = cfg.validate(_v({"mounts": [{"host": "/h", "container": "/"}]}))
        self.assertTrue(any("cannot be '/'" in e and "container" in e for e in errors))

    def test_duplicate_container_paths(self):
        errors = cfg.validate(_v({"mounts": [
            {"host": "/a", "container": "/x"},
            {"host": "/b", "container": "/x"},
        ]}))
        self.assertTrue(any("duplicates mounts[0]" in e for e in errors))

    def test_readonly_must_be_bool(self):
        errors = cfg.validate(_v({"mounts": [
            {"host": "/h", "container": "/c", "readonly": "yes"}
        ]}))
        self.assertTrue(any("readonly must be a boolean" in e for e in errors))

    def test_valid_mount(self):
        self.assertEqual(
            cfg.validate(_v({"mounts": [{"host": "/Hyphen", "container": "/Hyphen"}]})),
            [],
        )


class ValidateVolumes(unittest.TestCase):
    def test_volumes_must_be_array(self):
        errors = cfg.validate(_v({"volumes": "nope"}))
        self.assertTrue(any("'volumes' must be an array" in e for e in errors))

    def test_missing_keys(self):
        errors = cfg.validate(_v({"volumes": [{}]}))
        self.assertTrue(any("'name'" in e for e in errors))
        self.assertTrue(any("'container'" in e for e in errors))

    def test_invalid_volume_name(self):
        errors = cfg.validate(_v({"volumes": [{"name": "@bad", "container": "/c"}]}))
        self.assertTrue(any("not a valid Docker volume name" in e for e in errors))

    def test_duplicate_volume_names(self):
        errors = cfg.validate(_v({"volumes": [
            {"name": "vol1", "container": "/a"},
            {"name": "vol1", "container": "/b"},
        ]}))
        self.assertTrue(any("duplicates volumes[0]" in e for e in errors))

    def test_container_must_be_absolute(self):
        errors = cfg.validate(_v({"volumes": [{"name": "v", "container": "rel"}]}))
        self.assertTrue(any("must be an absolute path" in e for e in errors))

    def test_container_cannot_be_root(self):
        errors = cfg.validate(_v({"volumes": [{"name": "v", "container": "/"}]}))
        self.assertTrue(any("cannot be '/'" in e for e in errors))

    def test_valid_volume(self):
        self.assertEqual(
            cfg.validate(_v({"volumes": [{"name": "box-tools", "container": "/opt/box-tools"}]})),
            [],
        )


class ValidateCrossCollisions(unittest.TestCase):
    def test_mount_volume_container_collision(self):
        errors = cfg.validate(_v({
            "mounts": [{"host": "/h", "container": "/shared"}],
            "volumes": [{"name": "v", "container": "/shared"}],
        }))
        self.assertTrue(any("collides with mounts" in e for e in errors))


class ValidateEnv(unittest.TestCase):
    def test_env_must_be_object(self):
        errors = cfg.validate(_v({"env": []}))
        self.assertTrue(any("'env' must be an object" in e for e in errors))

    def test_env_value_must_be_string(self):
        errors = cfg.validate(_v({"env": {"X": 1}}))
        self.assertTrue(any("must be a string" in e for e in errors))

    def test_env_path_rejected(self):
        errors = cfg.validate(_v({"env": {"PATH": "/bin"}}))
        self.assertTrue(any("PATH_PREPEND" in e for e in errors))

    def test_env_invalid_key(self):
        errors = cfg.validate(_v({"env": {"1bad": "x"}}))
        self.assertTrue(any("not a valid environment variable name" in e for e in errors))

    def test_env_path_prepend_allowed(self):
        self.assertEqual(cfg.validate(_v({"env": {"PATH_PREPEND": "/opt/box-tools/.cargo/bin"}})), [])


class FromDict(unittest.TestCase):
    def test_round_trip_extras(self):
        raw = _v({
            "mounts": [{"host": "/a", "container": "/a"}],
            "apt_packages": ["binutils-arm-none-eabi"],
            "rustup": {"default_toolchain": "stable"},
        })
        c = cfg.BoxConfig.from_dict(raw)
        out = c.to_dict()
        self.assertEqual(out["apt_packages"], ["binutils-arm-none-eabi"])
        self.assertEqual(out["rustup"], {"default_toolchain": "stable"})

    def test_validation_error_raises(self):
        with self.assertRaises(cfg.ValidationError):
            cfg.BoxConfig.from_dict({"version": 999})


class HashIdempotency(unittest.TestCase):
    def test_same_inputs_same_hash(self):
        a = cfg.BoxConfig.from_dict(_v({"mounts": [{"host": "/a", "container": "/a"}]}))
        b = cfg.BoxConfig.from_dict(_v({"mounts": [{"host": "/a", "container": "/a"}]}))
        self.assertEqual(a.compute_hash(), b.compute_hash())

    def test_key_order_irrelevant(self):
        raw1 = {"version": 1, "mounts": [], "env": {"A": "1", "B": "2"}}
        raw2 = {"env": {"B": "2", "A": "1"}, "mounts": [], "version": 1}
        h1 = cfg.BoxConfig.from_dict(raw1).compute_hash()
        h2 = cfg.BoxConfig.from_dict(raw2).compute_hash()
        self.assertEqual(h1, h2)

    def test_different_extras_change_hash(self):
        a = cfg.BoxConfig.from_dict(_v({"apt_packages": ["a"]}))
        b = cfg.BoxConfig.from_dict(_v({"apt_packages": ["a", "b"]}))
        self.assertNotEqual(a.compute_hash(), b.compute_hash())


class DockerArgs(unittest.TestCase):
    def test_mount_arg(self):
        m = cfg.Mount(host="/a", container="/b")
        self.assertEqual(m.to_docker_arg(), "-v /a:/b")

    def test_mount_arg_readonly(self):
        m = cfg.Mount(host="/a", container="/b", readonly=True)
        self.assertEqual(m.to_docker_arg(), "-v /a:/b:ro")

    def test_volume_arg(self):
        v = cfg.Volume(name="box-tools", container="/opt/box-tools")
        self.assertEqual(v.to_docker_arg(), "-v box-tools:/opt/box-tools")

    def test_quotes_paths_with_spaces(self):
        m = cfg.Mount(host="/path with space", container="/c")
        self.assertIn("'/path with space:/c'", m.to_docker_arg())

    def test_env_args(self):
        c = cfg.BoxConfig(env={"DEFMT_LOG": "info"})
        self.assertEqual(c.docker_env_args(), ["--env DEFMT_LOG=info"])


class LoadSave(unittest.TestCase):
    def test_load_missing_returns_none(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertIsNone(cfg.load(os.path.join(d, "missing.json")))

    def test_save_load_roundtrip(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "box_config.json")
            original = cfg.init_default()
            cfg.save(original, path)
            loaded = cfg.load(path)
            self.assertEqual(loaded.compute_hash(), original.compute_hash())


class InitDefault(unittest.TestCase):
    def test_box_tools_volume_present(self):
        c = cfg.init_default()
        self.assertEqual(len(c.volumes), 1)
        self.assertEqual(c.volumes[0].name, "box-tools")
        self.assertEqual(c.volumes[0].container, "/opt/box-tools")

    def test_default_validates(self):
        self.assertEqual(cfg.validate(cfg.init_default().to_dict()), [])


if __name__ == "__main__":
    unittest.main()
