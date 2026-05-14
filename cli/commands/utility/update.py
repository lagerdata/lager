# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

"""
    lager.commands.utility.update

    Update box code from GitHub repository

    Migrated from cli/update/commands.py to cli/commands/utility/update.py.
"""
import click
import requests
import shutil
import subprocess
import threading
import time
import sys
from ...box_storage import resolve_and_validate_box, get_box_user
from ...context import get_default_box
from ...core.ssh_utils import get_ssh_connection_pool


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
    """
    paths = ' '.join(_BUILD_HASH_INPUTS)
    return (
        'for f in ' + paths + '; do '
        '  [ -f "$(eval echo $f)" ] && sha256sum "$(eval echo $f)"; '
        'done | sha256sum | cut -d" " -f1'
    )


def _read_build_hash_state(ssh_runner):
    """Return (new_hash, stored_hash, mismatch) for the docker-cache decision.

    new_hash is the sha256 of the box's current Dockerfile + requirements.txt.
    stored_hash is what was recorded after the last successful build.
    mismatch is True only when both are non-empty and differ. First-run-after-
    deploy has no stored hash; we treat that as bootstrap (no rebuild forced —
    the hash gets written after the build) so it is reported as not-mismatched.

    Centralized so `--check` and the real update flow can't disagree about
    whether a rebuild is needed.
    """
    new_h = ssh_runner(_build_hash_shell_cmd())
    new = new_h.stdout.strip() if new_h.returncode == 0 else ''
    stored_h = ssh_runner('cat /etc/lager/build-hash 2>/dev/null || true')
    stored = stored_h.stdout.strip() if stored_h.returncode == 0 else ''
    return new, stored, bool(new) and bool(stored) and new != stored


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
        leaving the wrapped portion above as orphan text."""
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


