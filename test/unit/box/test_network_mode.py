# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0
"""
The opt-in container network mode, end to end across the three trees it
touches: box/start_box.sh (the shell that runs docker), the box-side shim
verbs, and the CLI-side mirror of the allowlist.

Why the shell half is asserted textually: start_box.sh is not rendered from
anything and is not importable, so the only way to pin "--network is a
variable, not a literal" is to read the file. That is the same technique
test_firewall_port_allowlist.py and test_authorized_keys_sync.py already use,
via the same BEGIN/END sentinel convention.

The port-publishing interaction has a dedicated test here because it is the
one place this change touches a block another test parses verbatim: the guard
condition moved, and every `-p` literal had to stay put.

A switch to host happens only through `lager box-config apply`. Every other
container start renders the same config, so the renderer withholds a pending
switch from those starts; that gate is pinned here too.
"""

import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import types
import unittest


_REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..', '..'))
_START_BOX = os.path.join(_REPO, 'box', 'start_box.sh')
_PKG_DIR = os.path.join(_REPO, 'box', 'lager', 'box_config')

# What `lager box-config apply` sets on the start_box.sh it runs.
_CONFIRM = "LAGER_APPLY_NETWORK_SWITCH"


def _extract(topic):
    """Return the shell between the BEGIN/END sentinels naming `topic`."""
    begin, end = f"# --- BEGIN {topic}", f"# --- END {topic}"
    body, inside, seen = [], False, False
    with open(_START_BOX, encoding='utf-8') as f:
        for line in f.read().splitlines():
            if line.startswith(begin):
                inside, seen = True, True
                continue
            if line.startswith(end):
                inside = False
                continue
            if inside:
                body.append(line)
    assert seen, f"sentinel {begin!r} not found in {_START_BOX}"
    assert body, f"no shell extracted for {topic!r}"
    return "\n".join(body)


def _run_network_block(block, mode):
    """Run the network-mode block with `mode` as input, return the normalized
    value. The block echoes a warning of its own, so its stdout is muted and
    only the trailing printf is captured."""
    script = (
        f'BOX_CONFIG_NETWORK={mode!r}\n'
        f'{{\n{block}\n}} >/dev/null 2>&1\n'
        'printf "%s" "$BOX_CONFIG_NETWORK"\n'
    )
    out = subprocess.run(["bash", "-c", script],
                         capture_output=True, text=True, check=True)
    return out.stdout


def _start_box_text():
    with open(_START_BOX, encoding='utf-8') as f:
        return f.read()


def _load_shim_package():
    """config.py + box_config_cli.py under a synthetic package, so the shim's
    `from . import config as cfg` resolves without the real box package."""
    pkg = types.ModuleType("boxcfg_netmode")
    pkg.__path__ = [_PKG_DIR]
    sys.modules["boxcfg_netmode"] = pkg

    spec = importlib.util.spec_from_file_location(
        "boxcfg_netmode.config", os.path.join(_PKG_DIR, "config.py"))
    cfgmod = importlib.util.module_from_spec(spec)
    sys.modules["boxcfg_netmode.config"] = cfgmod
    spec.loader.exec_module(cfgmod)

    spec2 = importlib.util.spec_from_file_location(
        "boxcfg_netmode.box_config_cli", os.path.join(_PKG_DIR, "box_config_cli.py"))
    shim = importlib.util.module_from_spec(spec2)
    sys.modules["boxcfg_netmode.box_config_cli"] = shim
    spec2.loader.exec_module(shim)
    return cfgmod, shim


_cfg, _shim = _load_shim_package()


# ---------------------------------------------------------------------------
# start_box.sh
# ---------------------------------------------------------------------------

