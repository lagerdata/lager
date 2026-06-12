# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

"""
    lager.commands.utility.update

    Update box code from GitHub repository

    Migrated from cli/update/commands.py to cli/commands/utility/update.py.
"""
import click
import requests
import re
import shutil
import subprocess
import threading
import time
import sys
from ...box_storage import (
    auto_lock_acquire_for_command,
    get_box_user,
    resolve_and_validate_box,
)
from ...context import get_default_box
from ...core.ssh_utils import get_ssh_connection_pool
from ..box._ssh import ensure_lager_box_keypair, key_auth_works
from ...errors import LagerError


def resolve_version_ref(target_version):
    """Resolve a ``--version`` value to the git refs used to update a box.

    A semver version — with or without a leading ``v`` (e.g. ``0.18.5`` or
    ``v0.18.5``), including common pre-release suffixes (``-rc1``, ``-beta2``,
    ``-alpha``, ``-preview``) — resolves to the release **tag** ``vX.Y.Z``.
    Version branches (the bare ``X.Y.Z`` refs) are deprecated in favour of tags;
    see RELEASE_PROCESS.md. Any other value (``main``, ``staging``, a feature
    branch, or a custom suffix like ``-notes``) is treated as a branch and
    resolves to ``origin/<name>``.

    Returns ``(checkout, reset, fetch)``:
    - ``checkout`` — ref for ``git checkout -f`` (and display): the tag, or the branch name.
    - ``reset``    — ref for ``git reset --hard`` / ``git rev-list``: the tag, or ``origin/<branch>``.
    - ``fetch``    — argument for ``git fetch origin``. For tags this is an explicit
      refspec (``refs/tags/<tag>:refs/tags/<tag>``) so the tag is created as a local
      ref; ``git fetch origin <tag>`` alone only sets FETCH_HEAD, leaving
      ``git rev-list``/``git checkout <tag>`` unable to resolve it. For branches it
      is just the branch name (``origin/<branch>`` is updated via the default refspec).
    """
    m = re.match(r'^v?(\d+\.\d+\.\d+(?:-(?:rc|alpha|beta|preview)\d*)?)$', target_version)
    if m:
        tag = f'v{m.group(1)}'
        return tag, tag, f'refs/tags/{tag}:refs/tags/{tag}'
    return target_version, f'origin/{target_version}', target_version


def wait_for_box_ready(box_ip, *, timeout_s=60, initial_delay_s=2):
    """Poll http://<box_ip>:5000/health until 200 or timeout.

    Returns True on ready, False on timeout. The Python service on port 5000
    (the on-box script-execution service) is the last to come up after the
    container restarts, so polling it is more conservative than polling the
    Flask server on 9000.
    """
    deadline = time.monotonic() + timeout_s
    time.sleep(initial_delay_s)
    backoff = 1.0
    while time.monotonic() < deadline:
        try:
            r = requests.get(f'http://{box_ip}:5000/health', timeout=3)
            if r.status_code == 200:
                return True
        except requests.exceptions.RequestException:
            pass
        time.sleep(backoff)
        backoff = min(backoff * 1.5, 5.0)
    return False


# Files whose contents legitimately invalidate the cached Docker image.
# Dockerfile is the universal one; requirements.txt may not exist on every box
# revision, so we sha256sum it only when present.
_BUILD_HASH_INPUTS = [
    '~/box/lager/docker/box.Dockerfile',
    '~/box/lager/requirements.txt',
]


def _build_hash_shell_cmd():
    """Shell snippet that prints a hash of the docker-build inputs.

    Missing files are silently skipped (so the hash still works on box
    revisions without a requirements.txt). Prints an empty string if nothing
    matched, which we treat as "skip auto-invalidation".

    `~/box/...` paths are tilde-expanded by the for-loop's word-expansion
    pass before iteration, so `$f` inside the body is already absolute —
    no `eval` needed. The `[ -n "$out" ]` gate is what makes the "empty
    string when nothing matched" promise true; without it, an empty pipe
    into sha256sum would hash the empty string (`e3b0c44...`) and mask the
    no-files case.
    """
    paths = ' '.join(_BUILD_HASH_INPUTS)
    return (
        'out=$(for f in ' + paths + '; do '
        '  [ -f "$f" ] && sha256sum "$f"; '
        'done); '
        '[ -n "$out" ] && echo "$out" | sha256sum | cut -d" " -f1'
    )


def _read_build_hash(ssh_runner):
    """Return the sha256 of the box's *current* docker-build inputs.

    Used after a git update to recompute the hash against the freshly pulled
    Dockerfile/requirements — the upfront probe's value reflects the pre-pull
    tree, so it goes stale the moment code actually changes. When nothing was
    pulled the probe's value is still accurate and this round-trip is skipped.
    """
    r = ssh_runner(_build_hash_shell_cmd())
    return r.stdout.strip() if r.returncode == 0 else ''


def _read_box_source_version(ssh_runner):
    """Return the `__version__` string declared in `cli/__init__.py` at the
    box's current HEAD, or empty.

    Reads via `git show HEAD:cli/__init__.py` so the value is available
    even on cone-mode-sparse-checkout boxes where the file isn't in the
    working tree but is still in the git object DB. The file is the
    actual source of truth in this repo — releases bump it first, then
    tag from there — so it tells us the current version even on untagged
    commits where `git describe` would only return the previous tag.
    """
    import re
    r = ssh_runner('git -C ~/box show HEAD:cli/__init__.py 2>/dev/null | grep "^__version__"')
    if r.returncode != 0:
        return ''
    m = re.match(r"""__version__\s*=\s*['"]([^'"]+)['"]""", r.stdout.strip())
    return m.group(1) if m else ''


# --- Box-state probe -------------------------------------------------------
#
# A single SSH round-trip that gathers every read-only fact the update flow
# needs, replacing ~11 individual test/cat/git/diff/stat calls. Each fact is
# emitted as a `LAGER_PROBE_<KEY>=<value>` line; the parser ignores anything
# without that prefix (motd banners, sudo lecture text, etc.).
_PROBE_PREFIX = 'LAGER_PROBE_'


def _probe_shell_script():
    """Shell script that prints `LAGER_PROBE_<KEY>=<value>` for every
    read-only fact the update flow needs about the box.

    Every fact is computed independently and guarded, so a missing file or
    failed sub-command yields an empty value instead of aborting the script
    — it exits 0 whenever SSH itself connected, and the parser treats absent
    keys as unknown.

    The build-hash inputs reflect the box's *current* (pre-pull) tree; after
    a git update the caller recomputes via `_read_build_hash`. The literal
    `LAGER_PROBE_` prefix below must stay in sync with `_PROBE_PREFIX`.
    """
    script = '''\
if [ -d ~/box/.git ]; then echo "LAGER_PROBE_IS_GIT_REPO=1"; else echo "LAGER_PROBE_IS_GIT_REPO=0"; fi
echo "LAGER_PROBE_REMOTE_URL=$(git -C ~/box remote get-url origin 2>/dev/null)"
if [ -d ~/box/box ]; then echo "LAGER_PROBE_HAS_BOX_SUBDIR=1"; else echo "LAGER_PROBE_HAS_BOX_SUBDIR=0"; fi
echo "LAGER_PROBE_GIT_LOG=$(git -C ~/box log -1 --format='%h - %s (%cr)' 2>/dev/null)"
echo "LAGER_PROBE_BUILD_HASH_NEW=$(__BUILD_HASH_CMD__)"
echo "LAGER_PROBE_BUILD_HASH_STORED=$(cat /etc/lager/build-hash 2>/dev/null)"
if [ -d ~/box/udev_rules ]; then _up=~/box/udev_rules
elif [ -d ~/box/box/udev_rules ]; then _up=~/box/box/udev_rules
else _up=""
fi
echo "LAGER_PROBE_UDEV_SRC_PATH=$_up"
if [ -n "$_up" ] && [ -f "$_up/99-instrument.rules" ]; then
  echo "LAGER_PROBE_UDEV_SRC_RULES=1"
  if diff -q "$_up/99-instrument.rules" /etc/udev/rules.d/99-instrument.rules >/dev/null 2>&1; then
    echo "LAGER_PROBE_UDEV_IN_SYNC=1"
  else
    echo "LAGER_PROBE_UDEV_IN_SYNC=0"
  fi
else
  echo "LAGER_PROBE_UDEV_SRC_RULES=0"
  echo "LAGER_PROBE_UDEV_IN_SYNC=0"
fi
# modprobe.d blacklist files (0.20.0+: usbtmc blacklist). Same shape as the
# udev probe above — find the source dir, check whether the specific file
# exists in /etc/modprobe.d/ and matches the source contents.
if [ -d ~/box/modprobe_d ]; then _mp=~/box/modprobe_d
elif [ -d ~/box/box/modprobe_d ]; then _mp=~/box/box/modprobe_d
else _mp=""
fi
echo "LAGER_PROBE_MODPROBE_SRC_PATH=$_mp"
if [ -n "$_mp" ] && [ -f "$_mp/blacklist-usbtmc.conf" ]; then
  echo "LAGER_PROBE_MODPROBE_SRC_CONFS=1"
  if diff -q "$_mp/blacklist-usbtmc.conf" /etc/modprobe.d/blacklist-usbtmc.conf >/dev/null 2>&1; then
    echo "LAGER_PROBE_MODPROBE_IN_SYNC=1"
  else
    echo "LAGER_PROBE_MODPROBE_IN_SYNC=0"
  fi
else
  echo "LAGER_PROBE_MODPROBE_SRC_CONFS=0"
  echo "LAGER_PROBE_MODPROBE_IN_SYNC=0"
fi
if lsmod 2>/dev/null | grep -q '^usbtmc'; then
  echo "LAGER_PROBE_USBTMC_LOADED=1"
else
  echo "LAGER_PROBE_USBTMC_LOADED=0"
fi
if [ -f /etc/sudoers.d/lagerdata-udev ]; then
  echo "LAGER_PROBE_SUDOERS_OWNER=$(stat -c '%u' /etc/sudoers.d/lagerdata-udev 2>/dev/null || stat -f '%u' /etc/sudoers.d/lagerdata-udev 2>/dev/null || echo UNKNOWN)"
else
  echo "LAGER_PROBE_SUDOERS_OWNER=NOTFOUND"
fi
if test -f /etc/lager/.boxcfg-sudoers-v2 && sudo -n DEBIAN_FRONTEND=noninteractive apt-get --version >/dev/null 2>&1; then
  echo "LAGER_PROBE_BOXCFG_SUDOERS_OK=1"
else
  echo "LAGER_PROBE_BOXCFG_SUDOERS_OK=0"
fi
echo "LAGER_PROBE_ETC_VERSION=$(cat /etc/lager/version 2>/dev/null)"
'''
    return script.replace('__BUILD_HASH_CMD__', _build_hash_shell_cmd())


