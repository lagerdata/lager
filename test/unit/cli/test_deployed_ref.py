# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

"""Persisting WHICH ref produced the code on a box (issue #266).

`lager update --version main` left `/etc/lager/version` untouched, so
`lager hello` reported the release version and a box running a branch was
indistinguishable from one on the release tag by any means the CLI offered.
The guard in `write_box_version_file` is not the bug: a branch not yet bumped
past the last release declares the same `__version__` as the tag, so the file
content genuinely had not changed. The bug is that the file records a version
*number*, which carries no information about the ref that produced it.

The failure mode this enabled is the expensive one: someone runs a test
believing a box is on the release, and gets a green result for unreleased
code.
"""
import importlib
import inspect
import re
from pathlib import Path

import click
import pytest

from cli.core.utils import looks_like_release_tag
from cli.commands.box.hello import _ref_suffix

ROOT = Path(__file__).resolve().parents[3]


class TestLooksLikeReleaseTag:
    def test_release_tags(self):
        for ref in ('v0.41.0', '0.41.0', 'v1.2.3', 'v0.41.0-rc1',
                    'v1.2.3-beta2', 'v1.2.3-alpha', 'v1.2.3-preview1'):
            assert looks_like_release_tag(ref), ref

    def test_branches_and_shas_are_not_release_tags(self):
        for ref in ('main', 'staging', 'de/my-fix', 'mainline',
                    'v0.41', '0.41', 'release/v0.41.0',
                    'd209f020ad7a2a935b4f30d77d2375a24b1a9ba5'):
            assert not looks_like_release_tag(ref), ref

    def test_empty_is_not_a_release_tag(self):
        # A box that reports no ref must not be flagged as a release build.
        assert not looks_like_release_tag(None)
        assert not looks_like_release_tag('')

    def test_it_agrees_with_resolve_version_ref(self):
        """The predicate and update.py's richer parse must not drift.

        `resolve_version_ref` needs the captured version, not a verdict, so it
        keeps its own regex. Nothing structural stops the two disagreeing, and
        a disagreement would make a branch deploy read as a release -- exactly
        the bug this file is about. So pin them together here: whatever
        `resolve_version_ref` resolves to a `refs/tags/` fetch is a release
        tag, and whatever it resolves to `origin/<branch>` is not.
        """
        update = importlib.import_module('cli.commands.utility.update')
        for ref in ('v0.41.0', '0.41.0', 'v0.41.0-rc1', 'v1.2.3-beta2',
                    'main', 'staging', 'de/my-fix', 'v0.41'):
            _checkout, _reset, fetch = update.resolve_version_ref(ref)
            resolves_to_a_tag = fetch.startswith('refs/tags/')
            assert looks_like_release_tag(ref) == resolves_to_a_tag, (
                f'{ref}: predicate says {looks_like_release_tag(ref)}, '
                f'resolve_version_ref fetches {fetch!r}'
            )


class TestRefSuffix:
    def test_a_release_tag_is_shown_plainly(self):
        assert _ref_suffix('v0.41.0@d209f02') == ' (v0.41.0@d209f02)'

    def test_a_branch_is_flagged(self):
        out = click.unstyle(_ref_suffix('main@85c1b64'))
        assert 'main@85c1b64' in out
        assert 'not a release build' in out

    def test_a_feature_branch_is_flagged(self):
        out = click.unstyle(_ref_suffix('de/my-fix@abc1234'))
        assert 'not a release build' in out

    def test_an_older_box_reads_exactly_as_before(self):
        # A box predating /etc/lager/ref reports no ref. It must not gain a
        # blank parenthetical or a warning it cannot answer.
        assert _ref_suffix(None) == ''
        assert _ref_suffix('') == ''

    def test_the_flag_is_decided_on_the_ref_not_the_sha(self):
        # `<ref>@<sha>` -- only the ref half decides. A SHA containing digits
        # and dots is not a thing, but splitting on the wrong half would make
        # every ref read as a branch.
        assert 'not a release build' not in click.unstyle(_ref_suffix('v0.41.0@0.41.0'))


