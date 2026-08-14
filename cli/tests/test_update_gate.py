# Copyright 2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

"""
Tests for the update flow's rebuild gate: probe parsing, the build-hash
mismatch predicate, and the early-exit verdict (including container liveness).
"""
import os
import stat
import subprocess

import pytest

from cli.commands.utility.update import (
    _box_image_ref_for_version,
    _build_hash_mismatch,
    _deployed_version_stale,
    _docker_pull_retag_cmd,
    _parse_probe_output,
    _probe_shell_script,
    _pull_shell_script,
    _rebuild_gate_verdict,
)

IN_SYNC = dict(
    git_sync_confirmed=True,
    needs_pull=False,
    needs_flatten=False,
    hash_mismatch=False,
    force=False,
)

SHA_A = 'a' * 64
SHA_B = 'b' * 64


class TestParseProbeOutput:
    @pytest.mark.parametrize('raw', ['1', '0', ''])
    def test_lager_running_values_pass_through(self, raw):
        facts = _parse_probe_output(f'LAGER_PROBE_LAGER_RUNNING={raw}\n')
        assert facts['LAGER_RUNNING'] == raw

    def test_absent_key_stays_absent(self):
        facts = _parse_probe_output('LAGER_PROBE_ETC_VERSION=1.2.3\n')
        assert 'LAGER_RUNNING' not in facts

    def test_noise_lines_ignored(self):
        stdout = 'Welcome to the box\nLAGER_PROBE_LAGER_RUNNING=1\nsudo lecture\n'
        assert _parse_probe_output(stdout) == {'LAGER_RUNNING': '1'}

    def test_probe_script_emits_liveness_fact(self):
        assert 'LAGER_PROBE_LAGER_RUNNING=' in _probe_shell_script()


class TestBuildHashMismatch:
    def test_changed_inputs_mismatch(self):
        assert _build_hash_mismatch(SHA_A, SHA_B)

    def test_matching_inputs_no_mismatch(self):
        assert not _build_hash_mismatch(SHA_A, SHA_A)

    def test_failed_sentinel_forces_mismatch(self):
        # A failed build stores 'FAILED'; any real recomputed sha must mismatch
        # so the retry rebuilds instead of early-exiting.
        assert _build_hash_mismatch(SHA_A, 'FAILED')

    def test_absent_stored_hash_skips_auto_invalidation(self):
        assert not _build_hash_mismatch(SHA_A, '')

    def test_unmeasurable_new_hash_skips_auto_invalidation(self):
        assert not _build_hash_mismatch('', SHA_A)


class TestRebuildGateVerdict:
    def test_in_sync_and_running_skips(self):
        assert _rebuild_gate_verdict({'LAGER_RUNNING': '1'}, **IN_SYNC) == 'skip'

    def test_container_down_blocks_skip(self):
        # The reported failure: a prior update removed the containers and died
        # mid-build; source reads as in-sync but nothing is running.
        assert _rebuild_gate_verdict({'LAGER_RUNNING': '0'}, **IN_SYNC) == 'container-down'

    @pytest.mark.parametrize('facts', [{}, {'LAGER_RUNNING': ''}])
    def test_unknown_liveness_fails_open(self, facts):
        assert _rebuild_gate_verdict(facts, **IN_SYNC) == 'skip'

    @pytest.mark.parametrize('override', [
        dict(git_sync_confirmed=False),
        dict(needs_pull=True),
        dict(needs_flatten=True),
        dict(hash_mismatch=True),
        dict(force=True),
    ])
    def test_source_divergence_rebuilds_regardless_of_liveness(self, override):
        args = {**IN_SYNC, **override}
        for facts in ({'LAGER_RUNNING': '1'}, {'LAGER_RUNNING': '0'}, {}):
            assert _rebuild_gate_verdict(facts, **args) == 'rebuild'

    def test_failed_sentinel_feeds_through_to_rebuild(self):
        mismatch = _build_hash_mismatch(SHA_A, 'FAILED')
        args = {**IN_SYNC, 'hash_mismatch': mismatch}
        assert _rebuild_gate_verdict({'LAGER_RUNNING': '1'}, **args) == 'rebuild'


class TestDeployedVersionStale:
    def test_matching_versions_not_stale(self):
        assert not _deployed_version_stale('0.32.1', '0.32.1|0.32.1')

    def test_tree_ahead_of_deploy_is_stale(self):
        # The tree-ahead-of-deploy state: a prior update pulled the new code
        # but exited before the rebuild, so the container still serves the
        # version last recorded by a successful update.
        assert _deployed_version_stale('0.32.1', '0.32.0|0.32.1')

    def test_legacy_value_without_cli_part(self):
        assert not _deployed_version_stale('0.31.0', '0.31.0')
        assert _deployed_version_stale('0.32.0', '0.31.0')

    @pytest.mark.parametrize('etc_raw', ['', None, '   ', '|0.32.1'])
    def test_unknown_deployed_fails_open(self, etc_raw):
        assert not _deployed_version_stale('0.32.1', etc_raw)

    def test_unknown_tree_version_fails_open(self):
        assert not _deployed_version_stale('', '0.32.0|0.32.0')


