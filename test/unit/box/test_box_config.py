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


class ValidateReservedPaths(unittest.TestCase):
    def test_each_reserved_path_rejected(self):
        # Spot-check every entry — keeps the test honest if someone adds a
        # path to the constant without updating start_box.sh.
        for reserved in cfg.RESERVED_CONTAINER_PATHS:
            errors = cfg.validate(_v({
                "mounts": [{"host": "/h", "container": reserved}],
            }))
            self.assertTrue(
                any("reserved by start_box.sh" in e and reserved in e for e in errors),
                f"expected rejection for reserved path {reserved!r}, got {errors}",
            )

    def test_ssh_collision_suggests_alternative(self):
        errors = cfg.validate(_v({
            "mounts": [{"host": "/h", "container": "/home/www-data/.ssh"}],
        }))
        self.assertTrue(
            any("/home/www-data/.ssh-git" in e for e in errors),
            f"expected suggestion for /home/www-data/.ssh, got {errors}",
        )

    def test_non_reserved_container_path_allowed(self):
        self.assertEqual(
            cfg.validate(_v({"mounts": [{"host": "/h", "container": "/Hyphen"}]})),
            [],
        )

    def test_suggest_alternative_only_for_known_paths(self):
        self.assertEqual(cfg.suggest_alternative("/home/www-data/.ssh"), "/home/www-data/.ssh-git")
        self.assertIsNone(cfg.suggest_alternative("/Hyphen"))


class MigrateRaw(unittest.TestCase):
    """Schema-migration scaffolding. _MIGRATIONS is empty in v1, so most
    branches are no-ops — these tests pin the contract so a future v2 can
    plug in without breaking the existing call sites."""

    def test_current_version_is_passthrough(self):
        raw = {"version": cfg.SCHEMA_VERSION, "mounts": [{"host": "/a", "container": "/a"}]}
        self.assertEqual(cfg.migrate_raw(raw), raw)

    def test_newer_version_raises(self):
        with self.assertRaises(cfg.ValidationError) as ctx:
            cfg.migrate_raw({"version": cfg.SCHEMA_VERSION + 1})
        self.assertIn("newer than this CLI supports", str(ctx.exception))

    def test_older_version_with_no_migrator_passes_through(self):
        # _MIGRATIONS is empty today; older versions fall through and let
        # validate() report the mismatch with a useful error string.
        raw = {"version": 0}
        self.assertEqual(cfg.migrate_raw(raw), raw)

    def test_non_dict_input_is_passthrough(self):
        # Malformed input shouldn't crash migrate_raw — validate handles it.
        self.assertEqual(cfg.migrate_raw("not a dict"), "not a dict")
        self.assertEqual(cfg.migrate_raw(None), None)

    def test_migration_chain_runs_to_current(self):
        # Inject a fake v0 -> v1 migrator to exercise the while loop. Restore
        # the empty dict after so we don't pollute other tests in this run.
        cfg._MIGRATIONS[0] = lambda r: {**r, "version": 1, "migrated": True}
        try:
            out = cfg.migrate_raw({"version": 0})
            self.assertEqual(out["version"], 1)
            self.assertTrue(out["migrated"])
        finally:
            cfg._MIGRATIONS.clear()


