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

import os
import shlex
import subprocess
import pathlib
import time
import uuid

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


def test_another_managers_block_keeps_keys_we_also_publish(box):
    """The case the test above cannot reach, and the one that actually bit.

    A peer publishing from this same key directory has every one of our staged
    keys inside its region. Adoption across the whole file therefore emptied
    that region on every pass — and the peer, following the same rule, emptied
    ours right back, so the two rewrote the file against each other forever.
    Adoption has to stop at another manager's marked block.
    """
    foreign = "\n".join([
        "# BEGIN OTHER MANAGED KEYS",
        KEY_A,
        "# END OTHER MANAGED KEYS",
    ])
    box.seed(foreign + "\n")
    box.stage("a", KEY_A)
    box.sync(passes=3)

    assert foreign in box.auth_keys.read_text(), \
        "a key we publish was deleted out of another manager's block"


def test_another_managers_block_survives_a_loose_duplicate_being_adopted(box):
    """Adoption still applies everywhere else in the file.

    Stopping at a peer's block must not turn into "stop adopting": a loose
    copy outside every block is still dropped, or revoking the key would leave
    it behind.
    """
    foreign = "\n".join([
        "# BEGIN OTHER MANAGED KEYS",
        KEY_A,
        "# END OTHER MANAGED KEYS",
    ])
    box.seed(KEY_A + "\n" + foreign + "\n")
    box.stage("a", KEY_A)
    box.sync()

    text = box.auth_keys.read_text()
    assert foreign in text, "the peer's block was modified"
    # One inside the peer's block, one inside ours — the loose copy is gone.
    assert box.keys().count(KEY_A) == 2


def test_unterminated_foreign_block_preserves_the_rest_of_the_file(box):
    """A truncated or hand-edited file must not lose keys.

    With no END to close it, everything after the marker is treated as the
    peer's and preserved — keeping keys is the safe direction here.
    """
    box.seed("\n".join(["# BEGIN OTHER MANAGED KEYS", KEY_A, KEY_USER]) + "\n")
    box.stage("a", KEY_A)
    box.sync()

    assert KEY_USER in box.keys()
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


PID_SH = _extract("ssh-sync pid file")


def _pid_script(pid_file, marker=None, legacy=None, lock_held=False, after=()):
    """The PID-file block under `set -e`, stopping the poller it starts.

    Every run gets its own marker and legacy pattern, so a test can never
    stop a real poller (or a real start_box.sh) on the machine running it.
    """
    token = uuid.uuid4().hex[:12]
    marker = marker or f"lager-ssh-sync-test-{token}"
    legacy = legacy or f"no-such-start-box-{token}[.]sh"
    return "\n".join([
        "set -e",
        "_sync_authorized_keys() { :; }",
        f"export LAGER_SSH_SYNC_PID_FILE={shlex.quote(str(pid_file))}",
        f"export LAGER_SSH_SYNC_MARKER={shlex.quote(marker)}",
        f"export LAGER_SSH_SYNC_LEGACY_NAME={shlex.quote(legacy)}",
        "_START_LOCK_HELD=1" if lock_held else ":",
        PID_SH,
        *after,
        'kill "$_SSH_SYNC_PID" 2>/dev/null || true',
        'echo "REACHED-THE-END"',
    ])


def _run_pid_block(pid_file, **kwargs):
    """Run the PID-file block under `set -e`, then stop the poller it starts."""
    return subprocess.run(["bash", "-c", _pid_script(pid_file, **kwargs)],
                          capture_output=True, text=True, timeout=30)


def _gone(proc, timeout=5):
    try:
        proc.wait(timeout=timeout)
        return True
    except subprocess.TimeoutExpired:
        return False


@pytest.fixture
def stray():
    """Start a long-lived stand-in process; kill whatever is left at the end."""
    procs = []

    def start(argv):
        proc = subprocess.Popen(argv)
        procs.append(proc)
        time.sleep(0.3)  # let exec -a / bash -c settle before anyone looks
        assert proc.poll() is None, "the stand-in exited on its own"
        return proc

    yield start
    for proc in procs:
        if proc.poll() is None:
            proc.kill()
            proc.wait()


# The PID file that ended an install as a second login user (#547). /tmp is
# sticky, so a file written by one login user cannot be removed by another.
# `rm -f` forgives a missing file, not EPERM -- and the block ran unguarded
# under `set -e`, after the old containers were already gone, so the install
# stopped there and left the box with no lager container.