class StartBoxNetworkFlag(unittest.TestCase):
    def test_docker_run_takes_the_network_from_a_variable(self):
        """The whole point of the change. A literal here means a box can never
        opt into host mode no matter what its config says."""
        text = _start_box_text()
        self.assertIn('--network "$BOX_CONFIG_NETWORK" \\', text)
        self.assertNotIn('--network lagernet \\', text)

    def test_the_variable_has_a_default_before_the_config_is_sourced(self):
        """A box with no /etc/lager/box_config.json never runs the renderer, so
        the default has to be set unconditionally -- and before the source, or
        a rendered value would be overwritten by it."""
        text = _start_box_text()
        default_at = text.index('BOX_CONFIG_NETWORK=lagernet')
        source_at = text.index('source "$BOX_CONFIG_ARGS_FILE"')
        run_at = text.index('docker run -d')
        self.assertLess(default_at, source_at)
        self.assertLess(source_at, run_at)

    def test_pending_has_a_default_before_the_config_is_sourced(self):
        text = _start_box_text()
        self.assertLess(text.index('BOX_CONFIG_NETWORK_PENDING='),
                        text.index('source "$BOX_CONFIG_ARGS_FILE"'))

    def test_an_unknown_mode_falls_back_instead_of_being_passed_through(self):
        """An unknown --network makes `docker run` fail outright, which would
        take the box down over a typo. The block corrects rather than propagates."""
        block = _extract("network mode")
        for mode, expected in (("lagernet", "lagernet"), ("host", "host"),
                               ("bridge", "lagernet"), ("", "lagernet")):
            with self.subTest(mode=mode):
                self.assertEqual(_run_network_block(block, mode), expected)

    def test_a_withheld_switch_is_announced(self):
        """An operator who set host and then ran an update must be told why the
        box is still on lagernet, and what makes the switch."""
        block = _extract("network mode")
        script = (
            "BOX_CONFIG_NETWORK=lagernet\n"
            "BOX_CONFIG_NETWORK_PENDING=host\n"
            f"{block}\n"
            'printf "RESULT=%s" "$BOX_CONFIG_NETWORK"\n'
        )
        out = subprocess.run(["bash", "-c", script],
                             capture_output=True, text=True, check=True).stdout
        self.assertIn("configured but not applied", out)
        self.assertIn("lager box-config apply", out)
        self.assertTrue(out.endswith("RESULT=lagernet"), out)

    def test_nothing_is_announced_when_nothing_is_withheld(self):
        block = _extract("network mode")
        script = "BOX_CONFIG_NETWORK=host\nBOX_CONFIG_NETWORK_PENDING=\n" + block + "\n"
        out = subprocess.run(["bash", "-c", script],
                             capture_output=True, text=True, check=True).stdout
        self.assertEqual(out, "")


class PortPublishingUnderHostMode(unittest.TestCase):
    """The guard condition moved into a block another test parses verbatim."""

    def _publish_args(self, *, no_publish="", network="lagernet", uart_disabled=""):
        block = _extract("port publishing")
        script = (
            f'NO_PUBLISH={no_publish!r}\n'
            f'BOX_CONFIG_NETWORK={network!r}\n'
            f'UART_SERVICE_DISABLED={uart_disabled!r}\n'
            f'{{\n{block}\n}} >/dev/null 2>&1\n'
            'printf "%s\\n" "${PORT_PUBLISH_ARGS[@]}"\n'
        )
        out = subprocess.run(["bash", "-c", script],
                             capture_output=True, text=True, check=True)
        return [ln for ln in out.stdout.splitlines() if ln.strip()]

    def test_lagernet_still_publishes_every_port(self):
        args = self._publish_args()
        self.assertIn("5000:5000", args)
        self.assertIn("9000:9000", args)
        self.assertIn("8301:5000", args)

    def test_host_mode_publishes_nothing(self):
        """docker ignores -p under host networking; not emitting it keeps the
        run line honest about what it is asking for."""
        self.assertEqual(self._publish_args(network="host"), [])

    def test_no_publish_still_wins_on_lagernet(self):
        self.assertEqual(self._publish_args(no_publish="1"), [])

    def test_every_published_port_is_still_inside_the_sentinels(self):
        """Regression guard for test_firewall_port_allowlist.py, which asserts
        set-equality between the -p literals in this block and the firewall
        allowlist. Moving a -p out of the block silently narrows the firewall."""
        block = _extract("port publishing")
        found = set(re.findall(r"-p\s+([0-9-]+):[0-9-]+", block))
        self.assertEqual(
            found,
            {"5000", "8301", "8080", "8081-8090", "8100", "8765",
             "2331-2342", "4444-4447", "6666-6669", "9090-9097", "9000"},
        )


# ---------------------------------------------------------------------------
# The renderer contract start_box.sh depends on
# ---------------------------------------------------------------------------

