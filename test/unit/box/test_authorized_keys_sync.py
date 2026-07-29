# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

"""Tests for the authorized_keys sync and single-instance guard in start_box.sh.

The shell under test is extracted verbatim from box/start_box.sh between its
`# --- BEGIN ... ---` / `# --- END ... ---` sentinels, so these tests exercise
the shipped code rather than a transcription of it. If the sentinels are
renamed or dropped, extraction fails loudly instead of silently testing
nothing.

The behaviour that matters here is the marker block. The sync rebuilds only the
region between its own two sentinels and preserves every line outside it — that
is what makes a deleted `.pub` revoke access without also revoking keys
installed by `lager ssh-setup` / ssh-copy-id / cloud-init, which never create a
`.pub` in the key directory.
"""

import shlex
import subprocess
import pathlib

import pytest

REPO_ROOT = pathlib.Path(__file__).resolve().parents[3]
START_BOX = REPO_ROOT / "box" / "start_box.sh"

KEY_A = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA keya"
KEY_B = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBB keyb"
# Installed by ssh-copy-id, never staged as a .pub — the key the old
# rebuild-from-directory design would have silently revoked.
KEY_USER = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIUUUUUUUUUUUUUUUUUUUUUUUUUUUUUUUUUUUUUUU chris@laptop"


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


SYNC_SH = _extract("authorized-keys sync")
GUARD_SH = _extract("single-instance guard")


@pytest.fixture
def box(tmp_path):
    """A fake box: a HOME, and a key directory the sync reads."""

    class Box:
        home = tmp_path / "home"
        keys_dir = tmp_path / "authorized_keys.d"

        def __init__(self):
            (self.home / ".ssh").mkdir(parents=True)
            self.keys_dir.mkdir()

        @property
        def auth_keys(self):
            return self.home / ".ssh" / "authorized_keys"

        def stage(self, name, key, trailing_newline=True):
            (self.keys_dir / f"{name}.pub").write_text(key + ("\n" if trailing_newline else ""))

        def unstage(self, name):
            (self.keys_dir / f"{name}.pub").unlink()

        def seed(self, text):
            self.auth_keys.write_text(text)

        def sync(self, passes=1):
            script = "\n".join([
                "set -e",
                f"export HOME={shlex.quote(str(self.home))}",
                f"export LAGER_AUTHORIZED_KEYS_D={shlex.quote(str(self.keys_dir))}",
                SYNC_SH,
                f"for _i in $(seq 1 {passes}); do _sync_authorized_keys; done",
            ])
            proc = subprocess.run(["bash", "-c", script], capture_output=True, text=True)
            assert proc.returncode == 0, f"sync failed: {proc.stderr}"
            return proc

        def lines(self):
            if not self.auth_keys.exists():
                return []
            return [ln for ln in self.auth_keys.read_text().splitlines() if ln.strip()]

        def keys(self):
            return [ln for ln in self.lines() if not ln.startswith("#")]

    return Box()


def test_staged_key_is_published(box):
    """The bootstrap path: a .pub dropped in the key directory reaches authorized_keys."""
    box.stage("control-plane", KEY_A)
    box.sync()
    assert KEY_A in box.keys()


def test_deleting_pub_revokes_the_key(box):
    """The headline change: removal is now possible at all."""
    box.stage("a", KEY_A)
    box.stage("b", KEY_B)
    box.sync()
    assert set(box.keys()) == {KEY_A, KEY_B}

    box.unstage("a")
    box.sync()

    assert KEY_A not in box.keys(), "deleting a .pub must revoke that key"
    assert KEY_B in box.keys(), "unrelated staged keys must survive"


def test_key_file_without_trailing_newline_does_not_concatenate(box):
    """A generated .pub with no trailing newline must not run into the next key."""
    box.stage("a", KEY_A, trailing_newline=False)
    box.stage("b", KEY_B)
    box.sync()

    assert KEY_A in box.keys()
    assert KEY_B in box.keys()
    assert not any(KEY_A in ln and KEY_B in ln for ln in box.lines()), \
        "two keys were concatenated onto one line"