def test_the_pid_path_is_per_user_by_default():
    """Two login users never contend for one path in the first place."""
    assert 'lager-ssh-sync-$(id -u).pid' in PID_SH
    assert '"/tmp/lager-ssh-sync.pid"' not in PID_SH


def test_a_pid_file_that_cannot_be_removed_does_not_end_the_script(tmp_path):
    """The reported failure: `rm` gets EPERM and `set -e` kills the run."""
    sticky = tmp_path / "sticky"
    sticky.mkdir()
    pid_file = sticky / "lager-ssh-sync.pid"
    pid_file.write_text("999999\n")
    # A read-only directory holds the file and refuses the unlink, which is
    # what a sticky /tmp does to another user's file.
    sticky.chmod(0o500)
    try:
        proc = _run_pid_block(pid_file)
    finally:
        sticky.chmod(0o700)
    assert proc.returncode == 0, proc.stderr
    assert "REACHED-THE-END" in proc.stdout
    assert "[WARNING]" in proc.stdout + proc.stderr


def test_a_pid_file_that_cannot_be_written_does_not_end_the_script(tmp_path):
    ro = tmp_path / "ro"
    ro.mkdir()
    ro.chmod(0o500)
    try:
        proc = _run_pid_block(ro / "lager-ssh-sync.pid")
    finally:
        ro.chmod(0o700)
    assert proc.returncode == 0, proc.stderr
    assert "REACHED-THE-END" in proc.stdout


def test_the_normal_path_still_records_the_poller(tmp_path):
    pid_file = tmp_path / "lager-ssh-sync.pid"
    proc = _run_pid_block(pid_file)
    assert proc.returncode == 0, proc.stderr
    assert pid_file.exists(), "the poller's pid was not recorded"
    assert pid_file.read_text().strip().isdigit()
    assert "[WARNING]" not in proc.stdout + proc.stderr


def test_a_stale_pid_file_is_replaced(tmp_path):
    pid_file = tmp_path / "lager-ssh-sync.pid"
    pid_file.write_text("999999\n")
    proc = _run_pid_block(pid_file)
    assert proc.returncode == 0, proc.stderr
    assert pid_file.read_text().strip() != "999999"


# #646: a poller the PID file did not name was never stopped, and neither
# `lager uninstall` nor any later run could find it. Two such pollers ran for
# more than two weeks on one box, rebuilding authorized_keys from a key
# directory that a reinstall could recreate with fewer keys.

def _needs_pkill():
    if subprocess.run(["bash", "-c", "command -v pkill && command -v pgrep"],
                      capture_output=True).returncode != 0:
        pytest.skip("pkill/pgrep not available on this platform")


def test_the_poller_runs_under_the_marker(tmp_path):
    _needs_pkill()
    marker = f"lager-ssh-sync-test-{uuid.uuid4().hex[:12]}"
    proc = _run_pid_block(
        tmp_path / "pid", marker=marker,
        after=['ps -o args= -p "$_SSH_SYNC_PID" | sed "s/^/ARGS:/"'],
    )
    assert proc.returncode == 0, proc.stderr
    args = [line for line in proc.stdout.splitlines() if line.startswith("ARGS:")]
    assert args and args[0].startswith(f"ARGS:{marker} "), proc.stdout


def test_the_poller_still_syncs_after_the_re_exec(box):
    """The poller is a fresh bash, so the function and its variables must be
    exported to it, or every pass fails silently and no key is published."""
    _needs_pkill()
    box.stage("a", KEY_A)
    marker = f"lager-ssh-sync-test-{uuid.uuid4().hex[:12]}"
    script = "\n".join([
        "set -e",
        f"export HOME={shlex.quote(str(box.home))}",
        f"LAGER_AUTHORIZED_KEYS_D={shlex.quote(str(box.keys_dir))}",
        SYNC_SH,
        f"export LAGER_SSH_SYNC_PID_FILE={shlex.quote(str(box.home / 'pid'))}",
        f"export LAGER_SSH_SYNC_MARKER={shlex.quote(marker)}",
        PID_SH,
        "sleep 7",
        'kill "$_SSH_SYNC_PID" 2>/dev/null || true',
    ])
    box.home.mkdir(parents=True, exist_ok=True)
    proc = subprocess.run(["bash", "-c", script], capture_output=True, text=True, timeout=30)
    assert proc.returncode == 0, proc.stderr
    assert KEY_A in box.auth_keys.read_text()