def _update_logic(ctx, *, box, yes, version, verbose, check):
    """Core update logic shared by `lager box update` and the legacy `lager update`.

    Not a Click command itself — the two thin Click wrappers at the bottom of
    this module add option decorators and dispatch here.
    """
    from ...box_storage import update_box_version
    from ... import __version__ as cli_version

    # Helper for conditional output
    def log(message, nl=True, **kwargs):
        """Print message only in verbose mode."""
        if verbose:
            click.echo(message, nl=nl, **kwargs)

    def log_status(message, status, color, print_message=False):
        """Print status in verbose mode.

        If print_message=True, prints the full message + status.
        If print_message=False (default), only prints the status (assumes message already printed by log()).
        """
        if verbose:
            if print_message:
                click.echo(message, nl=False)
            click.secho(f' {status}', fg=color)

    def log_error(message):
        """Always print errors."""
        click.secho(message, fg='red', err=True)

    # Default to 'main' version if not specified
    target_version = version or 'main'

    # Determine the correct git ref for reset/rev-list operations.
    # Tags (e.g. v0.14.0) start with 'v' and must be referenced directly;
    # version branches (e.g. 0.14.0) and named branches use origin/<name>.
    import re as _re_version
    _is_tag = bool(_re_version.match(r'^v\d+\.\d+\.\d+', target_version))
    git_ref = target_version if _is_tag else f'origin/{target_version}'

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

    # Initialize progress bar (only in non-verbose mode).
    #
    # Use the maximum possible step count (19 always-runs + 1 optional flatten
    # + 1 optional image-wipe = 21). We can't know whether flatten/wipe will
    # actually fire until later, so picking the max avoids a mid-flight
    # denominator jump (19 → 21 reads as a regression). Light paths that
    # don't take both conditionals will simply finish at 19/21 or 20/21 —
    # `_render()` already caps the bar fill at 100% via min(percent, 1.0),
    # and `finish()` overrides with a full bar at the end.
    progress = None if verbose else ProgressBar(total_steps=21)

    if not verbose:
        click.echo()  # Blank line before progress bar

    # Step 1: Check SSH connectivity
    if progress:
        progress.update("Checking SSH...")
    log('Checking connectivity...', nl=False)

    import os
    key_file = os.path.expanduser('~/.ssh/lager_box')
    use_interactive_ssh = False
    use_explicit_key = False

    def setup_ssh_key():
        """Create lager_box key if needed and copy to box. Returns True if successful."""
        nonlocal use_explicit_key

        key_exists = os.path.exists(key_file)

        # Create key if it doesn't exist
        if not key_exists:
            click.echo()
            click.echo('Creating SSH key...')
            os.makedirs(os.path.expanduser('~/.ssh'), exist_ok=True)
            keygen_result = subprocess.run(
                ['ssh-keygen', '-t', 'ed25519', '-f', key_file, '-N', '', '-C', 'lager-box-access'],
                capture_output=True, text=True
            )
            if keygen_result.returncode != 0:
                log_error('Error: Failed to create SSH key')
                return False
            click.secho('SSH key created', fg='green')

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

        if copy_result.returncode == 0:
            # Verify key works
            verify_result = subprocess.run(
                ['ssh', '-i', key_file,
                 '-o', 'BatchMode=yes',
                 '-o', 'StrictHostKeyChecking=accept-new',
                 '-o', 'ConnectTimeout=5',
                 ssh_host, 'echo test'],
                capture_output=True, text=True, timeout=10
            )
            if verify_result.returncode == 0:
                click.echo()
                click.secho('SSH key installed successfully!', fg='green')
                click.echo('Future connections will not require a password.')
                click.echo()
                use_explicit_key = True
                return True

        click.secho('Failed to set up SSH key.', fg='yellow')
        return False

    try:
        # First try with lager_box key if it exists
        if os.path.exists(key_file):
            result = subprocess.run(
                ['ssh', '-i', key_file,
                 '-o', 'ConnectTimeout=5',
                 '-o', 'BatchMode=yes',
                 '-o', 'StrictHostKeyChecking=accept-new',
                 ssh_host, 'echo test'],
                capture_output=True,
                text=True,
                timeout=10
            )
            if result.returncode == 0:
                use_explicit_key = True
                log_status('Checking connectivity...', 'OK', 'green')
                # Skip the rest of connectivity check - key already works
                result = type('obj', (object,), {'returncode': 0})()

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
                        progress = ProgressBar(total_steps=21)
                        progress.current_step = 1
                else:
                    # Key setup failed, ask if they want to continue with password
                    click.echo()
                    if yes or click.confirm('SSH key setup failed. Continue with password authentication?'):
                        use_interactive_ssh = True
                        if not verbose:
                            progress = ProgressBar(total_steps=21)
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

        # Short-circuit if the box already has exactly this content.
        existing = run_ssh_command_with_output('cat /etc/lager/version 2>/dev/null')
        if existing.returncode == 0 and existing.stdout.strip() == version_content:
            return True

        for attempt in range(3):
            result = run_ssh_command_with_output(
                f'echo "{version_content}" > /etc/lager/version',
                timeout_secs=30,
            )
            if result.returncode == 0:
                return True
            if attempt < 2:
                time.sleep(5)
        return False

    # Step 2: Check if box directory exists and is a git repo
    if progress:
        progress.update("Checking repository...")
    log('Checking box repository...', nl=False)

    result = run_ssh_command_with_output('test -d ~/box/.git')
    if result.returncode != 0:
        if progress:
            progress.finish(success=False)
        log_error('Error: Box directory is not a git repository')
        click.echo('The box may have been deployed with rsync instead of git clone.')
        click.echo('Please re-deploy the box using the latest deployment script.')
        ctx.exit(1)
    log_status('Checking box repository...', 'OK', 'green')

    # Step 2.1: Migrate remote URL from SSH to HTTPS if needed
    # Boxes deployed before open-source migration may still use git@github.com:
    if progress:
        progress.update("Checking remote URL...")
    log('Checking remote URL...', nl=False)

    remote_url_result = run_ssh_command_with_output('cd ~/box && git remote get-url origin')
    if remote_url_result.returncode == 0:
        remote_url = remote_url_result.stdout.strip()
        if remote_url.startswith('git@github.com:'):
            https_url = remote_url.replace('git@github.com:', 'https://github.com/')
            migrate_result = run_ssh_command_with_output(f'cd ~/box && git remote set-url origin {https_url}')
            if migrate_result.returncode == 0:
                log_status('Checking remote URL...', 'MIGRATED', 'yellow')
                log(f'Remote URL migrated from SSH to HTTPS')
            else:
                log_status('Checking remote URL...', 'FAILED', 'red')
                log('Warning: Could not migrate remote URL to HTTPS')
        else:
            log_status('Checking remote URL...', 'OK', 'green')
    else:
        log_status('Checking remote URL...', 'OK', 'green')

    # Step 2.5: Determine whether a flatten will be needed after the git ops.
    #
    # The repo uses sparse-checkout to put files under `box/`, but the runtime
    # expects them at `~/box/<file>` (the flat layout). After a successful
    # update, the box is permanently in the flat state: `~/box/lager/` exists,
    # `~/box/box/` does not. That is the **expected** post-update state, not a
    # broken one.
    #
    # The previous heuristic ("files at root + box/ absent + git wants box/"
    # → broken, must re-fetch and re-flatten) misfired on every consecutive
    # `lager update`: it'd repeatedly wipe the flat tree, refetch, and
    # re-flatten, doing 20+ seconds of pointless container churn each run.
    # We now treat the flat-with-no-box-subdir state as healthy and only set
    # needs_flatten when there's actually a `~/box/box/` to flatten.
    if progress:
        progress.update("Checking git state...")
    log('Checking layout...', nl=False)

    needs_flatten = False
    box_dir_check = run_ssh_command_with_output('cd ~/box && test -d box')
    if box_dir_check.returncode == 0:
        # box/ subdirectory present — needs flattening after the git ops below.
        needs_flatten = True
        log_status('Checking layout...', 'NEEDS FLATTEN', 'yellow')
    else:
        log_status('Checking layout...', 'OK (flat)', 'green')

    # Step 3: Show current version (verbose only)
    if verbose:
        click.echo('Current version:', nl=False)
        result = run_ssh_command_with_output('cd ~/box && git log -1 --format="%h - %s (%cr)"')
        if result.returncode == 0 and result.stdout.strip():
            click.echo(f' {result.stdout.strip()}')
        else:
            click.echo(' (unknown)')

    # Step 4: Fetch and check for updates
    if progress:
        progress.update("Fetching updates...")
    log(f'Fetching updates from {git_ref}...', nl=False)

    result = run_ssh_command_with_output(f'cd ~/box && git fetch origin {target_version}')
    if result.returncode != 0:
        if progress:
            progress.finish(success=False)
        stderr = result.stderr.strip() if result.stderr else ""
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
            click.secho("  ssh lagerdata@<box> 'cd ~/box && git remote set-url origin https://github.com/lagerdata/lager.git'", err=True)
        elif "not found" in stderr.lower() or f"couldn't find remote ref {target_version}" in stderr.lower():
            log_error(f"Error: Branch '{target_version}' not found on remote")
            click.secho(f"The branch '{target_version}' does not exist on GitHub.", err=True)
            click.secho("Available branches can be found at: https://github.com/lagerdata/lager/branches", err=True)
            click.secho("Common branches: main, staging", err=True)
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
    log_status(f'Fetching updates from {git_ref}...', 'OK', 'green')

    # Check if there are updates available
    result = run_ssh_command_with_output(f'cd ~/box && git rev-list HEAD..{git_ref} --count')

    needs_pull = False
    # Only trust "already up to date" for fast-path if rev-list succeeds with an integer count
    git_sync_confirmed = False
    if result.returncode == 0:
        try:
            commits_behind = int(result.stdout.strip())
        except ValueError:
            commits_behind = None
        if commits_behind is not None:
            git_sync_confirmed = True
            if commits_behind == 0:
                if verbose:
                    click.secho('Box code is already up to date!', fg='green')
                needs_pull = False
            else:
                log(f'Updates available: {commits_behind} new commit(s)')
                needs_pull = True

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

        current_version_result = run_ssh_command_with_output(
            'cat /etc/lager/version 2>/dev/null || true'
        )
        current_version_raw = current_version_result.stdout.strip()
        current_box_version = current_version_raw.split('|', 1)[0] if current_version_raw else '(unknown)'

        _check_new_hash, _check_stored_hash, deps_will_change = _read_build_hash_state(
            run_ssh_command_with_output
        )

        if not git_sync_confirmed:
            click.secho('Could not determine update state (git rev-list failed).', fg='red', err=True)
            ctx.exit(2)

        if commits_behind == 0:
            code_status = 'in sync'
        else:
            code_status = f'will update ({commits_behind} commit(s) behind {git_ref})'

        if deps_will_change:
            deps_status = 'will trigger fresh build (Dockerfile or requirements changed)'
        else:
            deps_status = 'cache valid (no rebuild)'

        if commits_behind == 0 and not deps_will_change:
            container_status = 'no restart needed'
            est = '~5s'
        elif deps_will_change:
            container_status = 'will restart'
            est = '~6 min (fresh build)'
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

        will_change = commits_behind != 0 or deps_will_change
        if will_change:
            click.echo('Run without --check to apply.')
            ctx.exit(1)
        click.echo('Nothing to do.')
        ctx.exit(0)

    if needs_pull:
        # Step 5: Update git repo
        if progress:
            progress.update("Pulling updates...")
        log('Ensuring required files are tracked...', nl=False)

        run_ssh_command_with_output(
            'cd ~/box && '
            'git sparse-checkout list | grep -q "^udev_rules$" || git sparse-checkout add udev_rules && '
            'git sparse-checkout list | grep -q "^cli/__init__.py$" || git sparse-checkout add cli/__init__.py'
        )
        log_status('Ensuring required files are tracked...', 'OK', 'green')

        log(f'Checking out version {target_version}...', nl=False)
        # `-f` so a prior flatten artifact (root-level tracked file overwritten
        # by box/<same-name>) doesn't block the switch. Observed on STG-C:
        # flatten of a branch that had both root README.md and box/README.md
        # left the root copy looking modified, and `git checkout main` failed
        # with "local changes would be overwritten". Editing the on-box git
        # tree by hand is not a supported workflow, so discarding such
        # "modifications" is safe.
        result = run_ssh_command_with_output(f'cd ~/box && git checkout -f {target_version}')
        if result.returncode != 0:
            if progress:
                progress.finish(success=False)
            log_error(f'Error: Failed to checkout version {target_version}')
            if result.stderr:
                click.echo(result.stderr.strip(), err=True)
            ctx.exit(1)
        log_status(f'Checking out version {target_version}...', 'OK', 'green')

        log(f'Updating to match {git_ref}...', nl=False)
        result = run_ssh_command_with_output(f'cd ~/box && git reset --hard {git_ref}')
        if result.returncode != 0:
            if progress:
                progress.finish(success=False)
            log_error('Error: Failed to update branch')
            if result.stderr:
                click.echo(result.stderr.strip(), err=True)
            ctx.exit(1)
        log_status(f'Updating to match {git_ref}...', 'OK', 'green')

        if verbose:
            click.echo('New version:', nl=False)
            result = run_ssh_command_with_output('cd ~/box && git log -1 --format="%h - %s (%cr)"')
            if result.returncode == 0 and result.stdout.strip():
                click.echo(f' {result.stdout.strip()}')
        needs_flatten = True  # After pull, always flatten
    else:
        if progress:
            progress.update("Already up to date")

    # Flatten the directory structure if needed (box/ -> root)
    # This handles sparse checkout where files are in ~/box/box/ but need to be in ~/box/
    if needs_flatten:
        if progress:
            progress.update("Flattening structure...")
        log('Updating file structure...', nl=False)
        result = run_ssh_command_with_output(
            'cd ~/box && '
            'if [ -d box ]; then '
            'shopt -s dotglob && '
            'cp -rf box/* . && '
            'rm -rf box; '
            'fi'
        )
        if result.returncode == 0:
            log_status('Updating file structure...', 'OK', 'green')
        else:
            # Non-fatal - box might already be flattened
            log_status('Updating file structure...', 'SKIPPED', 'yellow')

        # Verify the flatten actually left the box in a buildable shape.
        # Previously this step swallowed cp failures silently, and the docker
        # build would proceed against missing files, producing an image that
        # passed `docker ps` but failed at runtime — one of the documented
        # "had to run update 3 times" failure modes.
        verify_result = run_ssh_command_with_output(
            'test -f ~/box/lager/box_http_server.py && '
            'test -f ~/box/lager/docker/box.Dockerfile'
        )
        if verify_result.returncode != 0:
            if progress:
                progress.finish(success=False)
            log_error('Error: Source files missing after flatten step')
            click.echo('Expected to find ~/box/lager/box_http_server.py and ~/box/lager/docker/box.Dockerfile.', err=True)
            click.echo('The sparse-checkout layout on the box is inconsistent; try a fresh `lager install`.', err=True)
            ctx.exit(1)

    # Compute the docker-build cache state up front so the early-exit decision
    # below knows whether deps-only changes require a rebuild. Previously this
    # only happened after the early-exit, so a corrupted /etc/lager/build-hash
    # with code in sync would silently take the no-op path — the very case the
    # auto-invalidation feature is supposed to catch. The result is reused
    # below for `must_wipe_image`.
    new_build_hash, stored_build_hash, hash_mismatch = _read_build_hash_state(
        run_ssh_command_with_output
    )
    must_wipe_image = hash_mismatch

    # Step 6: Check and update udev rules if needed
    if progress:
        progress.update("Checking udev rules...")
    log('Checking udev rules...', nl=False)

    # Check for udev_rules in the flattened structure first, then fall back to box/udev_rules
    result = run_ssh_command_with_output('test -d ~/box/udev_rules')
    udev_path = '~/box/udev_rules' if result.returncode == 0 else '~/box/box/udev_rules'

    result = run_ssh_command_with_output(f'test -d {udev_path}')
    if result.returncode == 0:
        # Check if rules file exists in source
        rules_check = run_ssh_command_with_output(f'test -f {udev_path}/99-instrument.rules')
        if rules_check.returncode != 0:
            log_status('Checking udev rules...', 'FAILED (file not found)', 'red')
            if verbose:
                click.echo(f'  Error: {udev_path}/99-instrument.rules not found', err=True)
        else:
            # Check if already installed and matches source
            diff_check = run_ssh_command_with_output(
                f'diff -q {udev_path}/99-instrument.rules /etc/udev/rules.d/99-instrument.rules >/dev/null 2>&1'
            )

            if diff_check.returncode == 0:
                # Files match - skip installation
                log_status('Checking udev rules...', 'OK (already up-to-date)', 'green')
            else:
                # Need to install/update
                log_status('Checking udev rules...', 'UPDATE NEEDED', 'yellow')
                log('Installing udev rules...', nl=False)

                install_cmd = (
                    f'cp {udev_path}/99-instrument.rules /tmp/ && '
                    'sudo /bin/cp /tmp/99-instrument.rules /etc/udev/rules.d/ && '
                    'sudo /bin/chmod 644 /etc/udev/rules.d/99-instrument.rules && '
                    'sudo /usr/bin/udevadm control --reload-rules && '
                    'sudo /usr/bin/udevadm trigger && '
                    'sudo /bin/rm -f /tmp/99-instrument.rules'
                )

                # Use interactive mode for sudo commands - allows password prompts.
                # pause()/resume() match the sudoers and box-config-sudoers
                # paths below; without them the 1s periodic re-render would
                # overwrite the sudo password prompt and the user couldn't
                # see what's waiting on stdin.
                if not verbose and progress:
                    progress.pause()
                    click.echo('Installing udev rules (may require sudo password)...')
                elif verbose:
                    click.echo()  # Add newline before potential sudo prompt

                result = run_ssh_command_interactive(install_cmd, allow_sudo_prompt=True)

                if not verbose and progress:
                    progress.resume()
                elif verbose:
                    click.echo()  # Add newline after sudo command

                if result.returncode == 0:
                    # Verify installation succeeded
                    verify_result = run_ssh_command_with_output('test -f /etc/udev/rules.d/99-instrument.rules')
                    if verify_result.returncode == 0:
                        log_status('Installing udev rules...', 'OK', 'green')
                    else:
                        log_status('Installing udev rules...', 'FAILED (verification failed)', 'red')
                        if verbose:
                            click.echo('  Error: udev rules file not found after installation', err=True)
                            click.echo('  This may indicate a sudo permission issue', err=True)
                else:
                    log_status('Installing udev rules...', 'FAILED', 'red')
                    if verbose:
                        click.echo('  Error: Failed to install udev rules', err=True)
                        click.echo('  This may be a sudo permission issue. The sudoers file may need updating.', err=True)
                        click.echo(f'  You can manually install with: ssh {ssh_host}', err=True)
                        click.echo(f'    sudo cp ~/box/udev_rules/99-instrument.rules /etc/udev/rules.d/', err=True)
                        click.echo(f'    sudo udevadm control --reload-rules && sudo udevadm trigger', err=True)
    else:
        log_status('Checking udev rules...', 'FAILED (directory not found)', 'red')
        if verbose:
            click.echo(f'  Error: {udev_path} directory not found', err=True)
            click.echo('  The udev_rules directory should be included in the sparse checkout', err=True)

    # Step 6.5: Fix sudoers file ownership if needed
    # The /etc/sudoers.d/lagerdata-udev file must be owned by root for sudo to work
    # If it's owned by uid 1000 (lagerdata user), sudo will refuse to work
    if progress:
        progress.update("Checking sudoers...")
    log('Checking sudoers file ownership...', nl=False)

    # Check if the sudoers file exists and get its owner
    sudoers_check = run_ssh_command_with_output(
        '[ -f /etc/sudoers.d/lagerdata-udev ] && '
        'stat -c "%u" /etc/sudoers.d/lagerdata-udev 2>/dev/null || '
        'stat -f "%u" /etc/sudoers.d/lagerdata-udev 2>/dev/null || '
        'echo "NOTFOUND"'
    )

    if sudoers_check.returncode == 0:
        owner_uid = sudoers_check.stdout.strip()
        if owner_uid == "NOTFOUND":
            log_status('Checking sudoers file ownership...', 'SKIPPED (file not found)', 'yellow')
        elif owner_uid != "0":
            # File exists but not owned by root - fix it
            log_status('Checking sudoers file ownership...', f'FIXING (owned by uid {owner_uid})', 'yellow')
            log('Fixing sudoers file ownership...', nl=False)

            if not verbose and progress:
                progress.pause()
                click.echo('Fixing sudoers file ownership (may require sudo password)...')
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

            if fix_result.returncode == 0:
                log_status('Fixing sudoers file ownership...', 'OK', 'green')
            else:
                log_status('Fixing sudoers file ownership...', 'FAILED', 'red')
                if verbose:
                    click.echo('  Warning: Could not fix sudoers ownership. Sudo may not work correctly.', err=True)
        else:
            # File owned by root - all good
            log_status('Checking sudoers file ownership...', 'OK', 'green')
    else:
        log_status('Checking sudoers file ownership...', 'SKIPPED', 'yellow')

    # Step 6.6: Ensure passwordless sudo for `lager box config apply`.
    #
    # `lager box config apply` needs root on the host for apt-get install,
    # sysctl writes, and mount-path mkdir/chown — all over BatchMode SSH
    # where sudo can't prompt. The rule grants narrow NOPASSWD for exactly
    # those operations. This runs on every `lager update` so existing boxes
    # gradually pick up the rule without a full re-install; idempotent
    # (skips when already in place).
    if progress:
        progress.update("Checking box-config sudoers...")
    log('Checking box-config sudoers...', nl=False)

    # Functional check: can we actually run the thing the rule grants?
    # `sudo -n DEBIAN_FRONTEND=noninteractive apt-get --version` succeeds
    # only when (a) sudo -n is allowed for apt-get (NOPASSWD) and (b)
    # SETENV is permitted (otherwise sudo rejects with "not allowed to
    # set the following environment variables"). Grepping `sudo -l`
    # output is fragile across sudo versions / locales / `requiretty`.
    # Detection: marker file + functional probe.
    # - `/etc/lager/.boxcfg-sudoers-v2` is written by the bootstrap; its
    #   existence indicates the v2 rule shape (with the cp clause for
    #   rollback) is installed. Bump the version suffix when expanding
    #   the rule so older boxes re-bootstrap automatically.
    # - `sudo -n DEBIAN_FRONTEND=...` confirms the rule is still
    #   functionally live (catches the case where the sudoers file was
    #   manually deleted but the marker stayed).
    # The previous `sudo -n -l <cmd>` approach falsely passed because
    # Ubuntu's default `%sudo` group rule grants the user (ALL:ALL) ALL
    # with-password, and `-l` returns 0 if the command is permitted at
    # all — not specifically NOPASSWD.
    boxcfg_sudoers_check = run_ssh_command_with_output(
        "test -f /etc/lager/.boxcfg-sudoers-v2 "
        "&& sudo -n DEBIAN_FRONTEND=noninteractive apt-get --version >/dev/null 2>&1"
    )
    if boxcfg_sudoers_check.returncode == 0:
        log_status('Checking box-config sudoers...', 'OK', 'green')
    else:
        log_status('Checking box-config sudoers...', 'NEEDS BOOTSTRAP', 'yellow')

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

        if boxcfg_install_result.returncode == 0:
            # Same functional check as the pre-install detection — verify
            # the rule is actually live by running the thing it grants.
            boxcfg_verify = run_ssh_command_with_output(
                "sudo -n DEBIAN_FRONTEND=noninteractive apt-get --version >/dev/null 2>&1"
            )
            if boxcfg_verify.returncode == 0:
                log_status('Installing box-config sudoers...', 'OK', 'green')
            else:
                log_status('Installing box-config sudoers...', 'INSTALLED BUT NOT VERIFIED', 'yellow')
                if verbose:
                    click.echo(
                        '  Warning: rule installed but `sudo -n apt-get` still requires a password. '
                        'Check /etc/sudoers.d/lager-box-config syntax on the box.',
                        err=True,
                    )
        else:
            log_status('Installing box-config sudoers...', 'FAILED', 'yellow')
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
    if (
        git_sync_confirmed
        and not needs_pull
        and not needs_flatten
        and not hash_mismatch
    ):
        import re as _re

        _vp = _re.match(r'^v?(\d+\.\d+\.\d+)$', target_version)
        _box_v = _vp.group(1) if _vp else cli_version

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

    # Step 7: Stop containers
    if progress:
        progress.update("Stopping containers...")
    log('Stopping containers...', nl=False)

    run_ssh_command_with_output(
        'docker stop lager pigpio 2>/dev/null || true && '
        'docker rm lager pigpio 2>/dev/null || true',
        timeout_secs=30
    )
    log_status('Stopping containers...', 'OK', 'green')

    # Step 7.5: Wipe the cached Docker image when build inputs changed
    # (`hash_mismatch`), so a pip-dependency change in the Dockerfile doesn't
    # reuse a stale layer. `new_build_hash`, `stored_build_hash`,
    # `hash_mismatch`, and `must_wipe_image` were all set above just after
    # the flatten step.
    if must_wipe_image:
        if progress:
            progress.update("Removing cached image...")
        log('Removing cached Docker image (build inputs changed)...', nl=False)

        run_ssh_command_with_output(
            'docker rmi lager 2>/dev/null || true',
            timeout_secs=30
        )
        log_status('Removing cached Docker image (build inputs changed)...', 'OK', 'green')

    # Step 8: Rebuild Docker container (the slow part)
    if progress:
        progress.update("Building container...")
    log('Rebuilding Docker container (this may take several minutes)...')

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
                click.secho("Hint: Disk space is full on the box. Run: ssh lagerdata@<box> 'docker system prune -af'", fg='yellow', err=True)
            elif "network" in full_output.lower() and ("timeout" in full_output.lower() or "error" in full_output.lower()):
                click.secho("Hint: Network issue during build. Check box internet connectivity.", fg='yellow', err=True)
            elif "permission denied" in full_output.lower():
                click.secho("Hint: Permission issue. Check Docker daemon is running and user has access.", fg='yellow', err=True)
        ctx.exit(1)
    log_status('Building container...', 'OK', 'green')

    # Record the build-inputs hash so the next run can decide whether to
    # invalidate the cache automatically. Best-effort — a failure here just
    # means the next update may rebuild unnecessarily.
    if new_build_hash:
        run_ssh_command_with_output(
            f'echo "{new_build_hash}" > /etc/lager/build-hash',
            timeout_secs=15,
        )

    # Step 8.5: Clean up old images to save disk space (after successful build)
    if progress:
        progress.update("Cleaning up images...")
    log('Cleaning up old Docker images...', nl=False)
    run_ssh_command_with_output(
        'docker image prune -af --filter "until=24h"',
        timeout_secs=30
    )
    log_status('Cleaning up old Docker images...', 'OK', 'green')

    # Step 9: Ensure /etc/lager directory exists (required by start_box.sh)
    if progress:
        progress.update("Setting up /etc/lager...")
    log('Ensuring /etc/lager directory exists...', nl=False)

    # Use full paths to match sudoers whitelist in deployment script
    # Run mkdir and chmod - they're idempotent and passwordless via sudoers
    etc_lager_result = run_ssh_command_with_output(
        'sudo /bin/mkdir -p /etc/lager && sudo /bin/chmod 777 /etc/lager',
        timeout_secs=30
    )

    if etc_lager_result.returncode != 0:
        if progress:
            progress.finish(success=False)
        log_error('Error: Failed to create /etc/lager directory')
        click.echo('This may be a sudo permission issue. SSH into the box and run:', err=True)
        click.echo(f'  ssh {ssh_host}', err=True)
        click.echo(f'  sudo mkdir -p /etc/lager && sudo chmod 777 /etc/lager', err=True)
        click.echo('Then run lager update again.', err=True)
        ctx.exit(1)
    log_status('Ensuring /etc/lager directory exists...', 'OK', 'green')

    # Write version file BEFORE container restart (SSH is stable at this point)
    # Determine box version to write:
    # - If target is a version tag (v0.3.14, 0.3.14), use it directly
    # - If target is a branch (main, staging), use the CLI version since we're syncing to it
    import re
    version_pattern = re.match(r'^v?(\d+\.\d+\.\d+)$', target_version)

    if version_pattern:
        box_cli_version = version_pattern.group(1)
    else:
        box_cli_version = cli_version

    if box_cli_version:
        if progress:
            progress.update("Storing version...")
        log('Storing version information...', nl=False)

        if not write_box_version_file(box_cli_version):
            if progress:
                progress.finish(success=False)
            log_error('Error: Failed to write version file to /etc/lager/version')
            click.echo('The code was updated but the version file could not be written.', err=True)
            click.echo()
            click.echo('Manually fix with:', err=True)
            click.echo(f'  ssh {ssh_host} "echo \\"{box_cli_version}|{cli_version}\\" | sudo tee /etc/lager/version"', err=True)
            ctx.exit(1)
        log_status('Storing version information...', f'OK ({box_cli_version})', 'green')

    # Step 10: Start container
    if progress:
        progress.update("Starting container...")
    log('Starting lager container...', nl=False)

    try:
        result = run_ssh_command_with_output(
            'cd ~/box && chmod +x start_box.sh && ./start_box.sh',
            timeout_secs=600  # 10 minutes - covers docker build + cargo install on slow boxes
        )

        if result.returncode != 0:
            if progress:
                progress.finish(success=False)
            log_error('Error: Failed to start lager container')
            # Show error output even in non-verbose mode so users can see what went wrong
            if result.stdout:
                click.echo('Container output:', err=True)
                click.echo(result.stdout, err=True)
            if result.stderr:
                click.echo(result.stderr, err=True)
            ctx.exit(1)
        log_status('Starting lager container...', 'OK', 'green')
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
        log_error('Error: Box services did not respond within 60s after restart')
        click.echo(f'The lager container is running but http://{resolved_box}:5000/health did not return 200.', err=True)
        click.echo('Investigate with:', err=True)
        click.echo(f'  ssh {ssh_host} "docker logs lager --tail 50"', err=True)
        click.echo(f'  ssh {ssh_host} "docker ps"', err=True)
        ctx.exit(1)
    log_status('Waiting for box services...', 'OK', 'green')

    # Step 11: Setup customer binaries directory
    if progress:
        progress.update("Setting up binaries...")
    log('Setting up customer binaries directory...', nl=False)

    # Create the customer-binaries directory with proper permissions
    # This allows the container (running as www-data) to write uploaded binaries
    binaries_setup = run_ssh_command_with_output(
        'mkdir -p ~/third_party/customer-binaries && '
        'chmod 777 ~/third_party/customer-binaries'
    )
    if binaries_setup.returncode == 0:
        log_status('Setting up customer binaries directory...', 'OK', 'green')
    else:
        log_status('Setting up customer binaries directory...', 'SKIPPED', 'yellow')

    # Step 12: Install J-Link if not present
    if progress:
        progress.update("Checking J-Link...")
    log('Checking J-Link installation...', nl=False)

    # Check if J-Link is already installed
    jlink_check = run_ssh_command_with_output(
        'find ~/third_party -name JLinkGDBServerCLExe 2>/dev/null | head -n 1'
    )

    if jlink_check.returncode == 0 and jlink_check.stdout.strip():
        log_status('Checking J-Link installation...', 'OK (already installed)', 'green')
    else:
        log_status('Checking J-Link installation...', 'NOT FOUND', 'yellow')
        log('  Installing J-Link...')

        # Create installation script on box
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
            log_status('  Installing J-Link...', 'OK', 'green')
            if verbose and install_result.stdout:
                for line in install_result.stdout.strip().split('\n'):
                    click.echo(f'    {line}')
        else:
            log_status('  Installing J-Link...', 'FAILED (will use pyOCD)', 'yellow')
            if verbose:
                if install_result.stderr:
                    click.echo(f'    Error: {install_result.stderr.strip()}', err=True)
                click.echo()
                click.echo('    J-Link download failed. You can either:')
                click.echo(f'      1. Copy from another box: deployment/copy_jlink_from_box.sh <source-box> {box_name}')
                click.echo('      2. Manually download from https://www.segger.com/downloads/jlink/')
                click.echo('      3. Use pyOCD (already installed, works with most debug probes)')
                click.echo()

    # Step 13: Verify and store version
    if progress:
        progress.update("Verifying...")
    log('Verifying container status...', nl=False)

    result = run_ssh_command_with_output("docker ps --filter 'name=lager' --format '{{.Names}}' | wc -l")
    if result.returncode == 0:
        running_count = int(result.stdout.strip())
        if running_count >= 1:
            log_status('Verifying container status...', 'OK', 'green')
        else:
            log_status('Verifying container status...', 'WARNING', 'yellow')
    else:
        log_status('Verifying container status...', 'FAILED', 'red')

    # Show container status (verbose only)
    if verbose:
        click.echo()
        click.secho('Container Status:', fg='blue', bold=True)
        result = run_ssh_command_with_output(
            "docker ps --filter 'name=lager' "
            "--format 'table {{.Names}}\t{{.Status}}'"
        )
        if result.returncode == 0:
            click.echo(result.stdout.strip())

    # Update local .lager file with version (version was already written to box above)
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