class _Renderer:
    """Runs render_docker_args.py the way start_box.sh does."""

    def _render(self, config_dict, *, applied=None, applied_raw=None, confirm=None):
        """`applied` is the last applied snapshot (None: no apply has succeeded
        yet), `applied_raw` writes the snapshot file verbatim, and `confirm` is
        the value of the variable `lager box-config apply` sets."""
        d = tempfile.mkdtemp(prefix="lager-netmode-")
        self.addCleanup(shutil.rmtree, d, True)
        cfg_path = os.path.join(d, "box_config.json")
        out_path = os.path.join(d, "out.sh")
        applied_path = os.path.join(d, "box_config.applied.json")
        if config_dict is not None:
            with open(cfg_path, "w", encoding="utf-8") as f:
                json.dump(config_dict, f)
        if applied is not None:
            with open(applied_path, "w", encoding="utf-8") as f:
                json.dump(applied, f)
        if applied_raw is not None:
            with open(applied_path, "w", encoding="utf-8") as f:
                f.write(applied_raw)
        env = {k: v for k, v in os.environ.items() if k != _CONFIRM}
        if confirm is not None:
            env[_CONFIRM] = confirm
        proc = subprocess.run(
            [sys.executable,
             os.path.join(_PKG_DIR, "render_docker_args.py"),
             cfg_path, out_path, applied_path],
            capture_output=True, text=True, env=env)
        body = ""
        if os.path.exists(out_path):
            with open(out_path, encoding="utf-8") as f:
                body = f.read()
        return proc.returncode, body

    def _sourced(self, body, name="BOX_CONFIG_NETWORK"):
        out = subprocess.run(
            ["bash", "-c", f'{body}\nprintf "%s" "${name}"'],
            capture_output=True, text=True, check=True)
        return out.stdout


class RendererEmitsTheNetwork(_Renderer, unittest.TestCase):
    def test_default_config_renders_the_default(self):
        rc, body = self._render({"version": 1})
        self.assertEqual(rc, 0)
        self.assertEqual(self._sourced(body), "lagernet")

    def test_host_config_renders_host_on_apply(self):
        rc, body = self._render({"version": 1, "network_mode": "host"}, confirm="1")
        self.assertEqual(rc, 0)
        self.assertEqual(self._sourced(body), "host")

    def test_missing_config_still_defines_the_variable(self):
        """start_box.sh expands --network unconditionally. An undefined value
        here would become an empty --network argument, which docker rejects."""
        rc, body = self._render(None)
        self.assertEqual(rc, 0)
        self.assertEqual(self._sourced(body), "lagernet")
        self.assertIn("BOX_CONFIG_NETWORK_PENDING=", body)

    def test_invalid_config_still_defines_the_variable(self):
        rc, body = self._render({"version": 1, "mounts": "not-a-list"})
        self.assertEqual(rc, 1)
        self.assertEqual(self._sourced(body), "lagernet")
        self.assertIn("BOX_CONFIG_NETWORK_PENDING=", body)


class ASwitchToHostTakesEffectOnlyThroughApply(_Renderer, unittest.TestCase):
    """start_box.sh renders box_config.json on every container start: `lager
    update`, `lager install`, `box-config restart`, a hand-run script. The
    reachability check lives in `apply` alone, so a refused apply used to leave
    `host` in the config for the next routine update to apply unchecked. Every
    other start now keeps the mode the last successful apply recorded."""

    HOST = {"version": 1, "network_mode": "host"}

    def test_a_start_that_is_not_apply_withholds_host(self):
        rc, body = self._render(self.HOST)
        self.assertEqual(rc, 0)
        self.assertEqual(self._sourced(body), "lagernet")
        self.assertEqual(self._sourced(body, "BOX_CONFIG_NETWORK_PENDING"), "host")

    def test_apply_makes_the_switch(self):
        rc, body = self._render(self.HOST, confirm="1")
        self.assertEqual(rc, 0)
        self.assertEqual(self._sourced(body), "host")
        self.assertEqual(self._sourced(body, "BOX_CONFIG_NETWORK_PENDING"), "")

    def test_only_the_exact_confirmation_counts(self):
        for value in ("", "0", "true", "yes"):
            with self.subTest(value=value):
                rc, body = self._render(self.HOST, confirm=value)
                self.assertEqual(self._sourced(body), "lagernet")

    def test_a_box_already_on_host_stays_on_host(self):
        """Once an apply has succeeded the snapshot records host, so updating a
        box that uses host mode must not take it back off."""
        rc, body = self._render(self.HOST, applied=self.HOST)
        self.assertEqual(rc, 0)
        self.assertEqual(self._sourced(body), "host")
        self.assertEqual(self._sourced(body, "BOX_CONFIG_NETWORK_PENDING"), "")

    def test_returning_to_lagernet_needs_no_apply(self):
        """The safe direction: it restores the published ports."""
        rc, body = self._render({"version": 1}, applied=self.HOST)
        self.assertEqual(rc, 0)
        self.assertEqual(self._sourced(body), "lagernet")
        self.assertEqual(self._sourced(body, "BOX_CONFIG_NETWORK_PENDING"), "")

    def test_an_unreadable_snapshot_withholds_host(self):
        """A snapshot that cannot be read must not make the switch, and must not
        fail the render either: the render also carries mounts and env."""
        for raw in ("{ not json", '{"network_mode": "host"}', "[]"):
            with self.subTest(raw=raw):
                rc, body = self._render(self.HOST, applied_raw=raw)
                self.assertEqual(rc, 0)
                self.assertEqual(self._sourced(body), "lagernet")
                self.assertEqual(
                    self._sourced(body, "BOX_CONFIG_NETWORK_PENDING"), "host")