def test_a_marked_poller_the_pid_file_does_not_name_is_stopped(tmp_path, stray):
    """The reported case: the PID file names one poller, and another runs on."""
    _needs_pkill()
    marker = f"lager-ssh-sync-test-{uuid.uuid4().hex[:12]}"
    orphan = stray(["bash", "-c", f"exec -a {marker} sleep 300"])
    pid_file = tmp_path / "pid"
    pid_file.write_text("999999\n")
    proc = _run_pid_block(pid_file, marker=marker)
    assert proc.returncode == 0, proc.stderr
    assert _gone(orphan), "a marked poller not named in the PID file survived"


def _legacy_script(tmp_path):
    """A stand-in for a pre-marker poller: bash running a script file, which
    is the command line a real one has (`bash box/start_box.sh ...`).

    Returns (path, name regex). The name is unique, so no real process can
    match it.
    """
    stem = f"start-box-test-{uuid.uuid4().hex[:12]}"
    path = tmp_path / f"{stem}.sh"
    # `; :` stops bash exec-ing sleep in place, so the process stays bash.
    path.write_text("sleep 300; :\n")
    return path, f"{stem}[.]sh"


def test_a_pre_marker_poller_is_stopped_under_the_lock(tmp_path, stray):
    """Pollers started before the marker are subshells of start_box.sh."""
    _needs_pkill()
    path, legacy = _legacy_script(tmp_path)
    orphan = stray(["bash", str(path), "--no-publish"])
    proc = _run_pid_block(tmp_path / "pid", legacy=legacy, lock_held=True)
    assert proc.returncode == 0, proc.stderr
    assert _gone(orphan), "a pre-marker poller survived a run holding the lock"
    assert "left by an earlier run" in proc.stdout


def test_without_the_lock_pre_marker_processes_are_left_alone(tmp_path, stray):
    """Without the single-instance lock, a process naming start_box.sh might be
    a live run, so the sweep must not touch it."""
    _needs_pkill()
    path, legacy = _legacy_script(tmp_path)
    other = stray(["bash", str(path)])
    proc = _run_pid_block(tmp_path / "pid", legacy=legacy, lock_held=False)
    assert proc.returncode == 0, proc.stderr
    assert other.poll() is None, "the sweep ran without the lock"


def test_a_process_that_only_names_the_script_is_left_alone(tmp_path, stray):
    """The bench outage: `lager update` run ON the box (a CI runner on the
    bench box, same login user) starts this script with
    `ssh <box> 'cd ~/box && ./start_box.sh'`. That ssh client names the script
    but does not run it. The first sweep matched any command line naming the
    script, killed the client, and hung up the script's own session mid-start.
    """
    _needs_pkill()
    path, legacy = _legacy_script(tmp_path)
    remote = f"cd ~/box && chmod +x {path.name} && LAGER_SKIP_BUILD=1 ./{path.name}"
    ssh_client = stray(["bash", "-c", 'exec -a ssh bash -c "sleep 300; :" ssh "$@"', "x",
                        "-o", "BatchMode=yes", "lagerdata@box", remote])
    wrapper = stray(["bash", "-c", f"sleep 300; : {remote}"])
    proc = _run_pid_block(tmp_path / "pid", legacy=legacy, lock_held=True)
    assert proc.returncode == 0, proc.stderr
    assert ssh_client.poll() is None, "the sweep killed an ssh client that names the script"
    assert wrapper.poll() is None, "the sweep killed a `bash -c` wrapper that names the script"
    assert "left by an earlier run" not in proc.stdout


def test_the_sweep_never_kills_the_shell_that_started_this_run(tmp_path):
    """An install runs `bash -c "cd ~/box && ./start_box.sh"`, and that shell's
    command line names start_box.sh too."""
    _needs_pkill()
    path, legacy = _legacy_script(tmp_path)
    script = _pid_script(tmp_path / "pid", legacy=legacy, lock_held=True)
    wrapper = 'bash -c "$INNER"; echo "WRAPPER-SURVIVED"'
    proc = subprocess.run(
        ["bash", "-c", wrapper, str(path)],
        capture_output=True, text=True, timeout=30,
        env={**os.environ, "INNER": script},
    )
    assert "REACHED-THE-END" in proc.stdout, proc.stderr
    assert "WRAPPER-SURVIVED" in proc.stdout, "the sweep killed its own parent shell"
