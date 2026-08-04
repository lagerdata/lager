# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0
"""
Unit tests for the secret-file ownership block in box/start_box.sh.

The shell under test is extracted verbatim between its
``# --- BEGIN secret-file ownership`` / ``# --- END ...`` sentinels, the same
way test_authorized_keys_sync.py extracts the key sync, so these exercise the
shipped code rather than a transcription of it.

The bug being pinned: mode 0600 grants the OWNER alone. Everything that reads
these files runs as uid 33 inside the container, so 0600 is only safe once uid
33 owns the file. On a box where the secrets had been copied in by hand the
owner was the host login user — and start_box.sh runs as that user, so its
``chmod 600`` SUCCEEDED and locked the runtime out of its own secrets. The
executor caught the PermissionError and injected an empty secret set, so
nothing failed loudly; the box simply stopped having secrets.

The old loop's diagnostics were exactly inverted: it warned when chmod FAILED
(the healthy case — the file already belongs to uid 33, which is why this user
cannot chmod it) and said nothing at all when chmod succeeded, which is the
case that creates the lockout.

Ownership is the one thing a unit test cannot actually change: chown to another
uid needs privilege. So the container uid is injected via ``LAGER_CONTAINER_UID``
— pointing it at our own uid models "already owned by the container", pointing
it elsewhere models "owned by someone else" — and a fake ``sudo`` on PATH
records the chown that would have run without ever invoking the real one.
"""

import os
import pathlib
import shlex
import subprocess

import pytest

START_BOX = pathlib.Path(__file__).resolve().parents[3] / "box" / "start_box.sh"

OTHER_UID = 4242  # never this test process's uid


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


OWNERSHIP_SH = _extract("secret-file ownership")


@pytest.fixture
def box(tmp_path):
    """A fake box: secret files in a temp dir, and a `sudo` that only records.

    The fake sudo is what keeps this hermetic. Without it a developer with a
    warm sudo timestamp would have the block genuinely chown their temp files
    to uid 4242, which both needs a password eventually and makes the result
    depend on who is running the suite.
    """

    class Box:
        def __init__(self):
            self.dir = tmp_path / "etc-lager"
            self.dir.mkdir()
            self.bin = tmp_path / "bin"
            self.bin.mkdir()
            self.sudo_log = tmp_path / "sudo.log"
            self.sudo_rc = 1  # no chown grant on this box, by default

        def write_fake_sudo(self):
            sudo = self.bin / "sudo"
            sudo.write_text(
                "#!/usr/bin/env bash\n"
                f'printf "%s\\n" "$*" >> {shlex.quote(str(self.sudo_log))}\n'
                f"exit {self.sudo_rc}\n"
            )
            sudo.chmod(0o755)

        def secret(self, name, mode=0o644, create=True):
            path = self.dir / name
            if create:
                path.write_text('{"API_KEY": "shh"}')
                path.chmod(mode)
            return path

        def run(self, files, container_uid=None):
            """Run the extracted block; returns the CompletedProcess."""
            self.write_fake_sudo()
            uid = os.getuid() if container_uid is None else container_uid
            script = "\n".join([
                "set -e",  # start_box.sh runs under `set -e`; nothing may abort
                f"export PATH={shlex.quote(str(self.bin))}:$PATH",
                f"export LAGER_SECRET_FILES={shlex.quote(' '.join(str(f) for f in files))}",
                f"export LAGER_CONTAINER_UID={uid}",
                OWNERSHIP_SH,
            ])
            return subprocess.run(["bash", "-c", script],
                                  capture_output=True, text=True)

        def sudo_calls(self):
            if not self.sudo_log.exists():
                return []
            return [ln for ln in self.sudo_log.read_text().splitlines() if ln.strip()]

    return Box()


def _mode(path):
    return path.stat().st_mode & 0o777


