# Copyright 2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

"""
Tests for the update flow's rebuild gate: probe parsing, the build-hash
mismatch predicate, and the early-exit verdict (including container liveness).
"""
import os
import shutil
import stat
import subprocess

import pytest

from cli.commands.utility.update import (
    _REGISTRY_TIMEOUT,
    _acquire_box_image,
    _box_image_pull_enabled,
    _box_image_ref_for_version,
    _build_hash_at_ref_shell_cmd,
    _build_hash_mismatch,
    _build_hash_shell_cmd,
    _deployed_version_stale,
    _docker_build_line_summary,
    _parse_probe_output,
    _deps_preview,
    _probe_shell_script,
    _digest_ref,
    _docker_inspect_label_cmd,
    _docker_pull_cmd,
    _docker_tag_cmd,
    _docker_untag_cmd,
    _pull_miss_is_actionable,
    _pull_shell_script,
    _rebuild_gate_verdict,
    _resolve_image_digest,
    resolve_version_ref,
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


class TestDepsPreviewAtTargetRef:
    """`--check` must hash the *target* ref when a pull is pending.

    Field repro: a box ~120 commits behind whose pre-pull Dockerfile still
    matched the stored hash. The preview read that as "cache valid / ~90s";
    the pull then changed the Dockerfile and the real update took ~6 min.
    """

    # Everything that is not about the target ref, held constant.
    BASE = dict(force=False, needs_flatten=False, is_rollback=False,
                commits_ahead=0)

    def test_forward_jump_target_mismatch_reports_fresh_build(self):
        status, rebuild_certain, _ = _deps_preview(
            SHA_A, SHA_A,          # pre-pull tree still matches stored
            target_hash=SHA_B,     # ref about to be checked out differs
            needs_pull=True,
            **self.BASE,
        )
        assert rebuild_certain is True
        assert 'target Dockerfile' in status

    def test_forward_jump_target_match_reports_cache_valid(self):
        status, rebuild_certain, _ = _deps_preview(
            SHA_A, SHA_A, target_hash=SHA_A, needs_pull=True, **self.BASE,
        )
        assert rebuild_certain is False
        assert 'target matches' in status

    def test_unmeasurable_target_is_unknown_not_cache_valid(self):
        status, _, _ = _deps_preview(
            SHA_A, SHA_A, target_hash='', needs_pull=True, **self.BASE,
        )
        assert 'unknown until pull' in status
        assert 'cache valid' not in status

    def test_in_sync_uses_working_tree_hash(self):
        status, rebuild_certain, _ = _deps_preview(
            SHA_B, SHA_A, target_hash='', needs_pull=False, **self.BASE,
        )
        assert rebuild_certain is True
        assert 'Dockerfile, requirements or box source changed' in status

    def test_flatten_still_wins_over_a_matching_target_hash(self):
        # Regression guard for the reconciliation of this change with the
        # flatten fix. A box on the old `box/` subdir layout rebuilds
        # whatever the target digest says, because the flatten moves every
        # source path. Reading the target ref must not resurrect the "cache
        # valid" claim that `needs_flatten` exists to prevent.
        status, rebuild_certain, _ = _deps_preview(
            SHA_A, SHA_A,
            target_hash=SHA_A,
            needs_pull=True,
            force=False, needs_flatten=True, is_rollback=False,
            commits_ahead=0,
        )
        assert rebuild_certain is True
        assert 'cache valid' not in status
        assert 'flatten' in status


class TestBuildHashAtRefShellCmd:
    def test_emits_git_show_for_dockerfile_blob(self):
        script = _build_hash_at_ref_shell_cmd('origin/main')
        assert 'git show origin/main:box/lager/docker/box.Dockerfile' in script
        assert 'git cat-file -e origin/main:box/lager/docker/box.Dockerfile' in script

    def test_emits_source_tree_walk(self):
        # Must match main's `_BUILD_HASH_SOURCE_DIRS` composition or --check
        # spuriously reports a rebuild against stored hashes that include
        # every file under ~/box/lager.
        script = _build_hash_at_ref_shell_cmd('origin/main')
        assert 'git ls-tree -r --name-only origin/main box/lager' in script

    def test_rejects_metacharacters(self):
        assert _build_hash_at_ref_shell_cmd('main; rm -rf /') == 'echo ""'
        assert _build_hash_at_ref_shell_cmd('') == 'echo ""'

    def test_uses_only_posix_shell_constructs(self):
        # The remote login shell may be dash. Bash pattern substitution and
        # `printf -v` both silently changed the digest under /bin/sh.
        script = _build_hash_at_ref_shell_cmd('origin/main')
        assert 'printf -v' not in script
        assert '/#' not in script


@pytest.mark.skipif(
    shutil.which('sha256sum') is None,
    reason='needs GNU sha256sum (present on boxes and CI; macOS ships shasum)',
)
class TestBuildHashAtRefMatchesWorkingTree:
    """Execute both hashers under ``sh`` against a fake box layout.

    This is the invariant #12 depends on: the target-ref digest must equal the
    working-tree digest that `/etc/lager/build-hash` stores, or every --check
    would report a spurious rebuild. Verified end-to-end (real git, real
    sha256sum, dash-compatible) rather than by asserting on substrings.
    """

    DOCKERFILE_GIT_PATH = 'box/lager/docker/box.Dockerfile'
    DOCKERFILE_TREE_PATH = 'lager/docker/box.Dockerfile'
    SOURCE_GIT_PATH = 'box/lager/nets/net.py'
    SOURCE_TREE_PATH = 'lager/nets/net.py'

    def _fake_box(self, tmp_path, dockerfile_body, source_body='print("ok")\n'):
        """Build $HOME/box as the boxes have it: git tracks the `box/` prefix,
        the working tree is the flattened layout. Includes a source file so
        the `_BUILD_HASH_SOURCE_DIRS` walk is exercised (not just Dockerfile).
        """
        home = tmp_path / 'home'
        box = home / 'box'
        for rel, body in (
            (self.DOCKERFILE_GIT_PATH, dockerfile_body),
            (self.DOCKERFILE_TREE_PATH, dockerfile_body),
            (self.SOURCE_GIT_PATH, source_body),
            (self.SOURCE_TREE_PATH, source_body),
        ):
            path = box / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(body)
        env = dict(os.environ, HOME=str(home))
        run = lambda *args: subprocess.run(
            args, cwd=box, env=env, capture_output=True, text=True, timeout=30,
        )
        run('git', 'init', '-q', '-b', 'main')
        run('git', 'config', 'user.email', 'test@example.com')
        run('git', 'config', 'user.name', 'test')
        run('git', 'add', self.DOCKERFILE_GIT_PATH, self.SOURCE_GIT_PATH)
        commit = run('git', 'commit', '-q', '-m', 'dockerfile')
        assert commit.returncode == 0, commit.stderr
        return home, env

    def _sh(self, snippet, env, cwd):
        result = subprocess.run(
            ['sh'], input=snippet, text=True, capture_output=True,
            env=env, cwd=cwd, timeout=30,
        )
        return result.stdout.strip()

    def test_ref_digest_equals_working_tree_digest(self, tmp_path):
        home, env = self._fake_box(tmp_path, 'FROM python:3.12-slim\n')
        working = self._sh(_build_hash_shell_cmd(), env, home / 'box')
        at_ref = self._sh(_build_hash_at_ref_shell_cmd('HEAD'), env, home)
        assert working, 'working-tree hasher produced nothing'
        assert at_ref == working

    def test_ref_digest_differs_when_target_dockerfile_changes(self, tmp_path):
        home, env = self._fake_box(tmp_path, 'FROM python:3.12-slim\n')
        before = self._sh(_build_hash_at_ref_shell_cmd('HEAD'), env, home)
        box = home / 'box'
        (box / self.DOCKERFILE_GIT_PATH).write_text('FROM python:3.13-slim\n')
        subprocess.run(
            ['git', 'commit', '-qam', 'bump base'], cwd=box, env=env,
            capture_output=True, text=True, timeout=30,
        )
        after = self._sh(_build_hash_at_ref_shell_cmd('HEAD'), env, home)
        # The forward-jump case: working tree still matches the stored hash
        # while the ref about to be checked out does not.
        working = self._sh(_build_hash_shell_cmd(), env, box)
        assert after != before
        assert after != working

    def test_ref_digest_differs_when_source_file_changes(self, tmp_path):
        home, env = self._fake_box(tmp_path, 'FROM python:3.12-slim\n')
        before = self._sh(_build_hash_at_ref_shell_cmd('HEAD'), env, home)
        box = home / 'box'
        (box / self.SOURCE_GIT_PATH).write_text('print("changed")\n')
        subprocess.run(
            ['git', 'commit', '-qam', 'touch source'], cwd=box, env=env,
            capture_output=True, text=True, timeout=30,
        )
        after = self._sh(_build_hash_at_ref_shell_cmd('HEAD'), env, home)
        assert after != before

    def test_missing_ref_yields_empty_not_a_bogus_digest(self, tmp_path):
        home, env = self._fake_box(tmp_path, 'FROM python:3.12-slim\n')
        assert self._sh(_build_hash_at_ref_shell_cmd('no-such-ref'), env, home) == ''


class TestDockerBuildLineSummary:
    def test_buildkit_run_line(self):
        assert 'pip3 install' in (
            _docker_build_line_summary('#12 [8/20] RUN pip3 install cryptography') or ''
        )

    def test_buildkit_timed_setting_up(self):
        assert 'Setting up' in (
            _docker_build_line_summary('#15 3.2 Setting up nodejs (20.x)') or ''
        )

    def test_ignores_cached_and_blank(self):
        assert _docker_build_line_summary('#5 CACHED') is None
        assert _docker_build_line_summary('') is None
        assert _docker_build_line_summary('   ') is None


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


# --- Pre-built box image (GHCR) --------------------------------------------


class _R:
    """Stand-in for subprocess.CompletedProcess."""

    def __init__(self, returncode=0, stdout='', stderr=''):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


class _FakeSSH:
    """Fake for the `run_ssh_command_with_output` closure.

    The real one is defined inside `_update_logic` and cannot be patched,
    which is why `_acquire_box_image` takes the runner as an argument.
    """

    def __init__(self, *, pull=None, inspect=None):
        self.calls = []
        self.pull = _R(0) if pull is None else pull
        self.inspect = _R(0, 'v0.37.2') if inspect is None else inspect

    def __call__(self, cmd, timeout_secs=None):
        self.calls.append(cmd)
        if 'docker pull' in cmd:
            if isinstance(self.pull, BaseException):
                raise self.pull
            return self.pull
        if 'docker image inspect' in cmd:
            if isinstance(self.inspect, BaseException):
                raise self.inspect
            return self.inspect
        if 'docker rmi' in cmd:
            return _R(0)
        raise AssertionError(f'unexpected command: {cmd}')

    @property
    def pulled_ref(self):
        return [c for c in self.calls if 'docker pull' in c]

    @property
    def discarded(self):
        return [c for c in self.calls if 'docker rmi' in c]


class _FakeResp:
    def __init__(self, status_code=200, payload=None, headers=None):
        self.status_code = status_code
        self._payload = payload if payload is not None else {}
        self.headers = headers if headers is not None else {}

    def json(self):
        return self._payload


class _FakeHTTP:
    def __init__(self, token=None, manifest=None):
        self.token = token if token is not None else _FakeResp(200, {'token': 't0k'})
        self.manifest = manifest if manifest is not None else _FakeResp(
            200, headers={'Docker-Content-Digest': f'sha256:{"c" * 64}'})
        self.get_calls = []
        self.head_calls = []

    def get(self, url, params=None, timeout=None, headers=None):
        self.get_calls.append((url, params, headers))
        if isinstance(self.token, BaseException):
            raise self.token
        return self.token

    def head(self, url, headers=None, timeout=None):
        self.head_calls.append((url, headers))
        if isinstance(self.manifest, BaseException):
            raise self.manifest
        return self.manifest


REF = 'ghcr.io/lagerdata/lager-box:v0.37.2'
DIGEST = f'sha256:{"c" * 64}'


class TestBoxImageRef:
    def test_release_tag_maps_to_ghcr(self):
        assert _box_image_ref_for_version('v0.37.2') == REF
        assert _box_image_ref_for_version('0.37.2') == REF

    def test_prerelease_suffix_accepted(self):
        assert _box_image_ref_for_version('v0.37.2-rc1') == (
            'ghcr.io/lagerdata/lager-box:v0.37.2-rc1'
        )

    def test_branches_and_empty_are_not_published(self):
        for bad in ('main', 'staging', 'de/some-branch', '', None):
            assert _box_image_ref_for_version(bad) is None

    def test_matches_resolve_version_ref_on_what_is_a_tag(self):
        # The two must agree: an image may exist only where a tag exists.
        for v in ('v0.37.2', '0.37.2', 'v1.2.3-beta2', 'main', 'de/x', '0.1'):
            checkout, _reset, _fetch = resolve_version_ref(v)
            is_tag = checkout.startswith('v') and checkout[1:2].isdigit()
            assert bool(_box_image_ref_for_version(v)) == is_tag, v


class TestBoxImagePullEnabled:
    def test_off_by_default(self):
        assert _box_image_pull_enabled(False, env={}) is False

    def test_flag_enables(self):
        assert _box_image_pull_enabled(True, env={}) is True

    def test_env_enables(self):
        for val in ('1', 'true', 'TRUE', 'yes'):
            assert _box_image_pull_enabled(False, env={'LAGER_BOX_IMAGE_PULL': val})

    def test_env_junk_does_not_enable(self):
        for val in ('0', 'no', '', 'maybe'):
            assert not _box_image_pull_enabled(False, env={'LAGER_BOX_IMAGE_PULL': val})


class TestDockerCommandBuilders:
    def test_pull_uses_digest_not_tag(self):
        cmd = _docker_pull_cmd(REF, DIGEST)
        # Pulling the tag would reintroduce the mutable-reference window the
        # digest resolution exists to close.
        assert f'lager-box@{DIGEST}' in cmd
        assert ':v0.37.2' not in cmd

    def test_pull_pins_platform_to_the_box(self):
        cmd = _docker_pull_cmd(REF, DIGEST)
        # Without this an arm64 box silently tags an amd64 image it cannot
        # execute -- a box that is down, not merely slow.
        assert '--platform "linux/$(dpkg --print-architecture 2>/dev/null || uname -m)"' in cmd

    def test_tag_and_untag_quote_refs(self):
        assert _docker_tag_cmd(_digest_ref(REF, DIGEST)).endswith(' lager')
        nasty = _docker_untag_cmd('ghcr.io/x/y:v1;rm -rf /')
        assert "'ghcr.io/x/y:v1;rm -rf /'" in nasty
        assert nasty.count('||') == 1

    def test_inspect_reads_the_oci_version_label(self):
        cmd = _docker_inspect_label_cmd(_digest_ref(REF, DIGEST))
        assert 'org.opencontainers.image.version' in cmd
        assert 'docker image inspect --format' in cmd


class TestResolveImageDigest:
    def test_happy_path(self):
        http = _FakeHTTP()
        digest, reason = _resolve_image_digest(REF, http=http)
        assert digest == DIGEST
        assert reason == 'ok'

    def test_requests_anonymous_pull_scope(self):
        http = _FakeHTTP()
        _resolve_image_digest(REF, http=http)
        _url, params, _h = http.get_calls[0]
        # Anonymous on purpose: authenticating as the operator would make a
        # package boxes cannot read look fine in testing.
        assert params['scope'] == 'repository:lagerdata/lager-box:pull'

    def test_accepts_oci_manifest_types(self):
        http = _FakeHTTP()
        _resolve_image_digest(REF, http=http)
        _url, headers = http.head_calls[0]
        assert 'application/vnd.oci.image.manifest.v1+json' in headers['Accept']
        assert headers['Authorization'] == 'Bearer t0k'

    def test_unpublished_tag_is_a_clean_miss(self):
        http = _FakeHTTP(manifest=_FakeResp(404))
        digest, reason = _resolve_image_digest(REF, http=http)
        assert digest is None
        assert reason == 'not published'

    def test_registry_unreachable_is_a_clean_miss(self):
        import requests as _rq
        http = _FakeHTTP(token=_rq.exceptions.ConnectTimeout('boom'))
        digest, reason = _resolve_image_digest(REF, http=http)
        assert digest is None
        assert 'unreachable' in reason

    def test_token_rejection_is_a_clean_miss(self):
        http = _FakeHTTP(token=_FakeResp(403, {}))
        digest, reason = _resolve_image_digest(REF, http=http)
        assert digest is None
        assert '403' in reason

    def test_missing_content_digest_header_is_a_miss(self):
        http = _FakeHTTP(manifest=_FakeResp(200, headers={}))
        digest, reason = _resolve_image_digest(REF, http=http)
        assert digest is None
        assert 'no content digest' in reason

    def test_uses_short_timeouts(self):
        # A black-holed ghcr.io must not cost the update minutes before it
        # falls back to the build that always worked.
        http = _FakeHTTP()
        _resolve_image_digest(REF, http=http)
        assert max(_REGISTRY_TIMEOUT) <= 10


class TestAcquireBoxImage:
    def test_happy_path_reports_ok(self):
        ssh = _FakeSSH()
        res = _acquire_box_image(ssh, image_ref=REF, digest=DIGEST,
                                 expect_version='v0.37.2')
        assert res.ok is True
        assert res.digest == DIGEST

    def test_does_not_tag_lager(self):
        # Tagging happens only after the containers are stopped; if this
        # function tagged, a failure downstream would have already replaced
        # the image a running box depends on.
        ssh = _FakeSSH()
        _acquire_box_image(ssh, image_ref=REF, digest=DIGEST,
                           expect_version='v0.37.2')
        assert not any('docker tag' in c for c in ssh.calls)

    def test_version_label_mismatch_is_rejected_and_discarded(self):
        ssh = _FakeSSH(inspect=_R(0, 'v0.36.0'))
        res = _acquire_box_image(ssh, image_ref=REF, digest=DIGEST,
                                 expect_version='v0.37.2')
        assert res.ok is False
        assert 'claims v0.36.0' in res.reason
        assert ssh.discarded, 'a rejected ~1 GB image must not be left on the box'

    def test_unlabelled_image_is_rejected(self):
        # Images published before the labelling workflow landed carry no
        # provenance at all; no evidence is not good evidence.
        for out in ('', '<no value>'):
            ssh = _FakeSSH(inspect=_R(0, out))
            res = _acquire_box_image(ssh, image_ref=REF, digest=DIGEST,
                                     expect_version='v0.37.2')
            assert res.ok is False
            assert 'no version label' in res.reason
            assert ssh.discarded

    def test_pull_timeout_is_a_miss_not_a_crash(self):
        # subprocess.run RAISES on timeout rather than returning.
        ssh = _FakeSSH(pull=subprocess.TimeoutExpired(cmd='docker pull', timeout=300))
        res = _acquire_box_image(ssh, image_ref=REF, digest=DIGEST,
                                 expect_version='v0.37.2')
        assert res.ok is False
        assert 'exceeded' in res.reason

    def test_manifest_unknown_is_a_miss(self):
        ssh = _FakeSSH(pull=_R(1, stderr='manifest unknown'))
        res = _acquire_box_image(ssh, image_ref=REF, digest=DIGEST,
                                 expect_version='v0.37.2')
        assert res.ok is False
        assert res.reason == 'not published'

    def test_wrong_architecture_is_a_miss(self):
        ssh = _FakeSSH(pull=_R(1, stderr='image ... does not match the specified platform'))
        res = _acquire_box_image(ssh, image_ref=REF, digest=DIGEST,
                                 expect_version='v0.37.2')
        assert res.ok is False
        assert 'architecture' in res.reason

    def test_auth_failure_is_a_miss(self):
        ssh = _FakeSSH(pull=_R(1, stderr='denied: denied'))
        res = _acquire_box_image(ssh, image_ref=REF, digest=DIGEST,
                                 expect_version='v0.37.2')
        assert res.ok is False
        assert 'denied access' in res.reason

    def test_disk_full_is_a_miss(self):
        ssh = _FakeSSH(pull=_R(1, stderr='write /var/lib/docker: no space left on device'))
        res = _acquire_box_image(ssh, image_ref=REF, digest=DIGEST,
                                 expect_version='v0.37.2')
        assert res.ok is False
        assert 'no space left' in res.reason

    def test_unreachable_registry_from_the_box_is_a_miss(self):
        ssh = _FakeSSH(pull=_R(1, stderr='dial tcp: lookup ghcr.io: no such host'))
        res = _acquire_box_image(ssh, image_ref=REF, digest=DIGEST,
                                 expect_version='v0.37.2')
        assert res.ok is False
        assert 'cannot reach the registry' in res.reason

    def test_every_failure_mode_falls_back_rather_than_raising(self):
        failures = [
            _R(1, stderr='manifest unknown'),
            _R(1, stderr='denied'),
            _R(1, stderr='no space left on device'),
            _R(125, stderr='something nobody has seen before'),
            subprocess.TimeoutExpired(cmd='docker pull', timeout=1),
            subprocess.SubprocessError('transport died'),
        ]
        for f in failures:
            ssh = _FakeSSH(pull=f)
            res = _acquire_box_image(ssh, image_ref=REF, digest=DIGEST,
                                     expect_version='v0.37.2')
            assert res.ok is False and res.reason


class TestPullMissReporting:
    def test_config_problems_are_actionable(self):
        for reason in ('registry denied access (is the package still public?)',
                       'image carries no version label',
                       'image claims v0.1.0, expected v0.37.2'):
            assert _pull_miss_is_actionable(reason)

    def test_benign_misses_stay_quiet(self):
        # These are self-correcting and would otherwise fire on every update
        # at a restricted site until people learned to ignore the message.
        for reason in ('not published',
                       'box cannot reach the registry',
                       'registry unreachable (ConnectTimeout)',
                       'pull exceeded 300s'):
            assert not _pull_miss_is_actionable(reason)