def test_keys_installed_outside_the_block_are_preserved(box):
    """ssh-copy-id keys have no .pub; the sync must never revoke them.

    This is the regression guard for a rebuild-from-directory design, which
    would drop this key within one poll interval and lock the user out.
    """
    box.seed(KEY_USER + "\n")
    box.stage("control-plane", KEY_A)
    box.sync(passes=3)

    assert KEY_USER in box.keys(), "a key installed by ssh-copy-id was revoked"
    assert KEY_A in box.keys()


def test_repeated_passes_never_duplicate(box):
    """The old append-only sync raced itself into duplicate lines."""
    box.seed(KEY_USER + "\n")
    box.stage("a", KEY_A)
    box.sync(passes=10)

    assert box.keys().count(KEY_A) == 1
    assert box.keys().count(KEY_USER) == 1


def test_missing_key_directory_leaves_the_file_untouched(box):
    """A vanished key directory is ambiguous — never revoke on it."""
    box.seed(KEY_USER + "\n")
    box.stage("a", KEY_A)
    box.sync()
    before = box.auth_keys.read_text()

    for pub in box.keys_dir.iterdir():
        pub.unlink()
    box.keys_dir.rmdir()
    box.sync()

    assert box.auth_keys.read_text() == before, \
        "a missing key directory must not revoke anything"


def test_empty_key_directory_revokes_managed_keys_only(box):
    """An empty (but present) directory is unambiguous and does revoke."""
    box.seed(KEY_USER + "\n")
    box.stage("a", KEY_A)
    box.sync()
    assert KEY_A in box.keys()

    box.unstage("a")
    box.sync()

    assert KEY_A not in box.keys()
    assert KEY_USER in box.keys(), "an empty key dir must not empty the whole file"


def test_loose_copies_of_a_staged_key_are_adopted_and_collapsed(box):
    """Duplicates left by the old sync collapse into one managed line."""
    box.seed("\n".join([KEY_USER, KEY_A, KEY_A, KEY_A]) + "\n")
    box.stage("a", KEY_A)
    box.sync()

    assert box.keys().count(KEY_A) == 1, "historical duplicates were not collapsed"
    assert KEY_USER in box.keys()

    # Adopted, so it is now revocable.
    box.unstage("a")
    box.sync()
    assert KEY_A not in box.keys()


def test_another_managers_block_is_left_alone(box):
    """Distinct sentinel pairs are what let two key managers coexist.

    A manager that shared our sentinels would rebuild our region from its own
    source, and we would rebuild it back, every pass.
    """
    foreign = "\n".join([
        "# BEGIN OTHER MANAGED KEYS",
        KEY_USER,
        "# END OTHER MANAGED KEYS",
    ])
    box.seed(foreign + "\n")
    box.stage("a", KEY_A)
    box.sync(passes=3)

    text = box.auth_keys.read_text()
    assert foreign in text, "another manager's block was modified"
    assert KEY_A in box.keys()


def test_authorized_keys_is_not_world_readable(box):
    box.stage("a", KEY_A)
    box.sync()
    assert oct(box.auth_keys.stat().st_mode)[-3:] == "600"


def test_only_one_start_box_may_run(tmp_path):
    """Concurrent copies raced each other and accumulated across restarts."""
    if subprocess.run(["bash", "-c", "command -v flock"],
                      capture_output=True).returncode != 0:
        pytest.skip("flock(1) not available on this platform")

    lock = tmp_path / "start-box.lock"
    script = "\n".join([
        f"export LAGER_START_BOX_LOCK={shlex.quote(str(lock))}",
        GUARD_SH,
        "echo ACQUIRED",
        "sleep 5",
    ])

    holder = subprocess.Popen(["bash", "-c", script], stdout=subprocess.PIPE, text=True)
    try:
        assert holder.stdout.readline().strip() == "ACQUIRED"

        second = subprocess.run(["bash", "-c", script], capture_output=True, text=True,
                                timeout=15)
        assert second.returncode == 1, "a second start_box.sh was allowed to run"
        assert "already running" in second.stdout
        assert "ACQUIRED" not in second.stdout
    finally:
        holder.kill()
        holder.wait()

    # The lock is an fd, so it is released by exit — the next run must succeed.
    third = subprocess.run(["bash", "-c", script.replace("sleep 5", "true")],
                           capture_output=True, text=True, timeout=15)
    assert third.returncode == 0, "lock was not released after the holder exited"
    assert "ACQUIRED" in third.stdout