class TestModeTightening:
    def test_group_readable_secret_is_tightened(self, box):
        secret = box.secret("org_secrets.json", mode=0o644)
        result = box.run([secret])
        assert result.returncode == 0, result.stderr
        assert _mode(secret) == 0o600

    def test_world_readable_secret_is_tightened(self, box):
        secret = box.secret("secret_key", mode=0o666)
        box.run([secret])
        assert _mode(secret) == 0o600

    def test_already_correct_file_is_left_alone(self, box):
        secret = box.secret("org_secrets.json", mode=0o600)
        box.run([secret])
        assert _mode(secret) == 0o600


class TestOwnershipRepair:
    def test_chown_is_attempted_when_owner_is_wrong(self, box):
        """The repair the field failure needed: hand it back to the container
        user rather than only tightening the mode under the wrong owner."""
        secret = box.secret("org_secrets.json")
        box.run([secret], container_uid=OTHER_UID)
        assert box.sudo_calls() == [
            f"-n chown {OTHER_UID}:{OTHER_UID} {secret}"
        ]

    def test_no_chown_when_already_owned_by_container(self, box):
        """Default container_uid is our own uid, i.e. the file already belongs
        to the container user — there is nothing to repair."""
        secret = box.secret("org_secrets.json")
        box.run([secret])
        assert box.sudo_calls() == []


class TestLockoutWarning:
    def test_warns_when_0600_under_the_wrong_owner(self, box):
        """The state that broke a box: readable by its owner alone, and its
        owner is not the user the container runs as."""
        secret = box.secret("org_secrets.json", mode=0o600)
        result = box.run([secret], container_uid=OTHER_UID)
        assert "WARNING" in result.stdout
        assert "cannot read" in result.stdout
        assert str(secret) in result.stdout

    def test_warning_gives_the_exact_fix(self, box):
        secret = box.secret("org_secrets.json")
        result = box.run([secret], container_uid=OTHER_UID)
        assert (f"sudo chown {OTHER_UID}:{OTHER_UID} {secret} "
                f"&& sudo chmod 600 {secret}") in result.stdout

    def test_silent_when_the_container_owns_the_file(self, box):
        """The regression the old loop had backwards. A file owned by uid 33 at
        0600 is the healthy end state; the old code warned about exactly this,
        every boot, which is what taught operators to ignore the message."""
        secret = box.secret("org_secrets.json", mode=0o600)
        result = box.run([secret])
        assert "WARNING" not in result.stdout

    def test_no_warning_when_the_chown_succeeds(self, box):
        """With the grant in place the file is repaired, so there is nothing to
        warn about. Modelled by a fake sudo that reports success while the
        container uid is already ours."""
        box.sudo_rc = 0
        secret = box.secret("org_secrets.json", mode=0o600)
        result = box.run([secret])
        assert "WARNING" not in result.stdout


class TestRobustness:
    def test_missing_files_are_skipped_silently(self, box):
        absent = box.secret("org_secrets.json", create=False)
        result = box.run([absent])
        assert result.returncode == 0, result.stderr
        assert result.stdout.strip() == ""
        assert box.sudo_calls() == []

    def test_never_aborts_under_set_e(self, box):
        """start_box.sh runs under `set -e`, and this block runs before the
        container starts. A non-zero exit anywhere in it would stop the box
        from booting over a permissions warning."""
        present = box.secret("org_secrets.json", mode=0o600)
        absent = box.secret("secret_key", create=False)
        result = box.run([present, absent], container_uid=OTHER_UID)
        assert result.returncode == 0, result.stderr

    def test_handles_several_files(self, box):
        a = box.secret("org_secrets.json", mode=0o644)
        b = box.secret("secret_key", mode=0o640)
        result = box.run([a, b])
        assert result.returncode == 0, result.stderr
        assert _mode(a) == 0o600
        assert _mode(b) == 0o600

    def test_defaults_to_the_real_etc_lager_paths(self):
        """The env var exists for these tests; the shipped default must still
        be the real box paths."""
        assert '/etc/lager/org_secrets.json' in OWNERSHIP_SH
        assert '/etc/lager/secret_key' in OWNERSHIP_SH
        assert 'LAGER_CONTAINER_UID:-33' in OWNERSHIP_SH