class TestProbeLivenessSnippet:
    """Run the real probe script against a stubbed `docker` to pin the
    liveness fact's tri-state contract at the shell level.

    The script is designed to exit 0 and emit a value (possibly empty) for
    every fact regardless of what is installed on the host, so executing it
    verbatim also guards against a syntax error sneaking into the heredoc.
    """

    def _probe_facts(self, tmp_path, docker_body):
        shim_dir = tmp_path / 'bin'
        shim_dir.mkdir()
        shim = shim_dir / 'docker'
        shim.write_text(f'#!/bin/sh\n{docker_body}\n')
        shim.chmod(shim.stat().st_mode | stat.S_IXUSR)
        env = dict(os.environ, PATH=f'{shim_dir}:{os.environ["PATH"]}')
        result = subprocess.run(
            ['sh'], input=_probe_shell_script(), text=True,
            capture_output=True, env=env, timeout=30,
        )
        assert result.returncode == 0, result.stderr
        return _parse_probe_output(result.stdout)

    def test_running_container_reports_1(self, tmp_path):
        facts = self._probe_facts(tmp_path, 'printf "lager\\nstout\\n"')
        assert facts['LAGER_RUNNING'] == '1'

    def test_substring_named_container_does_not_count(self, tmp_path):
        # `docker ps --filter name=lager` matches substrings, so a container
        # named e.g. `lagertest` comes back from the filter; only an exact
        # `lager` row may count as the box container.
        facts = self._probe_facts(tmp_path, 'printf "lagertest\\n"')
        assert facts['LAGER_RUNNING'] == '0'

    def test_no_rows_reports_0(self, tmp_path):
        facts = self._probe_facts(tmp_path, ':')
        assert facts['LAGER_RUNNING'] == '0'

    def test_docker_failure_reports_unknown(self, tmp_path):
        facts = self._probe_facts(tmp_path, 'exit 1')
        assert facts['LAGER_RUNNING'] == ''


class TestProbeHostCliFacts:
    def test_probe_script_emits_all_host_cli_keys(self):
        script = _probe_shell_script()
        for key in (
            'HOST_CLI_VERSION', 'HOST_VENV_DIR', 'HOST_PY_VERSION',
            'HOST_ENSUREPIP', 'HOST_BOX_CLI_DIR',
        ):
            assert f'LAGER_PROBE_{key}=' in script, key

    def test_splice_placeholder_fully_replaced(self):
        assert '__HOST_CLI_PROBE__' not in _probe_shell_script()

    def test_parser_round_trips_host_keys(self):
        facts = _parse_probe_output(
            'LAGER_PROBE_HOST_CLI_VERSION=0.32.5\n'
            'LAGER_PROBE_HOST_VENV_DIR=1\n'
        )
        assert facts['HOST_CLI_VERSION'] == '0.32.5'
        assert facts['HOST_VENV_DIR'] == '1'

    def test_old_probe_output_leaves_host_keys_absent(self):
        # Callers must tolerate probe output that predates the host-CLI facts.
        facts = _parse_probe_output('LAGER_PROBE_ETC_VERSION=1.2.3\n')
        assert 'HOST_CLI_VERSION' not in facts


class TestGateIgnoresHostCliFacts:
    def test_host_cli_mismatch_never_forces_rebuild(self):
        # A missing/stale host CLI reconciles on the fast path; it must never
        # trigger a container rebuild.
        facts = {
            'LAGER_RUNNING': '1',
            'HOST_CLI_VERSION': '',
            'HOST_VENV_DIR': '0',
        }
        assert _rebuild_gate_verdict(facts, **IN_SYNC) == 'skip'


class TestPullShellScript:
    def test_adds_cli_directory_not_single_file(self):
        script = _pull_shell_script('main', 'origin/main')
        assert 'git sparse-checkout add cli 2>/dev/null || true' in script
        assert 'cli/__init__.py' not in script

    def test_udev_rules_add_stays_strict(self):
        udev_clause = _pull_shell_script('main', 'origin/main').split('modprobe_d')[0]
        assert 'git sparse-checkout add udev_rules; }' in udev_clause
        assert '|| true' not in udev_clause

    def test_checkout_and_reset_use_given_refs(self):
        script = _pull_shell_script('v0.32.5', 'v0.32.5')
        assert 'git checkout -f v0.32.5' in script
        assert 'git reset --hard v0.32.5' in script


class TestBoxImageRef:
    def test_release_tag_maps_to_ghcr(self):
        assert _box_image_ref_for_version('v0.36.0') == 'ghcr.io/lagerdata/lager-box:v0.36.0'
        assert _box_image_ref_for_version('0.36.0') == 'ghcr.io/lagerdata/lager-box:v0.36.0'

    def test_prerelease_suffix_accepted(self):
        assert _box_image_ref_for_version('v0.36.0-rc1') == (
            'ghcr.io/lagerdata/lager-box:v0.36.0-rc1'
        )

    def test_branches_and_empty_are_not_published(self):
        assert _box_image_ref_for_version('main') is None
        assert _box_image_ref_for_version('perf/foo') is None
        assert _box_image_ref_for_version('') is None
        assert _box_image_ref_for_version(None) is None

    def test_pull_retag_quotes_refs(self):
        cmd = _docker_pull_retag_cmd('ghcr.io/lagerdata/lager-box:v0.36.0')
        assert 'docker pull ghcr.io/lagerdata/lager-box:v0.36.0' in cmd
        assert 'docker tag ghcr.io/lagerdata/lager-box:v0.36.0 lager' in cmd
        # Metacharacters must be shell-quoted so a hostile tag cannot break out.
        nasty = _docker_pull_retag_cmd('ghcr.io/x/y:v1;rm')
        assert "'ghcr.io/x/y:v1;rm'" in nasty
        assert nasty.count('&&') == 1
