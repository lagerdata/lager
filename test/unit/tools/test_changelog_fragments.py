# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0
"""Guard tools/changelog_fragments.py.

Changelog entries are one file per change under changelog.d/, so two branches
never edit the same lines. These pin what the PR gate (--check) rejects and
what the release (--assemble) writes.
"""

import importlib.util
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]


def _load():
    """Import the tool by path: tools/ is a script directory, not a package."""
    path = REPO_ROOT / 'tools' / 'changelog_fragments.py'
    spec = importlib.util.spec_from_file_location('changelog_fragments', path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


tool = _load()

EMPTY = """\
# Changelog

Intro.

## [Unreleased]

<!-- Keep this heading, and keep it empty.
     More comment. -->

## [1.0.0] - 2026-01-01

### Fixed

- An old fix.
"""


@pytest.fixture
def repo(tmp_path, monkeypatch):
    frag_dir = tmp_path / 'changelog.d'
    frag_dir.mkdir()
    (frag_dir / 'README.md').write_text('# not a fragment\n')
    changelog = tmp_path / 'CHANGELOG.md'
    changelog.write_text(EMPTY)
    monkeypatch.setattr(tool, 'REPO_ROOT', tmp_path)
    monkeypatch.setattr(tool, 'CHANGELOG', changelog)
    monkeypatch.setattr(tool, 'FRAGMENT_DIR', frag_dir)

    class Repo:
        dir = frag_dir

        def __init__(self):
            self.changelog = changelog

        def __call__(self, name, body):
            (frag_dir / name).write_text(body)
    return Repo()


class TestCheck:

    def test_no_fragments_and_empty_unreleased_passes(self, repo):
        assert tool.main(['--check']) == 0

    def test_a_valid_fragment_passes(self, repo):
        repo('uart-timeout.fixed.md', '- `lager uart` stops at the limit.\n')
        assert tool.main(['--check']) == 0

    @pytest.mark.parametrize('name', [
        'uart-timeout.bugfix.md',   # unknown type
        'uart-timeout.md',          # no type
        'Uart-Timeout.fixed.md',    # capitals
        'uart timeout.fixed.md',    # space
        'uart-timeout.fixed.txt',   # extension
    ])
    def test_a_bad_name_fails(self, repo, name):
        repo(name, '- An entry.\n')
        assert tool.main(['--check']) == 1

    def test_an_empty_fragment_fails(self, repo):
        repo('x.fixed.md', '\n\n')
        assert tool.main(['--check']) == 1

    def test_a_fragment_that_is_not_a_bullet_fails(self, repo):
        repo('x.fixed.md', 'Fixed the thing.\n')
        assert tool.main(['--check']) == 1

    def test_a_heading_in_a_fragment_fails(self, repo):
        repo('x.fixed.md', '- An entry.\n\n### Fixed\n')
        assert tool.main(['--check']) == 1

    def test_a_conflict_marker_fails(self, repo):
        repo('x.fixed.md', '- An entry.\n<<<<<<< HEAD\n- Other.\n')
        assert tool.main(['--check']) == 1

    def test_an_entry_written_into_unreleased_fails(self, repo):
        repo.changelog.write_text(EMPTY.replace(
            '## [1.0.0]', '### Fixed\n\n- Written in the old place.\n\n## [1.0.0]'))
        assert tool.main(['--check']) == 1

    def test_the_shipped_tree_passes(self):
        """The real changelog.d/ and CHANGELOG.md, as CI sees them."""
        assert _load().main(['--check']) == 0


class TestAssemble:

    def test_writes_sections_in_order_and_deletes_fragments(self, repo, capsys):
        repo('b-fix.fixed.md', '- Fix B.\n')
        repo('a-fix.fixed.md', '- Fix A.\n')
        repo('new-cmd.added.md', '- **New command.** It does a thing.\n')
        repo('sec.security.md', '- A security fix.\n')

        assert tool.main(['--assemble', '1.1.0', '--date', '2026-02-02']) == 0

        text = repo.changelog.read_text()
        assert text.index('## [Unreleased]') < text.index('## [1.1.0] - 2026-02-02') \
            < text.index('## [1.0.0]')
        new = text[text.index('## [1.1.0]'):text.index('## [1.0.0]')]
        assert new.index('### Added') < new.index('### Fixed') < new.index('### Security')
        assert new.index('Fix A.') < new.index('Fix B.')
        # The Unreleased section and its comment survive, still empty.
        assert tool.unreleased_entries(text.splitlines()) == []
        assert 'Keep this heading' in text
        assert sorted(p.name for p in repo.dir.iterdir()) == ['README.md']
        assert 'changelog.d/a-fix.fixed.md' in capsys.readouterr().out

    def test_one_blank_line_between_sections(self, repo):
        repo('x.fixed.md', '- Fix.\n')
        tool.main(['--assemble', '1.1.0', '--date', '2026-02-02'])
        text = repo.changelog.read_text()
        assert 'More comment. -->\n\n## [1.1.0] - 2026-02-02\n\n### Fixed\n\n- Fix.\n\n## [1.0.0]' \
            in text

    def test_refuses_with_no_fragments(self, repo):
        before = repo.changelog.read_text()
        assert tool.main(['--assemble', '1.1.0']) == 1
        assert repo.changelog.read_text() == before

    def test_refuses_a_version_already_present(self, repo):
        repo('x.fixed.md', '- Fix.\n')
        assert tool.main(['--assemble', '1.0.0']) == 1
        assert (repo.dir / 'x.fixed.md').exists()

    def test_refuses_a_bad_version(self, repo):
        repo('x.fixed.md', '- Fix.\n')
        assert tool.main(['--assemble', 'v1.1']) == 1

    def test_refuses_when_check_fails(self, repo):
        repo('x.bugfix.md', '- Fix.\n')
        before = repo.changelog.read_text()
        assert tool.main(['--assemble', '1.1.0']) == 1
        assert repo.changelog.read_text() == before

    def test_a_second_run_finds_nothing_to_do(self, repo):
        repo('x.fixed.md', '- Fix.\n')
        assert tool.main(['--assemble', '1.1.0', '--date', '2026-02-02']) == 0
        after = repo.changelog.read_text()
        assert tool.main(['--assemble', '1.1.0', '--date', '2026-02-02']) == 1
        assert repo.changelog.read_text() == after


def test_preview_writes_nothing(repo, capsys):
    repo('x.fixed.md', '- Fix.\n')
    before = repo.changelog.read_text()
    assert tool.main(['--preview']) == 0
    assert '### Fixed\n\n- Fix.' in capsys.readouterr().out
    assert repo.changelog.read_text() == before
    assert (repo.dir / 'x.fixed.md').exists()