class TestHeadShaReaders:
    """Both writers decorate the ref with a short SHA. `main` alone is not
    reproducible once main moves, so the SHA is the half that makes the record
    useful after the fact.
    """

    def _fake_result(self, returncode, stdout):
        class R:
            pass
        r = R()
        r.returncode = returncode
        r.stdout = stdout
        return r

    def test_update_reader_extracts_the_sha(self):
        update = importlib.import_module('cli.commands.utility.update')
        runner = lambda cmd: self._fake_result(0, '85c1b64\n')
        assert update._read_box_head_sha(runner) == '85c1b64'

    def test_update_reader_tolerates_banner_noise(self):
        # Boxes print motd/sudo-lecture text on stdout; the probe parser
        # already assumes nothing about output cleanliness and neither does
        # this. A banner line must not become the recorded "SHA".
        update = importlib.import_module('cli.commands.utility.update')
        runner = lambda cmd: self._fake_result(
            0, 'Welcome to Ubuntu 22.04\nLast login: today\n85c1b64\n')
        assert update._read_box_head_sha(runner) == '85c1b64'

    def test_update_reader_returns_empty_on_failure(self):
        update = importlib.import_module('cli.commands.utility.update')
        runner = lambda cmd: self._fake_result(1, '')
        assert update._read_box_head_sha(runner) == ''

    def test_update_reader_returns_empty_when_nothing_looks_like_a_sha(self):
        update = importlib.import_module('cli.commands.utility.update')
        runner = lambda cmd: self._fake_result(0, 'fatal: not a git repository\n')
        assert update._read_box_head_sha(runner) == ''

    def test_it_does_not_reuse_the_pull_only_reset_output(self):
        """`git reset --hard` prints "HEAD is now at <hash>", but only on the
        pulled path. An update that was already up to date never produces it,
        and that is exactly when someone is checking what a box runs. The
        reader must be its own round-trip, not a parse of that line.
        """
        update = importlib.import_module('cli.commands.utility.update')
        src = inspect.getsource(update._read_box_head_sha)
        assert 'rev-parse' in src
        assert 'HEAD is now at' not in src.replace('"HEAD is now at <hash> <subject>"', '')


class TestRefIsWrittenOnTheAlreadyUpToDatePath:
    """`/etc/lager/ref` must be written when the box was ALREADY up to date.

    This is the path it matters most on. `_read_box_head_sha` says so itself:
    "an 'already up to date' run against a box whose ref file is missing or
    stale is exactly when someone is trying to find out what the box is
    running." That helper was written to serve both paths and then only ever
    called on one -- the sole `store_deployed_ref` call sat several hundred
    lines past this branch's `ctx.exit(0)`, so the file was never produced.

    Found on hardware: with CLI and box both on a branch, a re-run printed
    "already at version", wrote no ref file, and `lager hello` reported a bare
    version number. The documented way to confirm a branch deploy took is that
    `lager hello` names a ref -- so this reported FAILURE for a deploy that had
    in fact succeeded.

    /etc/lager/version had the identical bug on this identical branch and was
    fixed once already (see the comment above its reconciliation call). The
    two writes are pinned together here so a future ref-like file cannot be
    added to one path and forgotten on the other.
    """

    @staticmethod
    def _early_exit_block():
        """Source between the version-file write and this branch's exit."""
        src = (ROOT / 'cli' / 'commands' / 'utility' / 'update.py').read_text()
        start = src.index('if not write_box_version_file(_box_v):')
        end = src.index('ctx.exit(0)', start)
        return src[start:end]

    def test_the_ref_is_written_before_the_early_exit(self):
        assert 'store_deployed_ref' in self._early_exit_block(), (
            "store_deployed_ref is not called on the already-up-to-date path, "
            "so /etc/lager/ref is never written there"
        )

    def test_it_is_paired_with_the_version_file_write(self):
        block = self._early_exit_block()
        assert 'write_box_version_file' in block
        assert block.index('write_box_version_file') < block.index(
            'store_deployed_ref'), (
            "reconcile the version file first, then record the ref -- the "
            "order the successful path uses"
        )

    def test_a_failed_write_warns_rather_than_exiting(self):
        """Best-effort, like every other store_deployed_ref call site.

        Nothing gates on this file; failing the whole update over it would
        turn a cosmetic gap into an outage.
        """
        block = self._early_exit_block()
        after = block[block.index('store_deployed_ref'):]
        assert 'ctx.exit(1)' not in after
        assert 'Warning' in after or 'warning' in after