# ---------------------------------------------------------------------------
# Click wrappers
#
# `lager box update`  (canonical)  — update_cmd
# `lager update`      (deprecated alias, hidden in --help)  — update
#
# Both delegate to _update_logic above. The deprecated entry prints a one-line
# notice on every invocation so existing scripts keep working but users are
# nudged toward the new name.
# ---------------------------------------------------------------------------


def _update_options(fn):
    """Shared option decorators for both `lager box update` and the
    deprecated top-level `lager update` alias. Keeping them in one place
    so the two surfaces can't drift apart."""
    for opt in reversed([
        click.option('--box', required=False, help='Lagerbox name or IP'),
        click.option('--yes', is_flag=True, help='Skip confirmation prompt'),
        click.option('--version', required=False, help='Box version/branch to update to (e.g., staging, main)'),
        click.option('--verbose', '-v', is_flag=True, help='Show detailed output (default shows progress bar only)'),
        click.option('--check', is_flag=True, help='Dry run: report what would change without modifying the box'),
    ]):
        fn = opt(fn)
    return fn


@click.command(name='update')
@click.pass_context
@_update_options
def update_cmd(ctx, box, yes, version, verbose, check):
    """Update box code from GitHub repository."""
    _update_logic(
        ctx,
        box=box, yes=yes, version=version, verbose=verbose, check=check,
    )


@click.command(name='update', hidden=True)
@click.pass_context
@_update_options
def update(ctx, box, yes, version, verbose, check):
    """[DEPRECATED] Use `lager box update` instead."""
    click.secho('Note: `lager update` is deprecated; use `lager box update` instead.', fg='yellow', err=True)
    _update_logic(
        ctx,
        box=box, yes=yes, version=version, verbose=verbose, check=check,
    )
