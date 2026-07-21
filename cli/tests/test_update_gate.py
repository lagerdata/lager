# Copyright 2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

"""
Tests for the update flow's rebuild gate: probe parsing, the build-hash
mismatch predicate, and the early-exit verdict (including container liveness).
"""
import pytest

from cli.commands.utility.update import (
    _build_hash_mismatch,
    _parse_probe_output,
    _probe_shell_script,
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
