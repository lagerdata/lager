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