def _parse_probe_output(stdout):
    """Parse `LAGER_PROBE_<KEY>=<value>` lines into a dict.

    Tolerant by design: any line without the prefix is ignored (so a motd
    banner or sudo lecture can't corrupt the result), and a repeated key
    keeps its last value.
    """
    facts = {}
    for line in stdout.splitlines():
        if line.startswith(_PROBE_PREFIX):
            key, _, value = line[len(_PROBE_PREFIX):].partition('=')
            facts[key] = value
    return facts


# Progress-bar denominator. 15 steps always run; 3 are conditional (flatten,
# cached-image wipe, J-Link install). We use the max so the denominator never
# jumps mid-flight — light paths simply finish below 18/18 and `finish()`
# overrides with a full bar. Keep in sync with the `progress.update()` calls
# in `_update_logic`.
_PROGRESS_TOTAL_STEPS = 18


class ProgressBar:
    """Simple progress bar for tracking update steps.

    Rendering strategy:
      * On every `update()` and `finish()`, render once.
      * Additionally, when stdout is a TTY, a daemon thread re-renders
        every second so the elapsed-time counter advances during long
        steps (e.g. 'Building container' ~6 min) instead of appearing
        frozen at the moment the step started.
      * When stdout is NOT a TTY (piped, redirected, CI log), the
        periodic thread does not start — captured output is therefore
        one frame per step, not dozens. Pastes from live terminal
        scrollback will still capture every per-second frame; that's
        unavoidable because the bytes really did go to stdout.
    """

    # ANSI escape codes for cursor control
    CLEAR_LINE = '\033[2K'  # Clear entire line
    CURSOR_START = '\r'     # Move cursor to start of line

    # Reserved characters in the rendered line that aren't the bar itself:
    # "[]" brackets + " N/N " step counter (up to 7) + label + " " + elapsed
    # (up to ~10 for "1h 23m 45s"). Computed dynamically below per-render so
    # the bar width can shrink when the label or elapsed widens. The hard
    # cap below is what stops the line from EVER touching the terminal edge,
    # which is the wrap that produces stacked-line artifacts.
    _RIGHT_MARGIN = 2  # leave at least this many cols free of the right edge

    def __init__(self, total_steps):
        self.total_steps = total_steps
        self.current_step = 0
        self.current_task = ""
        self.start_time = time.time()
        self._stop_event = threading.Event()
        self._render_thread = None
        self._tty = sys.stdout.isatty()
        # Guards `_render` so the periodic thread's tick and a main-thread
        # `update()` can't interleave their stdout writes mid-frame. Short
        # critical section — one read of shared state + one write.
        self._lock = threading.Lock()

    def _start_periodic_thread(self):
        """Spin up the 1s tick thread (idempotent). Only call on a TTY."""
        if self._render_thread is not None and self._render_thread.is_alive():
            return
        self._stop_event.clear()
        self._render_thread = threading.Thread(target=self._periodic_render, daemon=True)
        self._render_thread.start()

    def _stop_periodic_thread(self):
        """Stop the 1s tick thread if running (idempotent)."""
        if self._render_thread is None:
            return
        self._stop_event.set()
        self._render_thread.join(timeout=2)
        self._render_thread = None

    def _periodic_render(self):
        """Tick the bar once per second so elapsed time advances during
        long steps. Exits when `_stop_event` is set (via `pause()` or
        `finish()`)."""
        while not self._stop_event.wait(timeout=1.0):
            self._render()

    def update(self, task_name):
        """Advance to the next step and render."""
        self.current_step += 1
        self.current_task = task_name
        self._render()
        # Lazy-start the periodic thread on first step so we don't tick a
        # 0/N bar when nobody has called update() yet.
        if self._tty:
            self._start_periodic_thread()

    def _format_elapsed_time(self):
        """Format elapsed time as human-readable string."""
        elapsed = int(time.time() - self.start_time)
        if elapsed < 60:
            return f"{elapsed}s"
        elif elapsed < 3600:
            minutes = elapsed // 60
            seconds = elapsed % 60
            return f"{minutes}m {seconds:02d}s"
        else:
            hours = elapsed // 3600
            minutes = (elapsed % 3600) // 60
            seconds = elapsed % 60
            return f"{hours}h {minutes:02d}m {seconds:02d}s"

    def _terminal_cols(self):
        """Best-effort terminal width. Falls back to 80 on detection failure
        (non-TTY, weird env, etc.) — same default as POSIX."""
        try:
            return shutil.get_terminal_size(fallback=(80, 24)).columns
        except Exception:
            return 80

    # Pad elapsed to a fixed width so the bar doesn't jitter sideways as
    # the time grows (e.g. "5s" → "1m 41s" → "1h 23m 45s"). 10 covers the
    # longest case from `_format_elapsed_time`.
    _ELAPSED_FIELD_WIDTH = 10

    def _layout(self, label, elapsed_padded):
        """Pick a bar width that, combined with the rest of the line, stays
        clear of the right edge. `elapsed_padded` is the elapsed string
        already padded to `_ELAPSED_FIELD_WIDTH`. Returns (bar_width,
        truncated_label)."""
        cols = self._terminal_cols()
        step_counter = f' {min(self.current_step, self.total_steps)}/{self.total_steps} '
        # Fixed overhead = elapsed + space + brackets + step counter + space
        overhead = len(elapsed_padded) + 1 + 2 + len(step_counter) + 1
        # What's left for bar + label, minus the right margin.
        budget = cols - overhead - self._RIGHT_MARGIN
        if budget < 12:
            # Pathologically narrow terminal: render nothing rather than wrap.
            return 0, ''
        # Split: prefer a 20-wide bar, give the rest (up to 25) to the label.
        bar_width = min(20, max(6, budget - 10))
        label_room = max(0, budget - bar_width)
        label = label[:label_room].ljust(label_room)
        return bar_width, label

    def _render(self):
        """Render the bar. Width is computed against the live terminal width
        so the line never reaches the right edge — that wrap is what causes
        the stacked-bar artifact: \\r\\033[2K only clears the current row,
        leaving the wrapped portion above as orphan text.

        Held under `_lock` so the periodic 1s thread and the main-thread
        `update()` can't interleave their writes to stdout mid-frame."""
        with self._lock:
            capped_step = min(self.current_step, self.total_steps)
            elapsed_padded = self._format_elapsed_time().ljust(self._ELAPSED_FIELD_WIDTH)
            bar_width, label = self._layout(self.current_task, elapsed_padded)
            if bar_width == 0:
                output = f'{self.CLEAR_LINE}{self.CURSOR_START}{capped_step}/{self.total_steps}'
            else:
                percent = min(self.current_step / self.total_steps, 1.0)
                filled = int(bar_width * percent)
                bar = '█' * filled + '░' * (bar_width - filled)
                output = (
                    f'{self.CLEAR_LINE}{self.CURSOR_START}'
                    f'{elapsed_padded} [{bar}] {capped_step}/{self.total_steps} {label}'
                )
            sys.stdout.write(output)
            sys.stdout.flush()

    def finish(self, success=True):
        """Complete the progress bar."""
        self._stop_periodic_thread()
        elapsed_padded = self._format_elapsed_time().ljust(self._ELAPSED_FIELD_WIDTH)
        # Pick the bar width the same way _render does, but for an empty
        # label slot (finish text replaces the label). Keeps the closing
        # frame the same shape as the in-flight ones.
        bar_width, _ = self._layout('', elapsed_padded)
        bar_width = max(bar_width, 6)
        if success:
            bar = '█' * bar_width
            sys.stdout.write(f'{self.CLEAR_LINE}{self.CURSOR_START}{elapsed_padded} [{bar}] Complete!\n')
        else:
            filled = int(bar_width * self.current_step / self.total_steps)
            bar = '█' * filled + '░' * (bar_width - filled)
            sys.stdout.write(f'{self.CLEAR_LINE}{self.CURSOR_START}{elapsed_padded} [{bar}] Failed\n')
        sys.stdout.flush()

    def pause(self):
        """Stop the periodic re-render and clear the in-flight bar before
        an interactive prompt — otherwise the next 1s tick would overwrite
        the prompt and the user couldn't see what's waiting on stdin.
        Pair with `resume()` after the interactive subprocess returns."""
        self._stop_periodic_thread()
        sys.stdout.write(f'{self.CLEAR_LINE}{self.CURSOR_START}')
        sys.stdout.flush()

    def resume(self):
        """Restart the periodic re-render after `pause()`. No-op when
        stdout isn't a TTY."""
        if self._tty:
            self._start_periodic_thread()