class TestRefIsASiblingFileNotAThirdVersionField:
    """The obvious fix -- a third `|` field in /etc/lager/version -- silently
    corrupts every reader. Four of them do `split('|', 1)` and would land the
    ref inside `updater_version`.
    """

    def test_no_reader_would_have_survived_a_third_field(self):
        readers = [
            'box/lager/box_http_server.py',
            'box/lager/python/service.py',
            'box/lager/mcp/config.py',
            'box/lager/mcp/engine/bench_loader.py',
        ]
        found = 0
        for rel in readers:
            src = (ROOT / rel).read_text()
            if "split('|', 1)" in src or 'split("|", 1)' in src:
                found += 1
        assert found >= 3, (
            'expected the version file to still be parsed with a 2-field '
            'split in several readers; if that changed, re-examine whether a '
            'third field is now safe'
        )

    def test_update_writes_a_sibling_file(self):
        src = (ROOT / 'cli' / 'commands' / 'utility' / 'update.py').read_text()
        assert 'def store_deployed_ref' in src
        assert '/etc/lager/ref' in src
        # The version file's own content must stay two fields.
        assert "f'{box_cli_version_value}|{cli_version}'" in src

    def test_install_writes_it_too(self):
        # install.py is a second, independently written version-file path.
        # A box installed from a branch has the same problem as one updated
        # to it.
        src = (ROOT / 'cli' / 'commands' / 'utility' / 'install.py').read_text()
        assert '/etc/lager/ref' in src

    def test_the_box_reports_it(self):
        src = (ROOT / 'box' / 'lager' / 'box_http_server.py').read_text()
        assert 'REF_FILE_PATH' in src
        assert "'ref': ref," in src

    def test_the_constant_exists(self):
        src = (ROOT / 'box' / 'lager' / 'constants.py').read_text()
        assert re.search(r'REF_FILE_PATH\s*=\s*"/etc/lager/ref"', src)


class _Result:
    def __init__(self, returncode, stdout='', stderr=''):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


class _FakeBox:
    """Plays the box's side of install's post-deploy ssh calls.

    Commands are matched by what they do rather than by exact text, except the
    build-hash command, which install must take verbatim from update.py.
    """

    def __init__(self, *, source_version='0.46.2', head_sha='85c1b64',
                 build_hash='f' * 64, fail_write=None, readback=None):
        update = importlib.import_module('cli.commands.utility.update')
        self._build_hash_cmd = update._build_hash_shell_cmd()
        self.source_version = source_version
        self.head_sha = head_sha
        self.build_hash = build_hash
        self.fail_write = fail_write
        self.readback = readback
        self.files = {}
        self.calls = []

    def __call__(self, cmd, timeout_secs=30):
        import shlex
        self.calls.append(cmd)
        if cmd.startswith('git -C ~/box show HEAD:cli/__init__.py'):
            if not self.source_version:
                return _Result(1)
            return _Result(0, f"__version__ = '{self.source_version}'\n")
        if 'rev-parse --short HEAD' in cmd:
            return _Result(0, f'{self.head_sha}\n')
        if cmd == self._build_hash_cmd:
            return _Result(0, f'{self.build_hash}\n' if self.build_hash else '')
        if cmd == 'cat /etc/lager/version':
            content = self.readback if self.readback is not None else self.files.get('version', '')
            return _Result(0, f'{content}\n')
        written = re.search(r'mv -f "\$tmp" /etc/lager/([a-z-]+)', cmd)
        if written:
            name = written.group(1)
            if name == self.fail_write:
                return _Result(1, '', 'mktemp: failed to create file via template\n')
            self.files[name] = shlex.split(cmd.split("printf '%s\\n' ", 1)[1])[0]
            return _Result(0)
        raise AssertionError(f'unexpected command sent to the box: {cmd}')