# ---------------------------------------------------------------------------
# Box-side shim verbs
# ---------------------------------------------------------------------------

class NetworkModeShimVerbs(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.cfg_path = os.path.join(self.tmp.name, "box_config.json")

        orig_path, orig_save = _cfg.BOX_CONFIG_PATH, _cfg.save
        orig_audit, orig_stdout = _shim._audit, _shim._stdout_json
        _cfg.BOX_CONFIG_PATH = self.cfg_path
        _cfg.save = lambda c, path=None: orig_save(c, path or self.cfg_path)
        _shim._audit = lambda verb, args: None
        self.responses = []
        _shim._stdout_json = self.responses.append

        def _restore():
            _cfg.BOX_CONFIG_PATH = orig_path
            _cfg.save = orig_save
            _shim._audit = orig_audit
            _shim._stdout_json = orig_stdout
        self.addCleanup(_restore)

    def _saved(self):
        with open(self.cfg_path, encoding="utf-8") as f:
            return json.load(f)

    def _last(self):
        return self.responses[-1]

    def test_set_persists_host(self):
        _shim._cmd_network_mode_set(json.dumps({"mode": "host"}))
        self.assertTrue(self._last()["ok"], msg=self._last())
        self.assertEqual(self._saved()["network_mode"], "host")

    def test_set_is_idempotent(self):
        _shim._cmd_network_mode_set(json.dumps({"mode": "host"}))
        _shim._cmd_network_mode_set(json.dumps({"mode": "host"}))
        self.assertTrue(self._last()["ok"])
        self.assertEqual(self._saved()["network_mode"], "host")

    def test_set_rejects_an_unknown_mode(self):
        _shim._cmd_network_mode_set(json.dumps({"mode": "bridge"}))
        self.assertFalse(self._last()["ok"])
        self.assertFalse(os.path.exists(self.cfg_path))

    def test_set_rejects_a_non_string(self):
        _shim._cmd_network_mode_set(json.dumps({"mode": 5}))
        self.assertFalse(self._last()["ok"])

    def test_unset_drops_the_key_rather_than_writing_the_default(self):
        """Leaving "network_mode": "lagernet" behind would keep the config's
        hash changed forever, so a box that tried host mode once would never
        return to the hash it had before."""
        _shim._cmd_network_mode_set(json.dumps({"mode": "host"}))
        _shim._cmd_network_mode_unset()
        self.assertTrue(self._last()["ok"], msg=self._last())
        self.assertNotIn("network_mode", self._saved())

    def test_unset_reports_the_previous_mode(self):
        _shim._cmd_network_mode_set(json.dumps({"mode": "host"}))
        _shim._cmd_network_mode_unset()
        self.assertEqual(self._last()["previous"], "host")

    def test_set_then_unset_restores_the_original_hash(self):
        _shim._cmd_network_mode_set(json.dumps({"mode": "host"}))
        before = _cfg.load(self.cfg_path).compute_hash()
        _shim._cmd_network_mode_unset()
        after = _cfg.load(self.cfg_path).compute_hash()
        self.assertNotEqual(before, after)
        baseline = _cfg.BoxConfig.from_dict(
            {**self._saved(), "network_mode": "lagernet"}).compute_hash()
        self.assertEqual(after, baseline)


class NetworkModeShowVerb(NetworkModeShimVerbs):
    """The read verb. It exists because the generic `show` verb predates the
    feature and answers identically on a box that has it and one that does not,
    so `show` could not be used to confirm a deploy landed."""

    def test_reports_the_default_without_creating_a_config(self):
        _shim._cmd_network_mode_show()
        self.assertEqual(self._last(),
                         {"ok": True, "exists": False,
                          "mode": "lagernet", "explicit": False})
        self.assertFalse(os.path.exists(self.cfg_path))

    def test_reports_an_explicit_mode(self):
        _shim._cmd_network_mode_set(json.dumps({"mode": "host"}))
        _shim._cmd_network_mode_show()
        self.assertEqual(self._last()["mode"], "host")
        self.assertTrue(self._last()["explicit"])

    def test_a_config_at_the_default_reads_as_not_explicit(self):
        _shim._cmd_network_mode_set(json.dumps({"mode": "host"}))
        _shim._cmd_network_mode_unset()
        _shim._cmd_network_mode_show()
        self.assertEqual(self._last()["mode"], "lagernet")
        self.assertFalse(self._last()["explicit"])
        self.assertTrue(self._last()["exists"])


class SettingTheModeBundlesNothingElse(NetworkModeShimVerbs):
    def test_a_fresh_box_gets_no_volume_it_did_not_ask_for(self):
        """Observed on hardware: setting the mode on a box with no config also
        introduced `+ box-tools -> /opt/box-tools`, which the next apply then
        applied. A single-setting change must carry only that setting."""
        _shim._cmd_network_mode_set(json.dumps({"mode": "host"}))
        self.assertEqual(self._saved()["volumes"], [])

    def test_it_preserves_an_existing_config_rather_than_replacing_it(self):
        """`volume add` on a fresh box does seed the box-tools default, and
        that stays correct -- someone provisioning a box deliberately wants it.
        The mode change must carry that config forward untouched, adding
        nothing and dropping nothing."""
        _shim._cmd_volume_add(json.dumps({"name": "keepme", "container": "/keep"}))
        before = [v["name"] for v in self._saved()["volumes"]]
        _shim._cmd_network_mode_set(json.dumps({"mode": "host"}))
        self.assertEqual([v["name"] for v in self._saved()["volumes"]], before)
        self.assertIn("keepme", before)

    def test_unset_also_bundles_nothing(self):
        _shim._cmd_network_mode_unset()
        self.assertEqual(self._saved()["volumes"], [])


# ---------------------------------------------------------------------------
# Cross-tree agreement
# ---------------------------------------------------------------------------

class AllowlistDoesNotDrift(unittest.TestCase):
    """cli/ and box/ ship separately and cannot import each other, so the
    allowlist is written twice. A box accepting a mode the CLI refuses (or the
    reverse) is exactly the drift this repo keeps re-learning."""

    def test_cli_and_box_agree_on_the_modes_and_the_default(self):
        from cli.commands.box import config as cli_cfg
        self.assertEqual(tuple(cli_cfg.NETWORK_MODES), tuple(_cfg.NETWORK_MODES))
        self.assertEqual(cli_cfg._DEFAULT_NETWORK_MODE, _cfg.DEFAULT_NETWORK_MODE)

    def test_the_default_is_one_of_the_allowed_modes(self):
        self.assertIn(_cfg.DEFAULT_NETWORK_MODE, _cfg.NETWORK_MODES)

    def test_start_box_accepts_exactly_the_allowlisted_modes(self):
        """The shell has its own copy of the allowlist in a case statement."""
        block = _extract("network mode")
        for mode in _cfg.NETWORK_MODES:
            with self.subTest(mode=mode):
                self.assertEqual(_run_network_block(block, mode), mode)

    def test_cli_and_renderer_agree_on_the_confirmation(self):
        """`apply` sets this variable and the renderer reads it. If the two
        names drift, apply can never make the switch and nothing reports it."""
        from cli.commands.box import config as cli_cfg
        spec = importlib.util.spec_from_file_location(
            "netmode_render_docker_args",
            os.path.join(_PKG_DIR, "render_docker_args.py"))
        render = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(render)
        self.assertEqual(render._CONFIRM_ENV, cli_cfg._NETWORK_SWITCH_CONFIRM_ENV)
        self.assertEqual(render._CONFIRM_ENV, _CONFIRM)


if __name__ == "__main__":
    unittest.main()