def _update_logic(ctx, *, box, yes, version, verbose, check, force=False):
    """Core update logic behind `lager update`.

    Not a Click command itself — the thin Click wrapper at the bottom of this
    module adds option decorators and dispatches here.

    `force` does two things: it skips the "already up to date" early-exit so a
    box left in a half-updated state by a prior failed run can be re-updated,
    and it forces a clean rebuild (wiping the cached image and the cargo/npm
    persistence volumes) so no stale layer or toolchain survives the retry.
    """
    from ...box_storage import update_box_version
    from ... import __version__ as cli_version

    # Helper for conditional output
    def log(message, nl=True, **kwargs):
        """Print message only in verbose mode."""
        if verbose:
            click.echo(message, nl=nl, **kwargs)

    def log_status(status, color):
        """Append a colored status to the current verbose line.

        Pairs with a preceding `log('...', nl=False)`; no-op unless --verbose.
        """
        if verbose:
            click.secho(f' {status}', fg=color)

    def log_error(message):
        """Always print errors."""
        click.secho(message, fg='red', err=True)

    # Default to 'main' version if not specified
    target_version = version or 'main'

    # Resolve the version to git refs. A semver pin (with or without a leading
    # 'v') maps to the release TAG 'vX.Y.Z'; version branches are deprecated in
    # favour of tags (see RELEASE_PROCESS.md). Named branches use origin/<name>.
    # `target_version` is normalised (e.g. '0.18.5' -> 'v0.18.5') so the fetch,
    # checkout and user-facing messages all agree. `fetch_ref` is what we hand to
    # `git fetch origin` (an explicit tag refspec for tags; see resolve_version_ref).
    target_version, git_ref, fetch_ref = resolve_version_ref(target_version)

    # Use default box if none specified
    if not box:
        box = get_default_box(ctx)

    box_name = box

    # Resolve box name to IP address
    resolved_box = resolve_and_validate_box(ctx, box)

    # Get username (defaults to 'lagerdata' if not specified)
    username = get_box_user(box) or 'lagerdata'

    ssh_host = f'{username}@{resolved_box}'

    # Display update information (always show this)
    click.echo()
    click.secho('Box Update', fg='blue', bold=True)
    click.echo(f'Target:  {box_name} ({resolved_box})')
    click.echo(f'Version: {target_version}')
    if verbose:
        click.echo(f'CLI:     {cli_version}')
    click.echo()

    # Confirm before proceeding (skipped in --check: the dry-run is read-only,
    # so there's nothing to confirm).
    if not yes and not check:
        if not click.confirm('This will update the box code and restart services. Continue?'):
            click.secho('Update cancelled.', fg='yellow')
            ctx.exit(0)

    # Initialize progress bar (only in non-verbose mode). See
    # `_PROGRESS_TOTAL_STEPS` for why the denominator is the max step count.
    progress = None if verbose else ProgressBar(total_steps=_PROGRESS_TOTAL_STEPS)

    if not verbose:
        click.echo()  # Blank line before progress bar

    # Step 1: Check SSH connectivity
    if progress:
        progress.update("Checking SSH...")
    log('Checking SSH connectivity...', nl=False)

    import os
    key_file = os.path.expanduser('~/.ssh/lager_box')
    use_interactive_ssh = False
    use_explicit_key = False

    def setup_ssh_key():
        """Create lager_box key if needed and copy to box. Returns True if successful."""
        nonlocal use_explicit_key

        # Create key if it doesn't exist. Shared with `lager authorize` via
        # ensure_lager_box_keypair so the key type/comment can't drift between
        # the two provisioning paths; it raises on failure, which we translate
        # to this function's bool-return contract.
        if not os.path.exists(key_file):
            click.echo()
            click.echo('Creating SSH key...')
        try:
            if ensure_lager_box_keypair(key_file):
                click.secho('SSH key created', fg='green')
        except LagerError:
            log_error('Error: Failed to create SSH key')
            return False

        # Copy key to box
        click.echo()
        click.echo('Copying SSH key to box (enter password when prompted):')
        copy_result = subprocess.run(
            [
                'ssh-copy-id',
                '-o', 'StrictHostKeyChecking=accept-new',
                '-o', 'ConnectTimeout=30',
                '-i', key_file,
                ssh_host,
            ],
            timeout=300  # 5 minutes - allow time for user to enter password
        )

        if copy_result.returncode == 0 and key_auth_works(ssh_host):
            click.echo()
            click.secho('SSH key installed successfully!', fg='green')
            click.echo('Future connections will not require a password.')
            click.echo()
            use_explicit_key = True
            return True

        click.secho('Failed to set up SSH key.', fg='yellow')
        return False

    try:
        # First try with the lager_box key if it exists. Same unattended
        # probe `lager authorize` uses, so "does the key already work?" is
        # answered identically in both commands.
        if os.path.exists(key_file) and key_auth_works(ssh_host):
            use_explicit_key = True
            log_status('OK', 'green')

        # If lager_box key didn't work for this box, we need to set it up
        if not use_explicit_key:
            if progress:
                progress.finish(success=False)
            click.echo()  # New line after progress bar
            click.secho('SSH key not configured for this box', fg='yellow')
            click.echo()

            if yes or click.confirm('Set up SSH key for this box? (requires password once, then never again)'):
                if setup_ssh_key():
                    # Key setup successful, reinitialize progress bar
                    if not verbose:
                        progress = ProgressBar(total_steps=_PROGRESS_TOTAL_STEPS)
                        progress.current_step = 1
                else:
                    # Key setup failed, ask if they want to continue with password
                    click.echo()
                    if yes or click.confirm('SSH key setup failed. Continue with password authentication?'):
                        use_interactive_ssh = True
                        if not verbose:
                            progress = ProgressBar(total_steps=_PROGRESS_TOTAL_STEPS)
                            progress.current_step = 1
                    else:
                        click.secho('Update cancelled.', fg='yellow')
                        ctx.exit(0)
            else:
                click.secho('Update cancelled.', fg='yellow')
                ctx.exit(0)

        # At this point we should have either key-based or password-based auth ready
        if not use_explicit_key and not use_interactive_ssh:
            # This shouldn't happen, but just in case
            log_error('Error: No SSH authentication method available')
            ctx.exit(1)

    except subprocess.TimeoutExpired:
        if progress:
            progress.finish(success=False)
        log_error(f'Error: Connection to {ssh_host} timed out')
        ctx.exit(1)
    except Exception as e:
        if progress:
            progress.finish(success=False)
        log_error(f'Error: {str(e)}')
        ctx.exit(1)

    # Multiplex all subsequent SSH commands over a single TCP connection.
    # ControlMaster=auto starts the master on the first call (which is
    # `run_ssh_command_with_output` below) and reuses it for everything else.
    # ControlPersist=10m keeps it alive briefly past command exit so a
    # follow-up `lager hello` reuses it for free.
    _ssh_pool = get_ssh_connection_pool()
    _ssh_control_path = _ssh_pool.get_control_path(ssh_host)
    _ssh_mux_opts = [
        '-o', 'ControlMaster=auto',
        '-o', f'ControlPath={_ssh_control_path}',
        '-o', 'ControlPersist=10m',
        '-o', 'ServerAliveInterval=30',
        '-o', 'ServerAliveCountMax=3',
    ]

    # Helper function to run SSH commands
    def run_ssh_command_with_output(cmd, timeout_secs=120):
        """Run an SSH command and capture output."""
        ssh_cmd = ['ssh']
        if use_explicit_key:
            ssh_cmd.extend(['-i', key_file])
        if not use_interactive_ssh:
            ssh_cmd.extend(['-o', 'BatchMode=yes'])
        ssh_cmd.extend(_ssh_mux_opts)
        ssh_cmd.append(ssh_host)
        ssh_cmd.append(cmd)
        return subprocess.run(ssh_cmd, capture_output=True, text=True, timeout=timeout_secs)

    def run_ssh_command_interactive(cmd, timeout_secs=300, allow_sudo_prompt=False):
        """Run an SSH command that may require sudo password input.

        This function allocates a pseudo-terminal (-t) to allow interactive
        password prompts when using password authentication mode.

        Args:
            cmd: Command to run on remote host
            timeout_secs: Timeout in seconds
            allow_sudo_prompt: If True, don't use BatchMode even if SSH keys work
                             (allows sudo password prompts)
        """
        ssh_cmd = ['ssh', '-t']  # Always use -t for interactive commands
        if use_explicit_key:
            ssh_cmd.extend(['-i', key_file])
        # Only use BatchMode if we don't need sudo prompts
        if not use_interactive_ssh and not allow_sudo_prompt:
            ssh_cmd.extend(['-o', 'BatchMode=yes'])
        ssh_cmd.extend(_ssh_mux_opts)
        ssh_cmd.append(ssh_host)
        ssh_cmd.append(cmd)
        # Don't capture output - let it stream to terminal for interactive prompts
        return subprocess.run(ssh_cmd, timeout=timeout_secs)

    def write_box_version_file(box_cli_version_value):
        """Write /etc/lager/version on the box. Idempotent (no-op if content matches).

        Returns True on success, False on failure (caller decides whether to
        treat as fatal). Retries the actual write up to 3 times with backoff.

        This used to live inline at the end of the update flow; extracted so
        the early-exit "already up to date" branch can also reconcile the
        on-box version file with the local cache — the previous behavior
        skipped this and was the primary cause of users having to run
        `lager update` 2-3 times before it stuck.
        """
        if not box_cli_version_value:
            return True
        version_content = f'{box_cli_version_value}|{cli_version}'

        # One round-trip per attempt: the `[ ... ] ||` guard makes the write
        # a no-op when the file already matches, folding the old separate
        # read-then-write into a single command.
        write_cmd = (
            f'[ "$(cat /etc/lager/version 2>/dev/null)" = "{version_content}" ] '
            f'|| echo "{version_content}" > /etc/lager/version'
        )
        for attempt in range(3):
            result = run_ssh_command_with_output(write_cmd, timeout_secs=30)
            if result.returncode == 0:
                return True
            if attempt < 2:
                time.sleep(5)
        return False

    # Step 2: Inspect box state — one SSH round-trip gathers every read-only
    # fact the rest of the flow needs (git-repo check, remote URL, layout,
    # current commit, docker-build cache inputs, udev/sudoers state, on-box
    # version file). This replaces what used to be ~11 separate
    # test/cat/git/diff/stat round-trips.
    if progress:
        progress.update("Inspecting box...")
    log('Inspecting box...')

    probe_result = run_ssh_command_with_output(_probe_shell_script())
    if probe_result.returncode != 0:
        if progress:
            progress.finish(success=False)
        log_error('Error: Could not inspect box state over SSH')
        if probe_result.stderr and probe_result.stderr.strip():
            click.echo(probe_result.stderr.strip(), err=True)
        ctx.exit(1)
    facts = _parse_probe_output(probe_result.stdout)

    # 2a: the box directory must be a git checkout.
    if facts.get('IS_GIT_REPO') != '1':
        if progress:
            progress.finish(success=False)
        log_error('Error: Box directory is not a git repository')
        click.echo('The box may have been deployed with rsync instead of git clone.')
        click.echo('Please re-deploy the box using the latest deployment script.')
        ctx.exit(1)

    # 2b: migrate a legacy git@github.com: remote to HTTPS (open-source
    # migration). This is the one probed fact that needs a follow-up write,
    # and only on boxes deployed before the migration.
    remote_url = facts.get('REMOTE_URL', '')
    remote_migrated = False
    remote_migrate_failed = False
    if remote_url.startswith('git@github.com:'):
        https_url = remote_url.replace('git@github.com:', 'https://github.com/')
        migrate_result = run_ssh_command_with_output(
            f'git -C ~/box remote set-url origin {https_url}'
        )
        remote_migrated = migrate_result.returncode == 0
        remote_migrate_failed = not remote_migrated

    # 2c: a `~/box/box/` subdirectory is a sparse-checkout artifact that must
    # be flattened after the git ops below. The flat layout (no box/ subdir)
    # is the healthy post-update state, not a broken one — only set
    # needs_flatten when there is actually a `~/box/box/` to flatten.
    needs_flatten = facts.get('HAS_BOX_SUBDIR') == '1'

    # Verbose: print the gathered state as one block instead of the dozen
    # "Checking X... OK" lines this used to emit.
    if verbose:
        click.echo('  Repository:    OK (git checkout)')
        if remote_migrated:
            click.echo('  Remote URL:    migrated SSH -> HTTPS')
        elif remote_migrate_failed:
            click.echo('  Remote URL:    WARNING could not migrate to HTTPS')
        click.echo(f'  Layout:        {"box/ subdir (will flatten)" if needs_flatten else "flat"}')
        click.echo(f'  Current:       {facts.get("GIT_LOG", "").strip() or "(unknown)"}')
        _udev_path = facts.get('UDEV_SRC_PATH', '')
        if not _udev_path:
            _udev_state = 'source dir missing'
        elif facts.get('UDEV_SRC_RULES') != '1':
            _udev_state = 'rules file missing'
        elif facts.get('UDEV_IN_SYNC') == '1':
            _udev_state = 'in sync'
        else:
            _udev_state = 'update needed'
        click.echo(f'  udev rules:    {_udev_state}')
        _mp_path = facts.get('MODPROBE_SRC_PATH', '')
        if not _mp_path:
            _mp_state = 'source dir missing (older box code)'
        elif facts.get('MODPROBE_SRC_CONFS') != '1':
            _mp_state = 'blacklist file missing'
        elif facts.get('MODPROBE_IN_SYNC') == '1':
            _mp_state = 'in sync'
        else:
            _mp_state = 'update needed'
        _usbtmc = 'loaded (will try to unload)' if facts.get('USBTMC_LOADED') == '1' else 'not loaded'
        click.echo(f'  modprobe.d:    {_mp_state} (usbtmc {_usbtmc})')
        _owner = facts.get('SUDOERS_OWNER', '')
        if _owner in ('NOTFOUND', 'UNKNOWN', ''):
            _sudoers_state = 'not present'
        elif _owner == '0':
            _sudoers_state = 'OK (root-owned)'
        else:
            _sudoers_state = f'fix needed (owned by uid {_owner})'
        click.echo(f'  sudoers:       {_sudoers_state}')
        click.echo(f'  box-config:    {"OK" if facts.get("BOXCFG_SUDOERS_OK") == "1" else "needs bootstrap"}')

    # Step 3: Fetch from origin and measure box-vs-target divergence — one call.
    if progress:
        progress.update("Fetching updates...")
    log(f'Fetching {git_ref}...', nl=False)

    # `git fetch` then `git rev-list` in a single round-trip. fetch's combined
    # stdout+stderr precedes the `LAGER_FETCH_RC=` marker (so the detailed
    # error classification below still works); the rev-list line follows it.
    #
    # `--left-right --count HEAD...{git_ref}` returns `<ahead>\t<behind>` —
    # commits on HEAD-not-in-target and on-target-not-in-HEAD respectively.
    # We need *both* directions so a rollback (`--version <older>`) doesn't
    # look like "already up to date" the way the older one-way `HEAD..ref`
    # rev-list did: that variant only counted commits the box was *behind*
    # and treated any "ahead" state as in-sync, making downgrade impossible.
    fetch_script = (
        f'cd ~/box && git fetch origin {fetch_ref} 2>&1; '
        'echo "LAGER_FETCH_RC=$?"; '
        f'git rev-list --left-right --count HEAD...{git_ref} 2>/dev/null'
    )
    # Retry the fetch on *transient* failures only. Boxes on flaky links (e.g.
    # WiFi with a slow/intermittent resolver) hit sporadic DNS-resolution or
    # connection timeouts on `git fetch` that clear on a retry seconds later;
    # without this a single blip aborts the whole update. Auth failures and
    # "branch not found" are NOT transient, so we don't retry those — retrying
    # can't fix them and would just delay the real error.
    _FETCH_MAX_ATTEMPTS = 3          # 1 initial try + 2 retries
    _FETCH_BACKOFF_SECS = (3, 6)     # waited before retry 1 and retry 2
    _TRANSIENT_FETCH_SIGNS = (
        'could not resolve host',
        'name or service not known',
        'connection timed out',
        'timed out',
        'temporary failure in name resolution',
    )

    def _parse_fetch_result(result):
        """Pull (rc, stderr, revlist) out of one fetch round-trip's output."""
        lines = []
        rc = None
        revlist = ''
        for line in result.stdout.splitlines():
            if rc is None:
                if line.startswith('LAGER_FETCH_RC='):
                    try:
                        rc = int(line.split('=', 1)[1])
                    except ValueError:
                        rc = -1
                else:
                    lines.append(line)
            elif line.strip():
                revlist = line.strip()
        stderr = '\n'.join(lines).strip()
        # `rc is None` means the marker never arrived — SSH transport itself
        # failed (or died mid-command), not git. Fold the SSH stderr in so the
        # classifier can still reason about it.
        if rc is None:
            stderr = (stderr + '\n' + (result.stderr or '')).strip()
        return rc, stderr, revlist

    fetch_rc = None
    fetch_stderr = ''
    revlist_count_str = ''
    for _attempt in range(_FETCH_MAX_ATTEMPTS):
        result = run_ssh_command_with_output(fetch_script)
        fetch_rc, fetch_stderr, revlist_count_str = _parse_fetch_result(result)
        if fetch_rc == 0:
            break
        # Only retry when the failure looks transient and attempts remain.
        _is_transient = any(s in fetch_stderr.lower() for s in _TRANSIENT_FETCH_SIGNS)
        if not _is_transient or _attempt == _FETCH_MAX_ATTEMPTS - 1:
            break
        _delay = _FETCH_BACKOFF_SECS[_attempt]
        if verbose:
            click.secho(f' transient network error, retrying in {_delay}s...', fg='yellow')
        elif progress:
            progress.update(f"Fetch retry in {_delay}s...")
        time.sleep(_delay)

    if fetch_rc != 0:
        if progress:
            progress.finish(success=False)
        log_status('FAILED', 'red')
        stderr = fetch_stderr
        # Distinguish between different fetch error types
        if "Could not resolve host" in stderr or "Name or service not known" in stderr:
            log_error('Error: Could not resolve GitHub hostname')
            click.secho("The box cannot reach github.com.", err=True)
            click.secho("Possible causes:", err=True)
            click.secho("  - No internet connection on the box", err=True)
            click.secho("  - DNS resolution failure", err=True)
            click.secho("  - Firewall blocking outbound connections", err=True)
        elif "Permission denied" in stderr or "Authentication failed" in stderr:
            log_error('Error: GitHub authentication failed')
            click.secho("The box could not authenticate with GitHub.", err=True)
            click.secho("The remote URL may still be using SSH (git@github.com:...).", err=True)
            click.secho("Fix by switching to HTTPS:", err=True)
            click.secho("  ssh lagerdata@[BOX_NAME] 'cd ~/box && git remote set-url origin https://github.com/lagerdata/lager.git'", err=True)
        elif "not found" in stderr.lower() or "couldn't find remote ref" in stderr.lower():
            log_error(f"Error: Version '{target_version}' not found on remote")
            click.secho(f"'{target_version}' does not exist on GitHub as a tag or branch.", err=True)
            click.secho("Release versions are tags (e.g. v0.21.3): https://github.com/lagerdata/lager/tags", err=True)
            click.secho("Branches (main, staging, ...): https://github.com/lagerdata/lager/branches", err=True)
        elif "Connection refused" in stderr:
            log_error('Error: Connection to GitHub refused')
            click.secho("GitHub is not accepting connections.", err=True)
            click.secho("This may be a temporary issue. Try again later.", err=True)
        elif "timed out" in stderr.lower() or "Connection timed out" in stderr:
            log_error('Error: Connection to GitHub timed out')
            click.secho("The box could not connect to GitHub within the timeout period.", err=True)
            click.secho("Check the box's network connectivity.", err=True)
        else:
            log_error('Error: Failed to fetch updates from GitHub')
            if stderr:
                click.secho(f"Git error: {stderr}", err=True)
        ctx.exit(1)

    # Parse "<ahead>\t<behind>" from the rev-list line. Only trust an
    # "already up to date" fast-path when rev-list produced two integers;
    # otherwise stay conservative and let the rebuild run. A pull is needed
    # whenever the box diverges in either direction — forward (behind>0) or
    # backward (ahead>0, i.e. rollback to an older ref).
    needs_pull = False
    git_sync_confirmed = False
    commits_ahead = None
    commits_behind = None
    if revlist_count_str:
        parts = revlist_count_str.split()
        if len(parts) == 2:
            try:
                commits_ahead = int(parts[0])
                commits_behind = int(parts[1])
            except ValueError:
                commits_ahead = None
                commits_behind = None
    if commits_ahead is not None and commits_behind is not None:
        git_sync_confirmed = True
        needs_pull = commits_ahead > 0 or commits_behind > 0
    is_rollback = bool(commits_ahead) and not commits_behind

    if not git_sync_confirmed:
        log_status('fetched (update state unknown)', 'yellow')
    elif not needs_pull:
        log_status('already up to date', 'green')
    elif is_rollback:
        log_status(f'rolling back {commits_ahead} commit(s)', 'yellow')
    elif commits_ahead == 0:
        log_status(f'{commits_behind} new commit(s)', 'green')
    else:
        log_status(f'switching ({commits_ahead} ahead, {commits_behind} behind)', 'yellow')

    # Rollback is destructive in the sense that it rewrites the on-box git
    # tree backward, so confirm explicitly when not in --yes / --check mode.
    # The earlier generic "update the box code and restart services" prompt
    # fired before we knew direction, so this second confirm catches typo'd
    # `--version` arguments that would silently downgrade a box.
    if is_rollback and not yes and not check:
        if progress:
            progress.pause()
        click.echo()
        if not click.confirm(
            f'This will ROLL BACK {box_name} by {commits_ahead} commit(s) to {git_ref}. Continue?',
            default=False,
        ):
            click.secho('Update cancelled.', fg='yellow')
            ctx.exit(0)
        if progress:
            progress.resume()

    # --check / dry-run: report what would happen and exit. Must run before
    # any mutation (no git checkout, no udev install, no docker stop).
    # Exit codes:
    #   0 — already in sync, nothing to do
    #   1 — would update (code, deps, or both)
    #   2 — could not determine state (network error, etc.)
    if check:
        if progress:
            progress.finish(success=True)
        click.echo()

        current_version_raw = facts.get('ETC_VERSION', '').strip()
        current_box_version = current_version_raw.split('|', 1)[0] if current_version_raw else '(unknown)'

        # --check does no git pull, so the probe's build-hash inputs still
        # reflect the tree that would be built — no extra round-trip needed.
        _check_new_hash = facts.get('BUILD_HASH_NEW', '')
        _check_stored_hash = facts.get('BUILD_HASH_STORED', '')
        deps_will_change = (
            bool(_check_new_hash) and bool(_check_stored_hash)
            and _check_new_hash != _check_stored_hash
        )

        if not git_sync_confirmed:
            click.secho('Could not determine update state (git rev-list failed).', fg='red', err=True)
            ctx.exit(2)

        if commits_behind == 0 and commits_ahead == 0:
            code_status = 'in sync'
        elif is_rollback:
            code_status = f'will roll back ({commits_ahead} commit(s) ahead of {git_ref})'
        elif commits_ahead == 0:
            code_status = f'will update ({commits_behind} commit(s) behind {git_ref})'
        else:
            code_status = (
                f'will switch ({commits_ahead} ahead / {commits_behind} behind {git_ref})'
            )

        if force:
            deps_status = 'forced clean rebuild (--force: image + cargo/npm volumes wiped)'
        elif deps_will_change:
            deps_status = 'will trigger fresh build (Dockerfile or requirements changed)'
        elif is_rollback or commits_ahead > 0:
            # Probe measured the *current* (pre-pull) Dockerfile/requirements,
            # so a backward jump can still trigger a rebuild we can't predict
            # without actually pulling. Be honest about the unknown.
            deps_status = 'unknown until pull (older ref may differ)'
        else:
            deps_status = 'cache valid (no rebuild)'

        if force:
            container_status = 'will restart (forced clean rebuild)'
            est = '~6 min (fresh build)'
        elif commits_behind == 0 and commits_ahead == 0 and not deps_will_change:
            container_status = 'no restart needed'
            est = '~5s'
        elif deps_will_change or is_rollback or commits_ahead > 0:
            # Rollback / branch-switch likely flips at least some COPY layers
            # in the Dockerfile cache; assume a real build.
            container_status = 'will restart'
            est = '~6 min (fresh build possible)'
        else:
            container_status = 'will restart'
            est = '~90s (cached build)'

        click.secho('Update preview', fg='blue', bold=True)
        click.echo(f'  Box:        {box_name} ({resolved_box})')
        click.echo(f'  Current:    {current_box_version}')
        click.echo(f'  Target:     {target_version}')
        click.echo(f'  Code:       {code_status}')
        click.echo(f'  Deps:       {deps_status}')
        click.echo(f'  Container:  {container_status}')
        click.echo(f'  Estimated:  {est}')
        click.echo()

        will_change = force or commits_behind != 0 or commits_ahead != 0 or deps_will_change
        if will_change:
            click.echo('Run without --check to apply.')
            ctx.exit(1)
        click.echo('Nothing to do.')
        ctx.exit(0)

    if needs_pull:
        # Step 4: Update the git repo — sparse-checkout fixups, checkout, and
        # reset all chained into a single SSH call.
        if progress:
            progress.update("Rolling back..." if is_rollback else "Pulling updates...")
        log(f'{"Rolling back to" if is_rollback else "Pulling"} {git_ref}...', nl=False)

        # `git checkout -f` so a prior flatten artifact (a root-level tracked
        # file overwritten by box/<same-name>) doesn't block the switch.
        # Observed on one box: flatten of a branch that had both root README.md
        # and box/README.md left the root copy looking modified, and
        # `git checkout main` failed with "local changes would be
        # overwritten". Editing the on-box git tree by hand is not a
        # supported workflow, so discarding such "modifications" is safe.
        # The `cli/__init__.py` add is best-effort: cone-mode sparse-checkout
        # (default since git 2.36) rejects single-file patterns with
        # "fatal: 'cli/__init__.py' is not a directory". The pre-batching
        # version of this code happened to run in a separate SSH call whose
        # exit was never checked, so the failure was silently swallowed; the
        # `|| true` here preserves that behavior so a newer-git box (e.g.
        # one box at 2.43) doesn't abort the whole pull. udev_rules is a
        # directory and never hits this, so it stays strict.
        pull_script = (
            'cd ~/box && '
            '{ git sparse-checkout list | grep -q "^udev_rules$" || '
            'git sparse-checkout add udev_rules; } && '
            '{ git sparse-checkout list | grep -q "^modprobe_d$" || '
            'git sparse-checkout add modprobe_d 2>/dev/null || true; } && '
            '{ git sparse-checkout list | grep -q "^cli/__init__.py$" || '
            'git sparse-checkout add cli/__init__.py 2>/dev/null || true; } && '
            f'git checkout -f {target_version} && '
            f'git reset --hard {git_ref}'
        )
        result = run_ssh_command_with_output(pull_script)
        if result.returncode != 0:
            if progress:
                progress.finish(success=False)
            log_status('FAILED', 'red')
            log_error(f'Error: Failed to update box code to {target_version}')
            for stream in (result.stderr, result.stdout):
                if stream and stream.strip():
                    click.echo(stream.strip(), err=True)
            ctx.exit(1)
        log_status('OK', 'green')

        # `git reset --hard` prints "HEAD is now at <hash> <subject>" — reuse
        # that line instead of a separate `git log` round-trip.
        if verbose:
            for line in result.stdout.splitlines():
                if line.startswith('HEAD is now at'):
                    click.echo(f'  New version:   {line[len("HEAD is now at "):].strip()}')
                    break
        needs_flatten = True  # After a pull, always flatten.
    else:
        if progress:
            progress.update("Already up to date")

    # Flatten the sparse-checkout layout (box/ -> root) when needed. The cp is
    # best-effort (the box may already be flat), but the post-flatten verify
    # is fatal: a silently-failed flatten used to let the docker build proceed
    # against missing files — an image that passed `docker ps` but failed at
    # runtime, one of the documented "had to run update 3 times" modes. The
    # `{ ...; true; }` keeps a cp failure non-fatal so the verify always runs
    # and is what actually decides. Flatten + verify share one SSH call.
    if needs_flatten:
        if progress:
            progress.update("Flattening structure...")
        log('Flattening layout...', nl=False)
        flatten_script = (
            'cd ~/box && '
            '{ if [ -d box ]; then shopt -s dotglob && cp -rf box/* . && rm -rf box; fi; true; } && '
            'test -f ~/box/lager/box_http_server.py && '
            'test -f ~/box/lager/docker/box.Dockerfile'
        )
        result = run_ssh_command_with_output(flatten_script)
        if result.returncode != 0:
            if progress:
                progress.finish(success=False)
            log_status('FAILED', 'red')
            log_error('Error: Source files missing after flatten step')
            click.echo('Expected ~/box/lager/box_http_server.py and ~/box/lager/docker/box.Dockerfile.', err=True)
            click.echo('The sparse-checkout layout on the box is inconsistent; try a fresh `lager install`.', err=True)
            ctx.exit(1)
        log_status('OK', 'green')

    # Docker-build cache state. The upfront probe measured the *pre-pull*
    # tree; if code actually changed we recompute the new hash against the
    # freshly pulled Dockerfile/requirements (one round-trip). When nothing
    # was pulled or flattened the probe's value is still accurate, so skip it.
    # `stored_build_hash` never changes mid-run, so the probe's value always
    # stands. This decides `must_wipe_image` and feeds the early-exit below.
    stored_build_hash = facts.get('BUILD_HASH_STORED', '')
    if needs_pull or needs_flatten:
        new_build_hash = _read_build_hash(run_ssh_command_with_output)
    else:
        new_build_hash = facts.get('BUILD_HASH_NEW', '')
    hash_mismatch = (
        bool(new_build_hash) and bool(stored_build_hash)
        and new_build_hash != stored_build_hash
    )
    # `--force` requests a clean rebuild: wipe the cached image and the
    # cargo/npm volumes so a prior failed/partial run can't leave a stale layer
    # or half-installed toolchain behind on the retry.
    must_wipe_image = hash_mismatch or force

    # Step 5: udev rules. The probe already located the source dir and diffed
    # its 99-instrument.rules against the installed copy, so we only touch the
    # box when an install/update is actually needed.
    if progress:
        progress.update("Checking udev rules...")
    log('Checking udev rules...', nl=False)

    udev_src_path = facts.get('UDEV_SRC_PATH', '')
    if not udev_src_path:
        log_status('SKIPPED (source dir missing)', 'yellow')
        if verbose:
            click.echo('  The udev_rules directory is not in the sparse checkout.', err=True)
    elif facts.get('UDEV_SRC_RULES') != '1':
        log_status('SKIPPED (rules file missing)', 'yellow')
        if verbose:
            click.echo(f'  {udev_src_path}/99-instrument.rules not found.', err=True)
    elif facts.get('UDEV_IN_SYNC') == '1':
        log_status('OK (already current)', 'green')
    else:
        log_status('update needed', 'yellow')

        # The `&&` chain means a non-zero exit already implies one of the
        # sudo steps failed, so no separate post-install verify is needed.
        # The "lager" group is what the instrument rules grant device access
        # to (GROUP="lager"); the getent guard keeps the groupadd off the
        # common path so provisioned boxes stay within the passwordless
        # sudoers grant and see no password prompt.
        install_cmd = (
            f'cp {udev_src_path}/99-instrument.rules /tmp/ && '
            '{ getent group lager >/dev/null || sudo /usr/sbin/groupadd lager; } && '
            'sudo /bin/cp /tmp/99-instrument.rules /etc/udev/rules.d/ && '
            'sudo /bin/chmod 644 /etc/udev/rules.d/99-instrument.rules && '
            'sudo /usr/bin/udevadm control --reload-rules && '
            'sudo /usr/bin/udevadm trigger && '
            'sudo /bin/rm -f /tmp/99-instrument.rules'
        )

        # Interactive mode so a sudo password prompt can appear. pause()/
        # resume() stop the 1s progress re-render from overwriting the prompt.
        if not verbose and progress:
            progress.pause()
            click.echo('Installing udev rules (may require sudo password)...')
        elif verbose:
            click.echo()

        result = run_ssh_command_interactive(install_cmd, allow_sudo_prompt=True)

        if not verbose and progress:
            progress.resume()
        elif verbose:
            click.echo()

        log('Installing udev rules...', nl=False)
        if result.returncode == 0:
            log_status('OK', 'green')
        else:
            log_status('FAILED', 'red')
            if verbose:
                click.echo('  Could not install udev rules — likely a sudoers permission issue.', err=True)
                click.echo(f'  Manual fix: ssh {ssh_host}, then:', err=True)
                click.echo('    sudo groupadd -f lager', err=True)
                click.echo(f'    sudo cp {udev_src_path}/99-instrument.rules /etc/udev/rules.d/', err=True)
                click.echo('    sudo udevadm control --reload-rules && sudo udevadm trigger', err=True)

    # Step 5b: modprobe.d blacklists (0.20.0+: usbtmc blacklist for USB-TMC
    # instrument drivers). Same shape as the udev step above — probe diffed
    # the source vs installed; only touch the box when an install is needed.
    # After install, try `modprobe -r usbtmc`; if a USB-TMC instrument is in
    # use the unload fails with EBUSY and we note that a reboot is required.
    if progress:
        progress.update("Checking modprobe.d blacklists...")
    log('Checking modprobe.d blacklists...', nl=False)

    mp_src_path = facts.get('MODPROBE_SRC_PATH', '')
    # The probe runs BEFORE the git pull. When modprobe_d first lands on a
    # box (i.e. this very update), the pre-pull probe correctly reports it
    # missing — but now it exists post-pull/flatten. Re-detect by checking
    # the canonical post-flatten path and the pre-flatten fallback.
    if not mp_src_path:
        recheck = run_ssh_command_with_output(
            'if [ -d ~/box/modprobe_d ]; then echo ~/box/modprobe_d; '
            'elif [ -d ~/box/box/modprobe_d ]; then echo ~/box/box/modprobe_d; fi'
        )
        mp_src_path = (recheck.stdout or '').strip()
        # Re-check whether the file is in sync against the rediscovered path.
        if mp_src_path:
            conf_file = f'{mp_src_path}/blacklist-usbtmc.conf'
            confs_check = run_ssh_command_with_output(
                f'test -f {conf_file} && echo 1 || echo 0'
            )
            facts['MODPROBE_SRC_CONFS'] = (confs_check.stdout or '').strip()
            sync_check = run_ssh_command_with_output(
                f'diff -q {conf_file} /etc/modprobe.d/blacklist-usbtmc.conf '
                '>/dev/null 2>&1 && echo 1 || echo 0'
            )
            facts['MODPROBE_IN_SYNC'] = (sync_check.stdout or '').strip()
            usbtmc_check = run_ssh_command_with_output(
                'lsmod 2>/dev/null | grep -q "^usbtmc" && echo 1 || echo 0'
            )
            facts['USBTMC_LOADED'] = (usbtmc_check.stdout or '').strip()
    if not mp_src_path:
        log_status('SKIPPED (source dir missing)', 'yellow')
        if verbose:
            click.echo('  The modprobe_d directory is not in the sparse checkout.', err=True)
    elif facts.get('MODPROBE_SRC_CONFS') != '1':
        log_status('SKIPPED (blacklist file missing)', 'yellow')
        if verbose:
            click.echo(f'  {mp_src_path}/blacklist-usbtmc.conf not found.', err=True)
    elif facts.get('MODPROBE_IN_SYNC') == '1':
        log_status('OK (already current)', 'green')
        # Even if the file is in sync, the kernel module may still be loaded
        # from before the blacklist was installed. Try to unload silently.
        if facts.get('USBTMC_LOADED') == '1':
            run_ssh_command_with_output('sudo /sbin/modprobe -r usbtmc 2>/dev/null || true')
    else:
        log_status('update needed', 'yellow')

        install_cmd = (
            f'cp {mp_src_path}/blacklist-usbtmc.conf /tmp/ && '
            'sudo /bin/cp /tmp/blacklist-usbtmc.conf /etc/modprobe.d/ && '
            'sudo /bin/chmod 644 /etc/modprobe.d/blacklist-usbtmc.conf && '
            'sudo /bin/rm -f /tmp/blacklist-usbtmc.conf; '
            # Try to unload immediately so the change takes effect without a
            # reboot. Fails with EBUSY if a USB-TMC instrument is in use; we
            # tolerate that and note a reboot is needed.
            'if lsmod | grep -q "^usbtmc"; then '
            '  sudo /sbin/modprobe -r usbtmc 2>/dev/null && '
            '  echo "LAGER_MP_UNLOAD=OK" || echo "LAGER_MP_UNLOAD=BUSY"; '
            'else echo "LAGER_MP_UNLOAD=NOT_LOADED"; fi'
        )

        if not verbose and progress:
            progress.pause()
            click.echo('Installing modprobe.d blacklists (may require sudo password)...')
        elif verbose:
            click.echo()

        result = run_ssh_command_interactive(install_cmd, allow_sudo_prompt=True)

        if not verbose and progress:
            progress.resume()
        elif verbose:
            click.echo()

        log('Installing modprobe.d blacklists...', nl=False)
        if result.returncode == 0:
            log_status('OK', 'green')
            if verbose:
                if 'LAGER_MP_UNLOAD=BUSY' in (result.stdout or ''):
                    click.echo('  Note: usbtmc still loaded (instrument in use); reboot the box to fully apply.', err=True)
                elif 'LAGER_MP_UNLOAD=OK' in (result.stdout or ''):
                    click.echo('  usbtmc kernel module unloaded; blacklist now in effect.')
        else:
            log_status('FAILED', 'red')
            if verbose:
                click.echo('  Could not install modprobe.d blacklist — likely a sudoers permission issue.', err=True)
                click.echo(f'  Manual fix: ssh {ssh_host}, then:', err=True)
                click.echo(f'    sudo cp {mp_src_path}/blacklist-usbtmc.conf /etc/modprobe.d/', err=True)
                click.echo('    sudo modprobe -r usbtmc  # optional: takes effect immediately', err=True)

    # Step 6: sudoers ownership. /etc/sudoers.d/lagerdata-udev must be
    # root-owned or sudo refuses it; the probe gave us the owner uid.
    if progress:
        progress.update("Checking sudoers...")
    log('Checking sudoers ownership...', nl=False)

    sudoers_owner = facts.get('SUDOERS_OWNER', '')
    if sudoers_owner in ('NOTFOUND', 'UNKNOWN', ''):
        log_status('SKIPPED (not present)', 'yellow')
    elif sudoers_owner == '0':
        log_status('OK', 'green')
    else:
        log_status(f'fixing (owned by uid {sudoers_owner})', 'yellow')

        if not verbose and progress:
            progress.pause()
            click.echo('Fixing sudoers ownership (may require sudo password)...')
        elif verbose:
            click.echo()

        fix_result = run_ssh_command_interactive(
            'sudo chown root:root /etc/sudoers.d/lagerdata-udev',
            allow_sudo_prompt=True
        )

        if not verbose and progress:
            progress.resume()
        elif verbose:
            click.echo()

        log('Fixing sudoers ownership...', nl=False)
        if fix_result.returncode == 0:
            log_status('OK', 'green')
        else:
            log_status('FAILED', 'red')
            if verbose:
                click.echo('  Warning: could not fix sudoers ownership; sudo may not work correctly.', err=True)

    # Step 7: passwordless sudo for `lager box config apply`.
    #
    # `lager box config apply` needs root on the host for apt-get install,
    # sysctl writes, and mount-path mkdir/chown — all over BatchMode SSH
    # where sudo can't prompt. The rule grants narrow NOPASSWD for exactly
    # those operations. The probe ran the functional check (marker file
    # present + `sudo -n apt-get` actually works), so we only bootstrap when
    # it came back negative. Runs on every update so existing boxes
    # gradually pick up the rule; idempotent.
    if progress:
        progress.update("Checking box-config sudoers...")
    log('Checking box-config sudoers...', nl=False)

    if facts.get('BOXCFG_SUDOERS_OK') == '1':
        log_status('OK', 'green')
    else:
        log_status('needs bootstrap', 'yellow')

        if not verbose and progress:
            progress.pause()
            click.echo('Installing box-config sudoers rule (may require sudo password)...')
        elif verbose:
            click.echo()

        boxcfg_sudoers_cmd = (
            "printf '%s\\n' "
            "'lagerdata ALL=(root) NOPASSWD: SETENV: /usr/bin/apt-get' "
            "'lagerdata ALL=(root) NOPASSWD: /bin/mkdir, /bin/chown, "
            "/usr/sbin/sysctl --system, /sbin/sysctl --system, "
            "/usr/bin/tee /etc/sysctl.d/99-lager-box-config.conf, "
            "/bin/rm -f /etc/sysctl.d/99-lager-box-config.conf, "
            "/bin/cp /etc/lager/box_config.applied.json /etc/lager/box_config.json' "
            "| sudo tee /etc/sudoers.d/lager-box-config >/dev/null "
            "&& sudo chmod 440 /etc/sudoers.d/lager-box-config "
            "&& sudo touch /etc/lager/.boxcfg-sudoers-v2 "
            "&& sudo chmod 644 /etc/lager/.boxcfg-sudoers-v2"
        )

        boxcfg_install_result = run_ssh_command_interactive(
            boxcfg_sudoers_cmd,
            allow_sudo_prompt=True,
        )

        if not verbose and progress:
            progress.resume()
        elif verbose:
            click.echo()

        log('Installing box-config sudoers...', nl=False)
        if boxcfg_install_result.returncode == 0:
            # `sudo tee` succeeds even if the sudoers content is malformed,
            # so this functional re-check is genuinely needed (unlike the
            # udev path above, where the `&&` chain is self-verifying).
            boxcfg_verify = run_ssh_command_with_output(
                "sudo -n DEBIAN_FRONTEND=noninteractive apt-get --version >/dev/null 2>&1"
            )
            if boxcfg_verify.returncode == 0:
                log_status('OK', 'green')
            else:
                log_status('installed but not verified', 'yellow')
                if verbose:
                    click.echo(
                        '  Warning: rule installed but `sudo -n apt-get` still requires a password. '
                        'Check /etc/sudoers.d/lager-box-config syntax on the box.',
                        err=True,
                    )
        else:
            log_status('FAILED', 'yellow')
            if verbose:
                click.echo(
                    '  Warning: box-config sudoers rule could not be installed. '
                    '`lager box config apply` will need manual sudoers setup on this box.',
                    err=True,
                )

    # No git updates, no box/→root flatten work, and the docker-build inputs
    # match what's already cached: a second consecutive `lager update` would
    # otherwise still stop every Docker container on the box, remove them, and
    # rebuild — redundant after a successful run and a common source of flaky
    # behavior. `hash_mismatch` participates here so the auto-invalidation
    # feature also fires on a deps-only change (Dockerfile/requirements moved
    # but code is in sync); previously this branch ignored the hash and the
    # rebuild silently never happened.
    #
    # `--force` deliberately skips this early-exit: a box whose previous update
    # failed can read as "in sync" here (git fetched fine, hash matches) even
    # though its container never came up, so the user needs a way to push the
    # rebuild through regardless.
    if (
        git_sync_confirmed
        and not needs_pull
        and not needs_flatten
        and not hash_mismatch
        and not force
    ):
        import re as _re

        # Prefer the box's source-declared `__version__` over the CLI
        # version. Reads `cli/__init__.py` at HEAD via `git show`, which
        # works even when the file isn't in the working tree (cone-mode
        # boxes). HEAD is unchanged in this branch (no pull), so one read
        # suffices.
        _vp = _re.match(r'^v?(\d+\.\d+\.\d+)$', target_version)
        if _vp:
            _box_v = _vp.group(1)
        else:
            _src_v = _read_box_source_version(run_ssh_command_with_output)
            _box_v = _src_v if _src_v else cli_version

        # Reconcile /etc/lager/version on the box with the local cache.
        # Previously this branch only updated the local ~/.lager file, so if
        # the box's /etc/lager/version was stale (e.g. an earlier update
        # exited via this same path before the file was ever written) the
        # next `lager hello` would surface the stale version and the user
        # would re-run `lager update` thinking the previous one didn't take.
        if not write_box_version_file(_box_v):
            if progress:
                progress.finish(success=False)
            log_error('Error: Failed to reconcile /etc/lager/version on the box')
            click.echo('The local cache shows the box is up to date, but writing the version file on the box failed.', err=True)
            click.echo(f'Manually fix with: ssh {ssh_host} "echo \\"{_box_v}|{cli_version}\\" > /etc/lager/version"', err=True)
            ctx.exit(1)

        if progress:
            progress.finish(success=True)
        if _box_v and box:
            update_box_version(box, _box_v)
        click.echo()
        click.secho(
            f'{box_name} is already at version {_box_v} ({target_version})',
            fg='green', bold=True,
        )
        click.echo()
        ctx.exit(0)

    # Acquire the auto-lock now — the first action below stops the lager
    # docker container, which would clobber a `lager python` test that's
    # mid-run. Held through the rest of `_update_logic` (container
    # rebuild + restart + health probe + J-Link install + version write)
    # and released on the final success message OR via the atexit hook
    # registered inside `auto_lock_acquire_for_command` for any
    # SystemExit / signal path. The imperative variant (not `with`) is
    # used here because re-indenting ~450 lines of branchy update logic
    # would be far riskier than registering an atexit release.
    _release_update_lock = auto_lock_acquire_for_command(
        resolved_box, box_name or resolved_box, 'update',
    )

    # Step 8: Stop containers
    if progress:
        progress.update("Stopping containers...")
    log('Stopping containers...', nl=False)

    run_ssh_command_with_output(
        'docker stop lager pigpio 2>/dev/null || true && '
        'docker rm lager pigpio 2>/dev/null || true',
        timeout_secs=30
    )
    log_status('OK', 'green')

    # Wipe the cached Docker image when build inputs changed (`hash_mismatch`)
    # so a pip-dependency change in the Dockerfile doesn't reuse a stale layer.
    # Also wipe the cargo/npm persistence volumes (mounted by start_box.sh) —
    # a Dockerfile change can move the rust/node toolchain, and a stale volume
    # would shadow the new image's tree with the old one. `|| true` keeps both
    # the first-run case (volumes don't exist yet) and the "in use" case
    # non-fatal; the prior `docker rm` already detached this container.
    if must_wipe_image:
        # `force` short-circuits the wipe independently of the build-hash, so
        # report the actual reason rather than always claiming a hash change.
        _wipe_reason = '--force' if force and not hash_mismatch else 'build inputs changed'
        if progress:
            progress.update("Removing cached image...")
        log(f'Removing cached image ({_wipe_reason})...', nl=False)

        run_ssh_command_with_output(
            'docker rmi lager 2>/dev/null || true; '
            'docker volume rm lager-cargo lager-npm-global 2>/dev/null || true',
            timeout_secs=30
        )
        log_status('OK', 'green')

    # Step 9: Rebuild Docker container (the slow part)
    if progress:
        progress.update("Building container...")
    log('Building container (this may take several minutes)...')

    ssh_cmd = ['ssh']
    if use_explicit_key:
        ssh_cmd.extend(['-i', key_file])
    if not use_interactive_ssh:
        ssh_cmd.extend(['-o', 'BatchMode=yes'])
    ssh_cmd.extend(_ssh_mux_opts)
    ssh_cmd.extend([ssh_host,
         'cd ~/box/lager && '
         'docker build -f docker/box.Dockerfile -t lager .'])

    build_output_lines = []
    if verbose:
        # Stream output in verbose mode
        process = subprocess.Popen(
            ssh_cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1
        )
        if process.stdout:
            for line in process.stdout:
                click.echo(f'    {line}', nl=False)
                build_output_lines.append(line.rstrip())
        return_code = process.wait(timeout=600)
    else:
        # Silent mode - capture output for error reporting
        process = subprocess.Popen(
            ssh_cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True
        )
        # Read and store output for potential error reporting
        if process.stdout:
            for line in process.stdout:
                build_output_lines.append(line.rstrip())
        return_code = process.wait(timeout=600)

    if return_code != 0:
        if progress:
            progress.finish(success=False)
        log_error('Error: Failed to rebuild Docker container')
        # Show last 20 lines of build output for debugging
        if build_output_lines:
            click.echo()
            click.secho("Docker build output (last 20 lines):", fg='yellow', err=True)
            for line in build_output_lines[-20:]:
                click.echo(f"  {line}", err=True)
            click.echo()
            # Detect common Docker build errors
            full_output = "\n".join(build_output_lines)
            if "No space left on device" in full_output:
                click.secho("Hint: Disk space is full on the box. Run: ssh lagerdata@[BOX_NAME] 'docker system prune -af'", fg='yellow', err=True)
            elif "network" in full_output.lower() and ("timeout" in full_output.lower() or "error" in full_output.lower()):
                click.secho("Hint: Network issue during build. Check box internet connectivity.", fg='yellow', err=True)
            elif "permission denied" in full_output.lower():
                click.secho("Hint: Permission issue. Check Docker daemon is running and user has access.", fg='yellow', err=True)
        ctx.exit(1)
    if verbose:
        click.secho('  Build complete', fg='green')

    # Record the build-inputs hash so the next run can decide whether to
    # invalidate the cache automatically. Best-effort — a failure here just
    # means the next update may rebuild unnecessarily.
    if new_build_hash:
        run_ssh_command_with_output(
            f'echo "{new_build_hash}" > /etc/lager/build-hash',
            timeout_secs=15,
        )

    # Reclaim disk from *superseded* images while preserving the build cache.
    #
    # `prune -f` removes only DANGLING images — the now-untagged orphans left
    # behind when this build retagged `lager`. The layers backing the current
    # `lager` image are NOT dangling, so they survive and serve as cache for
    # the next update. Because box.Dockerfile copies source code only after the
    # heavy apt/pip/rust/nrfutil layers, a code-only update then reuses all of
    # that work and finishes in ~1-2 min instead of rebuilding from scratch.
    #
    # The previous `prune -af --filter "until=24h"` deleted *all* unused images
    # (the `-a`), which wiped exactly those cache layers and forced a full
    # from-scratch rebuild (40+ pip packages, rust toolchain) on every run.
    if progress:
        progress.update("Cleaning up images...")
    log('Cleaning up old images...', nl=False)
    run_ssh_command_with_output(
        'docker image prune -f',
        timeout_secs=30
    )
    log_status('OK', 'green')

    # Step 10: Create the on-box directories needed before the container
    # starts — /etc/lager (start_box.sh writes here) and the customer-binaries
    # mount point (the container runs as www-data and writes uploaded binaries
    # there; creating it 777 up front keeps docker from auto-creating it
    # root-owned at mount time). Batched into one call. /etc/lager is fatal on
    # failure; customer-binaries is best-effort (`{ ...; } || true`).
    # Note: subpaths *inside* customer-binaries (e.g. the
    # ``openocd/flash-loaders/`` tree consumed by da1469x_loader.py) are
    # created by start_box.sh on every container start — they don't need
    # pre-creation here because they're not mount targets, only paths
    # inside an existing mount.
    if progress:
        progress.update("Setting up directories...")
    log('Setting up directories...', nl=False)

    # Full paths match the sudoers whitelist in the deployment script; mkdir
    # and chmod are idempotent and passwordless via sudoers.
    dirs_result = run_ssh_command_with_output(
        'sudo /bin/mkdir -p /etc/lager && sudo /bin/chmod 777 /etc/lager && '
        '{ mkdir -p ~/third_party/customer-binaries && '
        'chmod 777 ~/third_party/customer-binaries; } || true',
        timeout_secs=30
    )
    if dirs_result.returncode != 0:
        if progress:
            progress.finish(success=False)
        log_status('FAILED', 'red')
        log_error('Error: Failed to create /etc/lager on the box')
        click.echo('This is usually a sudo permission issue. SSH into the box and run:', err=True)
        click.echo(f'  ssh {ssh_host}', err=True)
        click.echo('  sudo mkdir -p /etc/lager && sudo chmod 777 /etc/lager', err=True)
        click.echo('Then run `lager box update` again.', err=True)
        ctx.exit(1)
    log_status('OK', 'green')

    # Write the version file BEFORE the container restart (SSH is stable here).
    # A version tag (v0.3.14 / 0.3.14) is used directly. For a branch target
    # we ask the box for the closest preceding `vX.Y.Z` tag at HEAD via
    # `git describe`, which reflects the actual code on disk — not the CLI's
    # own version, which used to make `Storing version... OK (0.18.3)` print
    # after a rollback to a v0.18.2 ref. Falls back to the CLI version only
    # when the box has no tags at all (brand-new repo) or the closest tag
    # isn't semver-shaped (downstream `compare_versions` parses `X.Y.Z` ints).
    import re
    version_pattern = re.match(r'^v?(\d+\.\d+\.\d+)$', target_version)
    if version_pattern:
        box_cli_version = version_pattern.group(1)
    else:
        # Branch target — read `__version__` from `cli/__init__.py` on the
        # box's post-pull HEAD. The file is the source of truth (releases
        # bump it before tagging), so this gives the right answer for both
        # tagged release commits and untagged work-in-progress commits.
        # `git show HEAD:cli/__init__.py` works even on cone-mode boxes
        # where the file isn't in the working tree.
        src_version = _read_box_source_version(run_ssh_command_with_output)
        box_cli_version = src_version if src_version else cli_version

    if box_cli_version:
        if progress:
            progress.update("Storing version...")
        log('Storing version...', nl=False)

        if not write_box_version_file(box_cli_version):
            if progress:
                progress.finish(success=False)
            log_status('FAILED', 'red')
            log_error('Error: Failed to write version file to /etc/lager/version')
            click.echo('The code was updated but the version file could not be written.', err=True)
            click.echo()
            click.echo('Manually fix with:', err=True)
            click.echo(f'  ssh {ssh_host} "echo \\"{box_cli_version}|{cli_version}\\" | sudo tee /etc/lager/version"', err=True)
            ctx.exit(1)
        log_status(f'OK ({box_cli_version})', 'green')

    # Step 11: Start container
    if progress:
        progress.update("Starting container...")
    log('Starting lager container...', nl=False)

    try:
        # LAGER_SKIP_BUILD=1: Step 9 already built the `lager` image (with full
        # build-error reporting), so tell start_box.sh to skip its own
        # redundant `docker build` and go straight to `docker run`. Without
        # this the image was built twice per update. The remaining timeout
        # budget now covers `docker run` + any user pip/cargo/npm installs
        # (box_config), not a from-scratch build.
        result = run_ssh_command_with_output(
            'cd ~/box && chmod +x start_box.sh && LAGER_SKIP_BUILD=1 ./start_box.sh',
            timeout_secs=600
        )

        if result.returncode != 0:
            if progress:
                progress.finish(success=False)
            log_status('FAILED', 'red')
            log_error('Error: Failed to start lager container')
            # Show error output even in non-verbose mode so users can see what went wrong
            if result.stdout:
                click.echo('Container output:', err=True)
                click.echo(result.stdout, err=True)
            if result.stderr:
                click.echo(result.stderr, err=True)
            ctx.exit(1)
        log_status('OK', 'green')
    except subprocess.TimeoutExpired:
        if progress:
            progress.finish(success=False)
        log_error('Error: Container startup timed out after 10 minutes')
        click.echo()
        click.echo('The container is taking too long to start. This could be because:', err=True)
        click.echo('  1. The box is slow or overloaded', err=True)
        click.echo('  2. Docker is pulling/building a large image', err=True)
        click.echo('  3. The startup script is hanging', err=True)
        click.echo()
        click.echo('Try:', err=True)
        click.echo(f'  ssh lagerdata@{resolved_box} "docker logs lager"', err=True)
        click.echo(f'  ssh lagerdata@{resolved_box} "docker ps -a"', err=True)
        ctx.exit(1)

    # Wait for the on-box services to become reachable. Previously this was a
    # blind `time.sleep(5)`; on slower boxes the Python service on port 5000
    # could still be initializing past that window, so subsequent steps would
    # race against an unready service and the user would re-run `lager update`
    # thinking the previous one didn't take. Poll /health (defined in
    # box/lager/python/service.py) with a 60s ceiling.
    if progress:
        progress.update("Waiting for services...")
    log('Waiting for box services...', nl=False)
    if not wait_for_box_ready(resolved_box, timeout_s=60):
        if progress:
            progress.finish(success=False)
        log_status('FAILED', 'red')
        log_error('Error: Box services did not respond within 60s after restart')
        click.echo(f'The lager container is running but http://{resolved_box}:5000/health did not return 200.', err=True)
        click.echo('Investigate with:', err=True)
        click.echo(f'  ssh {ssh_host} "docker logs lager --tail 50"', err=True)
        click.echo(f'  ssh {ssh_host} "docker ps"', err=True)
        ctx.exit(1)
    log_status('OK', 'green')

    # Step 12: Verify the lager container is up, and check J-Link presence —
    # one SSH call returns both. The customer-binaries directory was already
    # created back in the "Setting up directories" step.
    if progress:
        progress.update("Verifying...")
    log('Verifying...', nl=False)

    verify_result = run_ssh_command_with_output(
        "docker ps --filter 'name=lager' --format '{{.Names}}\t{{.Status}}'; "
        "echo '---LAGER-JLINK---'; "
        "find ~/third_party -name JLinkGDBServerCLExe 2>/dev/null | head -n 1"
    )
    container_lines = []
    jlink_path = ''
    _past_marker = False
    for line in verify_result.stdout.splitlines():
        if line.strip() == '---LAGER-JLINK---':
            _past_marker = True
            continue
        if _past_marker:
            if line.strip():
                jlink_path = line.strip()
        elif line.strip():
            container_lines.append(line.strip())

    if container_lines:
        log_status('OK', 'green')
    else:
        log_status('WARNING (lager container not detected)', 'yellow')

    if verbose and container_lines:
        click.echo()
        click.secho('Container status:', fg='blue', bold=True)
        for line in container_lines:
            click.echo(f'  {line}')
        click.echo()

    # Step 13: Install J-Link when the verify call above didn't find it.
    # Failure here is non-fatal — the box falls back to pyOCD.
    if jlink_path:
        log('Checking J-Link...', nl=False)
        log_status('OK (already installed)', 'green')
    else:
        if progress:
            progress.update("Installing J-Link...")
        log('Installing J-Link (downloading from segger.com)...', nl=False)

        # Installation script run on the box.
        install_script = """#!/bin/bash
set -e

USERNAME="${USER}"
THIRD_PARTY_DIR="/home/${USERNAME}/third_party"

# Check if already installed
if find "$THIRD_PARTY_DIR" -name JLinkGDBServerCLExe 2>/dev/null | grep -q .; then
    echo "J-Link already installed"
    exit 0
fi

mkdir -p "$THIRD_PARTY_DIR"
cd /tmp

echo "Downloading J-Link debian package..."
DEB_URL="https://www.segger.com/downloads/jlink/JLink_Linux_x86_64.deb"

if command -v wget &> /dev/null; then
    wget --post-data="accept_license_agreement=accepted" -q --show-progress -O JLink.deb "$DEB_URL" 2>&1 || \\
        wget -q --show-progress -O JLink.deb "$DEB_URL" 2>&1
elif command -v curl &> /dev/null; then
    curl -L -d "accept_license_agreement=accepted" -# -o JLink.deb "$DEB_URL" 2>&1 || \\
        curl -L -# -o JLink.deb "$DEB_URL" 2>&1
else
    echo "Error: Neither wget nor curl available"
    exit 1
fi

if [ ! -f JLink.deb ] || [ ! -s JLink.deb ]; then
    echo "Download failed"
    exit 1
fi

echo "Extracting J-Link..."

# Use dpkg-deb if available (most reliable), otherwise use ar
if command -v dpkg-deb &> /dev/null; then
    dpkg-deb -x JLink.deb extracted
    if [ -d extracted/opt/SEGGER ]; then
        JLINK_DIR=$(find extracted/opt/SEGGER -maxdepth 1 -type d -name "JLink*" | head -n 1)
        if [ -n "$JLINK_DIR" ]; then
            mv "$JLINK_DIR" "$THIRD_PARTY_DIR/"
            echo "J-Link installed to $THIRD_PARTY_DIR/$(basename $JLINK_DIR)"
            rm -rf extracted JLink.deb
            echo "Installation complete"
            exit 0
        fi
    fi
    echo "Error: Could not find J-Link in package"
    rm -rf extracted JLink.deb
    exit 1
elif command -v ar &> /dev/null; then
    ar x JLink.deb

    if [ -f data.tar.xz ]; then
        tar xJf data.tar.xz ./opt/SEGGER 2>&1 | grep -v "Cannot utime|Cannot change mode" || true
    elif [ -f data.tar.gz ]; then
        tar xzf data.tar.gz ./opt/SEGGER 2>&1 | grep -v "Cannot utime|Cannot change mode" || true
    else
        echo "Error: Package format not recognized"
        exit 1
    fi

    if [ -d opt/SEGGER ]; then
        JLINK_DIR=$(find opt/SEGGER -maxdepth 1 -type d -name "JLink*" | head -n 1)
        if [ -n "$JLINK_DIR" ]; then
            mv "$JLINK_DIR" "$THIRD_PARTY_DIR/"
            echo "J-Link installed to $THIRD_PARTY_DIR/$(basename $JLINK_DIR)"
        else
            echo "Error: J-Link directory not found in package"
            exit 1
        fi
    else
        echo "Error: Package extraction failed"
        exit 1
    fi

    cd /tmp
    rm -f JLink.deb control.tar.* data.tar.* debian-binary
    rm -rf opt etc usr var

    echo "Installation complete"
    exit 0
else
    echo "Error: Neither dpkg-deb nor ar available for extracting .deb package"
    echo "Please install dpkg or binutils package"
    exit 1
fi
"""

        # Copy install script to box and execute
        install_result = run_ssh_command_with_output(
            f'cat > /tmp/install_jlink.sh << \'EOF\'\n{install_script}\nEOF\n'
            'chmod +x /tmp/install_jlink.sh && '
            '/tmp/install_jlink.sh && '
            'rm /tmp/install_jlink.sh',
            timeout_secs=180
        )

        if install_result.returncode == 0:
            log_status('OK', 'green')
            if verbose and install_result.stdout:
                for line in install_result.stdout.strip().split('\n'):
                    click.echo(f'    {line}')
        else:
            log_status('FAILED (will use pyOCD)', 'yellow')
            if verbose:
                if install_result.stderr:
                    click.echo(f'    Error: {install_result.stderr.strip()}', err=True)
                click.echo()
                click.echo('    J-Link download failed. You can either:')
                click.echo(f'      1. Copy from another box: deployment/copy_jlink_from_box.sh [SOURCE_BOX] {box_name}')
                click.echo('      2. Manually download from https://www.segger.com/downloads/jlink/')
                click.echo('      3. Use pyOCD (already installed, works with most debug probes)')
                click.echo()

    # Update local .lager cache with the box version (already written to the
    # box itself back in the "Storing version" step).
    if box_cli_version and box:
        update_box_version(box, box_cli_version)

    # Finish progress bar
    if progress:
        progress.finish(success=True)

    # End-of-run summary.
    _to_version = box_cli_version or target_version
    click.echo()
    click.secho(
        f'{box_name} updated to version {_to_version} ({target_version})',
        fg='green', bold=True,
    )
    click.echo()

    # Release the auto-lock on the success path. SystemExit/error paths
    # above are covered by the atexit hook registered when the lock was
    # acquired. Idempotent — calling twice is harmless.
    _release_update_lock()


