# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0
"""Guard the gate that guards test/COVERAGE.md.

`tools/check_coverage_counts.py` decides whether the counts in COVERAGE.md are
a defect, and `--fix` rewrites the file in place. Both of those went untested,
and both were wrong off Linux: seven `test/unit/box/` tests are gated on
`/proc` and `flock(1)`, so on macOS the suite reports 1547 passed / 7 skipped
where a runner reports 1554 / 0. `--fix` wrote the local pair into the table
twice on one branch, each time producing a file that fails the CI job the tool
exists to feed.

These tests pin the rule that resolves it -- a moved skip count off Linux means
the machine differs, not the tree -- and the anchored summary parse that
`FORCE_COLOR` used to break.
"""

import importlib.util
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]


def _load():
    """Import the checker by path: tools/ is a script directory, not a package."""
    path = REPO_ROOT / 'tools' / 'check_coverage_counts.py'
    spec = importlib.util.spec_from_file_location('check_coverage_counts', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


checker = _load()


def counts(passed, skipped=0, xfailed=0):
    return {'passed': passed, 'skipped': skipped, 'xfailed': xfailed}


class TestClassify:
    """The three verdicts a row can get."""

    def test_identical_counts_are_ok_on_every_platform(self):
        row = counts(1554)
        assert checker.classify(row, dict(row), on_linux=True) == 'ok'
        assert checker.classify(row, dict(row), on_linux=False) == 'ok'

    def test_moved_skips_off_linux_are_platform_gated_not_drift(self):
        # The real case: box claims 1554/0, macOS runs 1547/7.
        verdict = checker.classify(counts(1554), counts(1547, skipped=7),
                                   on_linux=False)
        assert verdict == 'platform'

    def test_the_same_divergence_on_linux_is_drift(self):
        # On the platform the table describes there is nothing to excuse.
        verdict = checker.classify(counts(1554), counts(1547, skipped=7),
                                   on_linux=True)
        assert verdict == 'drift'

    def test_a_changed_passed_count_alone_is_drift_off_linux(self):
        # Skips unchanged, so nothing platform-gated can be hiding in it: a
        # developer on macOS still gets told their new tests are uncounted.
        verdict = checker.classify(counts(1200), counts(1211), on_linux=False)
        assert verdict == 'drift'

    def test_xfailed_divergence_is_drift_off_linux(self):
        verdict = checker.classify(counts(1200, xfailed=2),
                                   counts(1200, xfailed=3), on_linux=False)
        assert verdict == 'drift'


class TestGatedTotal:
    """What the total is allowed to assume."""

    def test_platform_rows_contribute_the_table_value(self):
        claimed = {'box': counts(1554), 'cli': counts(1220, xfailed=2)}
        actual = {'box': counts(1547, skipped=7), 'cli': counts(1220, xfailed=2)}
        verdicts = {'box': 'platform', 'cli': 'ok'}
        # 1554 + 1220 -- the machine's 1547 would understate the gate by 7 and
        # fail a total the developer has no way to satisfy locally.
        assert checker.gated_total(claimed, actual, verdicts) == 2774

    def test_drift_rows_contribute_the_measured_value(self):
        claimed = {'cli': counts(1200)}
        actual = {'cli': counts(1211)}
        assert checker.gated_total(claimed, actual, {'cli': 'drift'}) == 1211

    def test_all_ok_totals_the_measured_values(self):
        claimed = {'a': counts(10), 'b': counts(5)}
        assert checker.gated_total(claimed, claimed, {'a': 'ok', 'b': 'ok'}) == 15


class TestSummaryParsing:
    """The summary line is matched anchored, so colour must be off at the source."""

    def test_pytest_is_invoked_with_colour_disabled(self, monkeypatch):
        seen = {}

        class Result:
            returncode = 0
            stdout = '1220 passed, 2 xfailed in 16.51s\n'

        def fake_run(cmd, **kwargs):
            seen['cmd'] = cmd
            return Result()

        monkeypatch.setattr(checker.subprocess, 'run', fake_run)
        checker.run_suite(['test/unit/cli/'])
        # FORCE_COLOR in the environment makes pytest colourise a pipe, and the
        # escape prefix defeats the anchored parse below.
        assert '--color=no' in seen['cmd']

    @pytest.mark.parametrize('summary, expected', [
        ('1220 passed, 2 xfailed in 16.51s', counts(1220, xfailed=2)),
        ('1547 passed, 7 skipped in 54.55s', counts(1547, skipped=7)),
        ('105 passed in 3.20s', counts(105)),
        ('79 passed, 4 skipped, 2 warnings in 5.00s', counts(79, skipped=4)),
    ])
    def test_summary_lines_parse(self, monkeypatch, summary, expected):
        class Result:
            returncode = 0
            stdout = f'{summary}\n'

        monkeypatch.setattr(checker.subprocess, 'run',
                            lambda cmd, **kwargs: Result())
        assert checker.run_suite(['test/unit/cli/']) == expected

    def test_an_unparseable_summary_is_an_error_not_a_zero(self, monkeypatch):
        # Reporting 0 passed would let --fix wipe a row to nothing.
        class Result:
            returncode = 0
            stdout = 'no summary here\n'

        monkeypatch.setattr(checker.subprocess, 'run',
                            lambda cmd, **kwargs: Result())
        with pytest.raises(SystemExit) as excinfo:
            checker.run_suite(['test/unit/cli/'])
        assert excinfo.value.code == 2