class AppliedSnapshot(unittest.TestCase):
    def test_read_snapshot_missing_returns_none(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertIsNone(cfg.read_applied_snapshot(os.path.join(d, "missing.json")))

    def test_round_trip(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "snap.json")
            original = cfg.init_default()
            cfg.write_applied_snapshot(original, path)
            loaded = cfg.read_applied_snapshot(path)
            self.assertEqual(loaded.compute_hash(), original.compute_hash())

    def test_corrupt_snapshot_returns_none(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "bad.json")
            with open(path, "w") as f:
                f.write("not valid json")
            self.assertIsNone(cfg.read_applied_snapshot(path))


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

    def test_validate_env_key_helper(self):
        # Exported for the shim's env-set handler to reuse the same rule.
        ok, _ = cfg.validate_env_key("FOO")
        self.assertTrue(ok)
        ok, _ = cfg.validate_env_key("PATH_PREPEND")
        self.assertTrue(ok)
        ok, reason = cfg.validate_env_key("1bad")
        self.assertFalse(ok)
        self.assertIn("invalid env variable name", reason)
        ok, reason = cfg.validate_env_key("")
        self.assertFalse(ok)
        self.assertIn("empty", reason)
        ok, reason = cfg.validate_env_key("PATH")
        self.assertFalse(ok)
        self.assertIn("PATH_PREPEND", reason)


class FromDict(unittest.TestCase):
    def test_round_trip_extras(self):
        raw = _v({
            "mounts": [{"host": "/a", "container": "/a"}],
            "rustup": {"default_toolchain": "stable"},
            "hooks": {"post_apply": "echo done"},
        })
        c = cfg.BoxConfig.from_dict(raw)
        out = c.to_dict()
        self.assertEqual(out["rustup"], {"default_toolchain": "stable"})
        self.assertEqual(out["hooks"], {"post_apply": "echo done"})

    def test_validation_error_raises(self):
        with self.assertRaises(cfg.ValidationError):
            cfg.BoxConfig.from_dict({"version": 999})

    def test_pip_packages_not_in_extras(self):
        raw = _v({"pip_packages": ["numpy"]})
        c = cfg.BoxConfig.from_dict(raw)
        self.assertEqual(c.pip_packages, ["numpy"])
        self.assertNotIn("pip_packages", c.extras)
        self.assertEqual(c.to_dict()["pip_packages"], ["numpy"])

    def test_first_class_fields_not_in_extras(self):
        raw = _v({
            "apt_packages": ["tcpdump"],
            "sysctl": {"net.ipv4.ip_forward": "1"},
            "cargo_packages": ["defmt-print"],
        })
        c = cfg.BoxConfig.from_dict(raw)
        self.assertEqual(c.apt_packages, ["tcpdump"])
        self.assertEqual(c.sysctl, {"net.ipv4.ip_forward": "1"})
        self.assertEqual(c.cargo_packages, ["defmt-print"])
        for k in ("apt_packages", "sysctl", "cargo_packages"):
            self.assertNotIn(k, c.extras)


class ValidatePipPackages(unittest.TestCase):
    def test_missing_key_is_valid(self):
        self.assertEqual(cfg.validate(_v()), [])

    def test_empty_list_is_valid(self):
        self.assertEqual(cfg.validate(_v({"pip_packages": []})), [])

    def test_must_be_list(self):
        errors = cfg.validate(_v({"pip_packages": "numpy"}))
        self.assertTrue(any("'pip_packages' must be an array" in e for e in errors))

    def test_non_string_element(self):
        errors = cfg.validate(_v({"pip_packages": [42]}))
        self.assertTrue(any("must be a string" in e for e in errors))

    def test_empty_string(self):
        errors = cfg.validate(_v({"pip_packages": [""]}))
        self.assertTrue(any("cannot be empty" in e for e in errors))

    def test_whitespace_only(self):
        errors = cfg.validate(_v({"pip_packages": ["   "]}))
        self.assertTrue(any("cannot be empty" in e for e in errors))

    def test_accepts_bare_name(self):
        self.assertEqual(cfg.validate(_v({"pip_packages": ["numpy"]})), [])

    def test_accepts_version_specifier(self):
        self.assertEqual(cfg.validate(_v({"pip_packages": ["numpy==1.26.4"]})), [])

    def test_accepts_complex_version(self):
        self.assertEqual(cfg.validate(_v({"pip_packages": ["numpy>=1.20,<2.0"]})), [])

    def test_accepts_extras(self):
        self.assertEqual(cfg.validate(_v({"pip_packages": ["pandas[excel]"]})), [])

    def test_accepts_extras_with_version(self):
        self.assertEqual(cfg.validate(_v({"pip_packages": ["pandas[excel,plot]==2.0"]})), [])

    def test_rejects_pip_flag(self):
        errors = cfg.validate(_v({"pip_packages": ["--index-url", "https://x"]}))
        self.assertTrue(any("--index-url" in e for e in errors))

    def test_rejects_editable(self):
        errors = cfg.validate(_v({"pip_packages": ["-e ."]}))
        self.assertTrue(any("must start with a letter" in e for e in errors))

    def test_rejects_leading_digit(self):
        errors = cfg.validate(_v({"pip_packages": ["1package"]}))
        self.assertTrue(any("must start with a letter" in e for e in errors))

    def test_rejects_canonical_duplicate_case(self):
        errors = cfg.validate(_v({"pip_packages": ["numpy", "Numpy"]}))
        self.assertTrue(any("duplicates" in e and "numpy" in e for e in errors))

    def test_rejects_canonical_duplicate_versions(self):
        errors = cfg.validate(_v({"pip_packages": ["numpy==1.20", "Numpy>=1.0"]}))
        self.assertTrue(any("duplicates" in e for e in errors))

    def test_rejects_canonical_duplicate_underscore(self):
        # underscore <-> dash normalization
        errors = cfg.validate(_v({"pip_packages": ["scikit-learn", "scikit_learn"]}))
        self.assertTrue(any("duplicates" in e for e in errors))

    def test_normalize_pip_name_strips_version(self):
        self.assertEqual(cfg.normalize_pip_name("Numpy==1.26.4"), "numpy")
        self.assertEqual(cfg.normalize_pip_name("scikit_learn"), "scikit-learn")
        self.assertEqual(cfg.normalize_pip_name("pandas[excel]==2.0"), "pandas")

    def test_validate_pip_format_helper(self):
        ok, _ = cfg.validate_pip_format("numpy")
        self.assertTrue(ok)
        ok, reason = cfg.validate_pip_format("--bad")
        self.assertFalse(ok)
        self.assertIn("must start with a letter", reason)


class PipHashIdempotency(unittest.TestCase):
    def test_pip_packages_change_hash(self):
        a = cfg.BoxConfig.from_dict(_v({"pip_packages": ["numpy"]}))
        b = cfg.BoxConfig.from_dict(_v({"pip_packages": ["numpy", "scipy"]}))
        self.assertNotEqual(a.compute_hash(), b.compute_hash())

    def test_pip_order_matters_in_hash(self):
        # Order is preserved as authored; this is intentional so users can keep
        # a comment-style order. The renderer sorts before writing.
        a = cfg.BoxConfig.from_dict(_v({"pip_packages": ["numpy", "scipy"]}))
        b = cfg.BoxConfig.from_dict(_v({"pip_packages": ["scipy", "numpy"]}))
        self.assertNotEqual(a.compute_hash(), b.compute_hash())


class ValidateAptPackages(unittest.TestCase):
    def test_missing_key_is_valid(self):
        self.assertEqual(cfg.validate(_v()), [])

    def test_empty_list_is_valid(self):
        self.assertEqual(cfg.validate(_v({"apt_packages": []})), [])

    def test_must_be_list(self):
        errors = cfg.validate(_v({"apt_packages": "tcpdump"}))
        self.assertTrue(any("'apt_packages' must be an array" in e for e in errors))

    def test_non_string_element(self):
        errors = cfg.validate(_v({"apt_packages": [42]}))
        self.assertTrue(any("must be a string" in e for e in errors))

    def test_accepts_simple_name(self):
        self.assertEqual(cfg.validate(_v({"apt_packages": ["tcpdump"]})), [])

    def test_accepts_dashes_dots_pluses(self):
        self.assertEqual(
            cfg.validate(_v({"apt_packages": [
                "iptables-persistent", "g++", "libstdc++6", "linux-headers-5.10.0",
            ]})),
            [],
        )

    def test_rejects_uppercase(self):
        errors = cfg.validate(_v({"apt_packages": ["Tcpdump"]}))
        self.assertTrue(any("invalid Debian package name" in e for e in errors))

    def test_rejects_shell_metacharacters(self):
        for bad in ["foo;rm", "foo bar", "$evil", "foo`x`"]:
            errors = cfg.validate(_v({"apt_packages": [bad]}))
            self.assertTrue(
                any("invalid Debian package name" in e for e in errors),
                msg=f"expected rejection for {bad!r}, got {errors}",
            )

    def test_rejects_version_pinning_in_name(self):
        # apt's pkg=version syntax is not allowed in the name field (v1).
        errors = cfg.validate(_v({"apt_packages": ["tcpdump=4.99"]}))
        self.assertTrue(any("invalid Debian package name" in e for e in errors))

    def test_rejects_canonical_duplicate(self):
        errors = cfg.validate(_v({"apt_packages": ["tcpdump", "TCPDUMP"]}))
        # Normalization is just lowercase for apt; uppercase TCPDUMP fails
        # the validate_apt_format check first, so the duplicate is reported
        # only once it survives. Here we expect either format-rejection or
        # duplicate to fire — but uppercase should fail format. So the test
        # surfaces a single-name dup case below.
        self.assertTrue(any("invalid Debian package name" in e for e in errors))

    def test_rejects_exact_duplicate(self):
        errors = cfg.validate(_v({"apt_packages": ["tcpdump", "tcpdump"]}))
        self.assertTrue(any("duplicates apt_packages[0]" in e for e in errors))

    def test_validate_apt_format_helper(self):
        ok, _ = cfg.validate_apt_format("iptables-persistent")
        self.assertTrue(ok)
        ok, reason = cfg.validate_apt_format("foo;rm")
        self.assertFalse(ok)
        self.assertIn("invalid Debian package name", reason)


class ValidateSysctl(unittest.TestCase):
    def test_missing_key_is_valid(self):
        self.assertEqual(cfg.validate(_v()), [])

    def test_empty_object_is_valid(self):
        self.assertEqual(cfg.validate(_v({"sysctl": {}})), [])

    def test_must_be_object(self):
        errors = cfg.validate(_v({"sysctl": ["bad"]}))
        self.assertTrue(any("'sysctl' must be an object" in e for e in errors))

    def test_value_must_be_string(self):
        errors = cfg.validate(_v({"sysctl": {"net.ipv4.ip_forward": 1}}))
        self.assertTrue(any("must be a string" in e for e in errors))

    def test_invalid_key_with_dash(self):
        errors = cfg.validate(_v({"sysctl": {"bad-key": "1"}}))
        self.assertTrue(any("invalid sysctl key" in e for e in errors))

    def test_invalid_key_starting_with_digit(self):
        errors = cfg.validate(_v({"sysctl": {"1.bad": "1"}}))
        self.assertTrue(any("invalid sysctl key" in e for e in errors))

    def test_value_with_newline_rejected(self):
        errors = cfg.validate(_v({"sysctl": {"a.b": "line1\nline2"}}))
        self.assertTrue(any("must not contain newlines" in e for e in errors))

    def test_accepts_namespaced_key(self):
        self.assertEqual(
            cfg.validate(_v({"sysctl": {
                "net.ipv4.ip_forward": "1",
                "kernel.shmmax": "68719476736",
            }})),
            [],
        )


class ValidateCargoPackages(unittest.TestCase):
    def test_missing_key_is_valid(self):
        self.assertEqual(cfg.validate(_v()), [])

    def test_empty_list_is_valid(self):
        self.assertEqual(cfg.validate(_v({"cargo_packages": []})), [])

    def test_must_be_list(self):
        errors = cfg.validate(_v({"cargo_packages": "defmt-print"}))
        self.assertTrue(any("'cargo_packages' must be an array" in e for e in errors))

    def test_accepts_bare_name(self):
        self.assertEqual(cfg.validate(_v({"cargo_packages": ["defmt-print"]})), [])

    def test_accepts_name_at_version(self):
        self.assertEqual(
            cfg.validate(_v({"cargo_packages": ["defmt-print@0.3.13", "probe-rs@0.24.0"]})),
            [],
        )

    def test_accepts_underscore(self):
        self.assertEqual(cfg.validate(_v({"cargo_packages": ["my_crate"]})), [])

    def test_rejects_uppercase(self):
        errors = cfg.validate(_v({"cargo_packages": ["Defmt-Print"]}))
        self.assertTrue(any("invalid cargo crate spec" in e for e in errors))

    def test_rejects_git_url(self):
        # git+ URLs are out of scope for v1.
        errors = cfg.validate(_v({"cargo_packages": ["git+https://example.com/foo.git"]}))
        self.assertTrue(any("invalid cargo crate spec" in e for e in errors))

    def test_rejects_shell_metacharacters(self):
        for bad in ["foo;rm", "foo bar", "$evil"]:
            errors = cfg.validate(_v({"cargo_packages": [bad]}))
            self.assertTrue(
                any("invalid cargo crate spec" in e for e in errors),
                msg=f"expected rejection for {bad!r}, got {errors}",
            )

    def test_rejects_canonical_duplicate(self):
        errors = cfg.validate(_v({"cargo_packages": ["defmt-print", "defmt_print"]}))
        self.assertTrue(any("duplicates" in e for e in errors))

    def test_normalize_cargo_name(self):
        self.assertEqual(cfg.normalize_cargo_name("defmt-print@0.3.13"), "defmt-print")
        self.assertEqual(cfg.normalize_cargo_name("defmt_print"), "defmt-print")

    def test_validate_cargo_format_helper(self):
        ok, _ = cfg.validate_cargo_format("defmt-print")
        self.assertTrue(ok)
        ok, _ = cfg.validate_cargo_format("defmt-print@0.3.13")
        self.assertTrue(ok)
        ok, reason = cfg.validate_cargo_format("Bad Name")
        self.assertFalse(ok)
        self.assertIn("invalid cargo crate spec", reason)


class ValidateNpmPackages(unittest.TestCase):
    def test_missing_key_is_valid(self):
        self.assertEqual(cfg.validate(_v()), [])

    def test_empty_list_is_valid(self):
        self.assertEqual(cfg.validate(_v({"npm_packages": []})), [])

    def test_must_be_list(self):
        errors = cfg.validate(_v({"npm_packages": "express"}))
        self.assertTrue(any("'npm_packages' must be an array" in e for e in errors))

    def test_accepts_bare_name(self):
        self.assertEqual(cfg.validate(_v({"npm_packages": ["express"]})), [])

    def test_accepts_scoped_name(self):
        self.assertEqual(cfg.validate(_v({"npm_packages": ["@types/node"]})), [])

    def test_accepts_name_at_version(self):
        self.assertEqual(
            cfg.validate(_v({"npm_packages": ["lodash@4.17.21", "express@^4.18.0"]})),
            [],
        )

    def test_accepts_scoped_at_version(self):
        self.assertEqual(
            cfg.validate(_v({"npm_packages": ["@types/node@20.0.0"]})),
            [],
        )

    def test_rejects_uppercase(self):
        # npm registry rejects uppercase in the name part.
        errors = cfg.validate(_v({"npm_packages": ["Express"]}))
        self.assertTrue(any("invalid npm package spec" in e for e in errors))

    def test_rejects_shell_metacharacters(self):
        for bad in ["foo;rm", "foo bar", "$evil", "../etc/passwd"]:
            errors = cfg.validate(_v({"npm_packages": [bad]}))
            self.assertTrue(
                any("invalid npm package spec" in e for e in errors),
                msg=f"expected rejection for {bad!r}, got {errors}",
            )

    def test_rejects_overlong_name(self):
        # npm registry hard limit is 214 chars.
        too_long = "a" * 215
        errors = cfg.validate(_v({"npm_packages": [too_long]}))
        self.assertTrue(any("exceeds 214 chars" in e for e in errors))

    def test_rejects_canonical_duplicate(self):
        # Same package, different version specs — dedupe by name part.
        errors = cfg.validate(_v({"npm_packages": ["express", "express@4.18.0"]}))
        self.assertTrue(any("duplicates" in e for e in errors))

    def test_rejects_scoped_canonical_duplicate(self):
        errors = cfg.validate(_v({"npm_packages": ["@types/node", "@types/node@20.0.0"]}))
        self.assertTrue(any("duplicates" in e for e in errors))

    def test_normalize_npm_name_strips_version(self):
        self.assertEqual(cfg.normalize_npm_name("express@4.18.0"), "express")
        self.assertEqual(cfg.normalize_npm_name("@types/node@20.0.0"), "@types/node")
        self.assertEqual(cfg.normalize_npm_name("LODASH"), "lodash")

    def test_validate_npm_format_helper(self):
        for good in ["express", "@types/node", "lodash@4.17.21", "@scope/pkg@^1.0.0"]:
            ok, _ = cfg.validate_npm_format(good)
            self.assertTrue(ok, msg=f"expected {good!r} to validate")
        ok, reason = cfg.validate_npm_format("Bad Name")
        self.assertFalse(ok)
        self.assertIn("invalid npm package spec", reason)


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
        # `rustup` is round-tripped via extras (not yet a first-class field).
        a = cfg.BoxConfig.from_dict(_v({"rustup": {"default_toolchain": "stable"}}))
        b = cfg.BoxConfig.from_dict(_v({"rustup": {"default_toolchain": "nightly"}}))
        self.assertNotEqual(a.compute_hash(), b.compute_hash())

    def test_apt_sysctl_cargo_change_hash(self):
        base = cfg.BoxConfig.from_dict(_v())
        for change in [
            {"apt_packages": ["tcpdump"]},
            {"sysctl": {"net.ipv4.ip_forward": "1"}},
            {"cargo_packages": ["defmt-print"]},
        ]:
            other = cfg.BoxConfig.from_dict(_v(change))
            self.assertNotEqual(
                base.compute_hash(),
                other.compute_hash(),
                msg=f"hash should change for {change}",
            )


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
