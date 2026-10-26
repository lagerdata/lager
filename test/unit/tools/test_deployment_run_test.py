# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0
"""
The deployment suite's `run_test` must report the attempt that failed.

It used to run each command with its output discarded and, on failure, run it
a second time to print something. A flaky command fails once and then
succeeds, so the log showed `[FAIL]` above a clean second run, and the error
that caused the failure was never printed anywhere. `lager ble scan` on a box
whose Bluetooth controller resets itself after a fault does exactly that.

These tests pull the real `run_test` and `ble_scan_summary` out of
deployment.sh and drive them with stub commands, so they test the script's
own text rather than a copy of it.
"""

import pathlib
import re
import shutil
import subprocess

import pytest

REPO_ROOT = pathlib.Path(__file__).resolve().parents[3]
SUITE = REPO_ROOT / "test" / "integration" / "infrastructure" / "deployment.sh"

pytestmark = pytest.mark.skipif(shutil.which("bash") is None, reason="needs bash")

# A made-up advertiser. If its name or address reaches the output, the scan
# summary leaked what a real run would leak into a public CI log.
ADVERTISER_NAME = "bench-sensor-01"
ADVERTISER_ADDR = "AA:BB:CC:DD:EE:01"


def _function(name):
    text = SUITE.read_text()
    match = re.search(rf"^{name}\(\) \{{\n.*?^\}}\n", text, re.S | re.M)
    assert match, f"{name}() not found in {SUITE}"
    return match.group(0)


def _stub(path, body):
    path.write_text("#!/bin/bash\n" + body)
    path.chmod(0o755)


def _run(tmp_path, calls):
    """Run `calls` after the real helpers; return the CompletedProcess."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    # macOS has no `timeout`; the helpers only need it to run its command.
    _stub(bin_dir / "timeout", 'shift\nexec "$@"\n')
    helpers = (_function("run_test") + _function("ble_scan_summary")).replace(
        "/tmp/deployment_test_debug.log", str(tmp_path / "debug.log"))
    script = (
        "GREEN= RED= YELLOW= NC=\n"
        "TOTAL_TESTS=0 PASSED_TESTS=0 FAILED_TESTS=0\n"
        "DUT=bench\n"
        f"{helpers}\n{calls}\n"
        'echo "passed=$PASSED_TESTS failed=$FAILED_TESTS"\n'
    )
    return subprocess.run(
        ["bash", "-c", script], capture_output=True, text=True,
        env={"PATH": f"{bin_dir}:/usr/bin:/bin"}, cwd=tmp_path,
    )


@pytest.fixture
def flaky(tmp_path):
    """A command that fails on its first run only, and counts its runs."""
    _stub(tmp_path / "flaky.sh",
          'n=$(( $(cat runs 2>/dev/null || echo 0) + 1 ))\n'
          'echo "$n" > runs\n'
          'if [ "$n" -eq 1 ]; then echo "first attempt: controller fault"; exit 1; fi\n'
          'echo "later attempt: all good"\n')
    return tmp_path / "flaky.sh"


class TestRunTest:
    def test_a_failure_prints_the_failed_attempts_output(self, tmp_path, flaky):
        res = _run(tmp_path, f'run_test "flaky" "{flaky}"')
        assert "[FAIL]" in res.stdout
        assert "first attempt: controller fault" in res.stdout
        assert "later attempt" not in res.stdout, (
            "a second run's clean output under [FAIL] is how the error was lost")

    def test_the_command_runs_exactly_once(self, tmp_path, flaky):
        _run(tmp_path, f'run_test "flaky" "{flaky}"')
        assert (tmp_path / "runs").read_text().strip() == "1"

    def test_a_failure_is_never_turned_into_a_pass(self, tmp_path, flaky):
        res = _run(tmp_path, f'run_test "flaky" "{flaky}"')
        assert "passed=0 failed=1" in res.stdout

    def test_a_pass_prints_no_output(self, tmp_path):
        res = _run(tmp_path, 'run_test "ok" "echo hidden-on-pass"')
        assert "[PASS]" in res.stdout
        assert "hidden-on-pass" not in res.stdout

    def test_an_expected_failure_still_passes(self, tmp_path):
        res = _run(tmp_path, 'run_test "refused" "false" false')
        assert "[PASS] (expected failure)" in res.stdout
        assert "passed=1 failed=0" in res.stdout


class TestBleScanSummary:
    def _lager(self, tmp_path, rc):
        _stub(tmp_path / "bin" / "lager",
              'echo "Scanning for BLE devices for 2.0 seconds..."\n'
              'echo "Found 2 device(s)"\n'
              f'echo "  {ADVERTISER_NAME}  {ADVERTISER_ADDR}  -61"\n'
              'echo "Error: BLE error: controller fault" >&2\n'
              f'exit {rc}\n')

    def test_a_failed_scan_shows_its_error_but_no_advertiser(self, tmp_path):
        (tmp_path / "bin").mkdir()
        self._lager(tmp_path, 1)
        res = _run(tmp_path, 'run_test "lager ble scan" "ble_scan_summary"')
        assert "[FAIL]" in res.stdout
        assert "Found 2 device(s)" in res.stdout
        assert "Error: BLE error: controller fault" in res.stdout
        assert "exit status 1" in res.stdout
        assert ADVERTISER_NAME not in res.stdout + res.stderr
        assert ADVERTISER_ADDR not in res.stdout + res.stderr

    def test_a_passing_scan_prints_nothing_about_advertisers(self, tmp_path):
        (tmp_path / "bin").mkdir()
        self._lager(tmp_path, 0)
        res = _run(tmp_path, 'run_test "lager ble scan" "ble_scan_summary"')
        assert "[PASS]" in res.stdout
        assert ADVERTISER_NAME not in res.stdout + res.stderr
        assert ADVERTISER_ADDR not in res.stdout + res.stderr
