# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0
"""
Unit tests for the run-record ownership block in box/start_box.sh.

The shell under test is extracted verbatim between its
``# --- BEGIN run-record ownership`` / ``# --- END ...`` sentinels, as
test_secret_file_ownership.py does, so these exercise the shipped code.

The field case: a collector turned on collected mode by hand, creating
/etc/lager/run_records with sudo before Lager ever ran a script. The directory
belonged to root, the container (uid 33) could not create open/ in it, and a
collected box refuses every run it cannot record -- so the box stopped running
scripts at all. The repair is the root-owned helper `lager install` grants by
exact path, never a `sudo chown -R`, which the sudoers contract forbids.

Ownership cannot be changed in a unit test, so the container uid is injected
via ``LAGER_CONTAINER_UID``: our own uid models "already the container's",
another uid models "owned by someone else". A fake ``sudo`` on PATH records
what would have run.
"""

import os
import pathlib
import shlex
import subprocess

import pytest

START_BOX = pathlib.Path(__file__).resolve().parents[3] / "box" / "start_box.sh"

OTHER_UID = 4242  # never this test process's uid
HELPER = "/usr/local/lib/lager/etc_lager_perms.sh"


def _extract(topic):
    """Return the shell between the BEGIN/END sentinels naming `topic`."""
    begin, end = f"# --- BEGIN {topic}", f"# --- END {topic}"
    body, inside, seen = [], False, False
    for line in START_BOX.read_text().splitlines():
        if line.startswith(begin):
            inside, seen = True, True
            continue
        if line.startswith(end):
            inside = False
            continue
        if inside:
            body.append(line)
    assert seen, f"sentinel {begin!r} not found in {START_BOX}"
    assert body, f"no shell extracted for {topic!r}"
    return "\n".join(body)


OWNERSHIP_SH = _extract("run-record ownership")


@pytest.fixture
def box(tmp_path):
    """A records root in a temp dir, and a `sudo` that only records.

    ``repairs`` makes the fake sudo stand in for a helper that worked: it
    removes the foreign tree, which is all the block can observe of a repair.
    """

    class Box:
        def __init__(self):
            self.root = tmp_path / "run_records"
            self.bin = tmp_path / "bin"
            self.bin.mkdir()
            self.sudo_log = tmp_path / "sudo.log"

        def run(self, container_uid, repairs=False):
            repair = f"rm -rf {shlex.quote(str(self.root))}\n" if repairs else ""
            sudo = self.bin / "sudo"
            sudo.write_text(
                "#!/usr/bin/env bash\n"
                f'printf "%s\\n" "$*" >> {shlex.quote(str(self.sudo_log))}\n'
                f"{repair}"
                f"exit {0 if repairs else 1}\n"
            )
            sudo.chmod(0o755)
            script = "\n".join([
                "set -e",  # start_box.sh runs under `set -e`; nothing may abort
                f"export PATH={shlex.quote(str(self.bin))}:$PATH",
                f"export LAGER_RUN_RECORDS_ROOT={shlex.quote(str(self.root))}",
                f"export LAGER_CONTAINER_UID={container_uid}",
                OWNERSHIP_SH,
            ])
            return subprocess.run(["bash", "-c", script], capture_output=True, text=True)

        def sudo_calls(self):
            if not self.sudo_log.exists():
                return []
            return [ln for ln in self.sudo_log.read_text().splitlines() if ln.strip()]

    return Box()


def test_no_records_root_is_left_for_lager_to_create(box):
    result = box.run(OTHER_UID)
    assert result.returncode == 0, result.stderr
    assert box.sudo_calls() == []
    assert not box.root.exists()


def test_a_healthy_root_never_reaches_sudo(box):
    (box.root / "open").mkdir(parents=True)
    (box.root / "policy.json").write_text('{"mode": "collected"}')
    result = box.run(os.getuid())
    assert result.returncode == 0, result.stderr
    assert box.sudo_calls() == []
    assert "WARNING" not in result.stdout


def test_a_foreign_root_runs_the_granted_helper_with_no_arguments(box):
    box.root.mkdir()
    (box.root / "policy.json").write_text('{"mode": "collected"}')
    result = box.run(OTHER_UID, repairs=True)
    assert result.returncode == 0, result.stderr
    # Exactly the grant: `NOPASSWD: <helper>` with no arguments. Anything else
    # (a chown -R in particular) would prompt or be refused on a locked box.
    assert box.sudo_calls() == [f"-n {HELPER}"]
    assert "WARNING" not in result.stdout


def test_a_root_the_helper_cannot_fix_is_reported_with_the_fix(box):
    box.root.mkdir()
    result = box.run(OTHER_UID)
    assert result.returncode == 0, result.stderr
    assert box.sudo_calls() == [f"-n {HELPER}"]
    assert f"cannot write {box.root}" in result.stdout
    assert f"sudo chown -R {OTHER_UID}:{OTHER_UID} {box.root}" in result.stdout


def test_the_block_never_asks_sudo_for_a_recursive_chown():
    for line in OWNERSHIP_SH.splitlines():
        code = line.split("#", 1)[0]
        if "sudo" in code and "echo" not in code:
            assert "chown" not in code, line