# ---------------------------------------------------------------------------
# Click wrapper
#
# `lager update`  (canonical)  — update
#
# Registered as a top-level command in cli/main.py. There used to be a second
# `lager box update` surface delegating to the same logic; it was removed in
# favor of the shorter top-level spelling.
# ---------------------------------------------------------------------------


def _update_options(fn):
    """Shared option decorators for `lager update`. Kept in one place so the
    command signature and the decorator list can't drift apart."""
    for opt in reversed([
        click.option('--box', required=False, help='Lagerbox name or IP'),
        click.option('--yes', is_flag=True, help='Skip confirmation prompt'),
        click.option('--version', required=False, help='Version to update to: a release tag (e.g. v0.21.3) or a branch (main, staging)'),
        click.option('--verbose', '-v', is_flag=True, help='Show detailed output (default shows progress bar only)'),
        click.option('--check', is_flag=True, help='Dry run: report what would change without modifying the box'),
        click.option('--force', is_flag=True, help='Update even if the box reports it is already up to date, and force a clean rebuild (wipes the cached image and cargo/npm volumes)'),
    ]):
        fn = opt(fn)
    return fn


@click.command(name='update')
@click.pass_context
@_update_options
def update(ctx, box, yes, version, verbose, check, force):
    """Update box code from GitHub repository"""
    _update_logic(
        ctx,
        box=box, yes=yes, version=version, verbose=verbose, check=check, force=force,
    )