class TestInstallRecordsWhatItInstalled:
    """`lager install` records the installed version, or fails and says so.

    install used to write /etc/lager/version and /etc/lager/ref through sudo
    over `ssh -t`, discard the exit status, and print "Version X stored on box"
    either way. A write that failed left an older release's number in place,
    and `lager hello` reported it for a box that had just been installed.
    """

    @staticmethod
    def _install():
        return importlib.import_module('cli.commands.utility.install')

    def _record(self, box, version, cli_version='0.47.0'):
        return self._install()._record_install_state(
            box, 'lagerdata@10.0.0.1', version=version, cli_version=cli_version)

    def test_a_release_tag_records_its_own_number_and_ref(self):
        box = _FakeBox()
        box_version, ref, build_hash = self._record(box, '0.46.2')
        assert box_version == '0.46.2'
        assert box.files['version'] == '0.46.2|0.47.0'
        assert box.files['ref'] == ref == 'v0.46.2@85c1b64'
        assert box.files['build-hash'] == build_hash == 'f' * 64

    def test_a_branch_records_the_trees_version_not_the_clis(self):
        box = _FakeBox(source_version='0.46.2')
        box_version, ref, _ = self._record(box, 'main', cli_version='9.9.9')
        assert box_version == '0.46.2'
        assert box.files['version'] == '0.46.2|9.9.9'
        assert ref == 'main@85c1b64'

    def test_a_commit_sha_is_recorded_the_way_update_resolves_it(self):
        box = _FakeBox()
        sha = '5D84C68612384EED2854638C1E0941A4FF8B7893'
        _, ref, _ = self._record(box, sha)
        assert ref == f'{sha.lower()}@85c1b64'

    def test_an_unknown_tree_version_is_an_error_not_the_clis_version(self):
        from cli.errors import LagerError
        box = _FakeBox(source_version='')
        with pytest.raises(LagerError):
            self._record(box, 'main')
        assert box.files == {}, 'nothing may be written without a version'

    @pytest.mark.parametrize('name', ['version', 'ref', 'build-hash'])
    def test_a_failed_write_raises_naming_the_file(self, name):
        from cli.errors import LagerError
        box = _FakeBox(fail_write=name)
        with pytest.raises(LagerError) as excinfo:
            self._record(box, 'v0.46.2')
        assert f'/etc/lager/{name}' in str(excinfo.value)

    def test_a_version_file_that_reads_back_wrong_raises(self):
        from cli.errors import LagerError
        box = _FakeBox(readback='0.27.0|0.45.0')
        with pytest.raises(LagerError):
            self._record(box, 'v0.46.2')

    def test_no_build_hash_skips_only_that_file(self):
        box = _FakeBox(build_hash='')
        _, _, build_hash = self._record(box, 'v0.46.2')
        assert build_hash == ''
        assert set(box.files) == {'version', 'ref'}

    def test_nothing_it_sends_uses_sudo_or_a_fixed_tmp_path(self):
        box = _FakeBox()
        self._record(box, 'v0.46.2')
        for cmd in box.calls:
            assert 'sudo' not in cmd, cmd
            assert '/tmp/lager_' not in cmd, cmd

    def test_the_runner_never_allocates_a_tty(self, monkeypatch):
        install = self._install()
        seen = []

        def fake_run(argv, **kwargs):
            seen.append(argv)
            return _Result(0)

        monkeypatch.setattr(install.subprocess, 'run', fake_run)
        install._install_state_runner('lagerdata@10.0.0.1', ['-i', 'key'])('true')
        assert '-t' not in seen[0]
        assert 'BatchMode=yes' in seen[0]

    def test_the_old_unconditional_success_line_is_gone(self):
        src = (ROOT / 'cli' / 'commands' / 'utility' / 'install.py').read_text()
        assert 'stored on box' not in src
        assert '/tmp/lager_version_tmp' not in src
        assert '/tmp/lager_ref_tmp' not in src

    @pytest.fixture
    def run_install(self, monkeypatch):
        """Drive the real `lager install` command with the SSH side faked."""
        from contextlib import contextmanager, nullcontext
        from click.testing import CliRunner

        install = self._install()

        class _Session:
            def suspended(self):
                return nullcontext()

        @contextmanager
        def fake_lock(*args, **kwargs):
            yield _Session()

        monkeypatch.setattr(install, 'probe_box_identity',
                            lambda host, extra_args=(): (None, _Result(0)))
        monkeypatch.setattr(install, 'auto_lock_around_command', fake_lock)
        # The deploy script and the box-config sudoers precheck both report
        # success; the state writes go to the fake box.
        monkeypatch.setattr(install.subprocess, 'run', lambda *a, **k: _Result(0))

        def _run(box):
            monkeypatch.setattr(install, '_install_state_runner', lambda host, args: box)
            return CliRunner().invoke(
                install.install,
                ['--ip', '10.0.0.1', '--version', 'v0.46.2', '--yes'],
            )
        return _run

    def test_install_exits_non_zero_when_the_version_write_fails(self, run_install):
        result = run_install(_FakeBox(fail_write='version'))
        assert result.exit_code != 0
        assert '/etc/lager/version' in result.output
        assert 'Installation complete' not in result.output

    def test_install_exits_non_zero_when_the_version_reads_back_wrong(self, run_install):
        result = run_install(_FakeBox(readback='0.27.0|0.45.0'))
        assert result.exit_code != 0
        assert 'Installation complete' not in result.output

    def test_install_reports_the_recorded_version_on_success(self, run_install):
        box = _FakeBox()
        result = run_install(box)
        assert result.exit_code == 0, result.output
        assert 'Recorded version 0.46.2 (v0.46.2@85c1b64)' in result.output
        assert box.files['version'].startswith('0.46.2|')
