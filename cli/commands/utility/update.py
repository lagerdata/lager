# Copyright 2024-2026 Lager Data LLC
# SPDX-License-Identifier: Apache-2.0

"""
    lager.commands.utility.update

    Update box code from GitHub repository

    Migrated from cli/update/commands.py to cli/commands/utility/update.py.
"""
import click
import requests
from ...sort_utils import natural_sort_key
import subprocess
import time
import sys
import threading
from ...box_storage import resolve_and_validate_box, get_box_user, list_boxes
from ...context import get_default_box
from ..box.boxes import compare_versions


class ProgressBar:
    """Simple progress bar for tracking update steps."""

    # ANSI escape codes for cursor control
    CLEAR_LINE = '\033[2K'  # Clear entire line
    CURSOR_START = '\r'     # Move cursor to start of line

    def __init__(self, total_steps, width=30):
        self.total_steps = total_steps
        self.current_step = 0
        self.width = width
        self.current_task = ""
        self.start_time = time.time()
        self._stop_event = threading.Event()
        self._render_thread = None
        self._thread_started = False

    def _periodic_render(self):
        """Background thread that renders progress bar every second."""
        while not self._stop_event.is_set():
            self._render()
            time.sleep(1)

    def update(self, task_name):
        """Update progress bar with new task."""
        # Start background thread on first update to avoid showing empty 0/13 bar
        if not self._thread_started:
            self._render_thread = threading.Thread(target=self._periodic_render, daemon=True)
            self._render_thread.start()
            self._thread_started = True

        self.current_step += 1
        self.current_task = task_name
        self._render()

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

    def _render(self):
        """Render the progress bar."""
        percent = self.current_step / self.total_steps
        filled = int(self.width * percent)
        bar = '█' * filled + '░' * (self.width - filled)
        elapsed = self._format_elapsed_time()
        task_text = self.current_task[:25]  # Shorter task text to fit on one line
        # Use ANSI clear line + carriage return for reliable in-place updates
        output = f'{self.CLEAR_LINE}{self.CURSOR_START}[{bar}] {self.current_step}/{self.total_steps} {task_text:<25} {elapsed}'
        sys.stdout.write(output)
        sys.stdout.flush()

    def finish(self, success=True):
        """Complete the progress bar."""
        # Stop the background rendering thread if it was started
        if self._thread_started:
            self._stop_event.set()
            self._render_thread.join(timeout=2)

        elapsed = self._format_elapsed_time()
        if success:
            bar = '█' * self.width
            sys.stdout.write(f'{self.CLEAR_LINE}{self.CURSOR_START}[{bar}] Complete! {elapsed}\n')
        else:
            filled = int(self.width * self.current_step / self.total_steps)
            bar = '█' * filled + '░' * (self.width - filled)
            sys.stdout.write(f'{self.CLEAR_LINE}{self.CURSOR_START}[{bar}] Failed {elapsed}\n')
        sys.stdout.flush()


@click.command()
@click.pass_context
@click.option("--box", required=False, help="Lagerbox name or IP")
@click.option('--all', 'update_all', is_flag=True, help='Update all saved boxes that need updating')
@click.option('--yes', is_flag=True, help='Skip confirmation prompt')
@click.option('--skip-restart', is_flag=True, help='Skip container restart after update')
@click.option('--version', required=False, help='Box version/branch to update to (e.g., staging, main)')
@click.option('--verbose', '-v', is_flag=True, help='Show detailed output (default shows progress bar only)')
@click.option('--force', is_flag=True, help='Force fresh Docker build by removing cached image (use for major code changes)')
def update(ctx, box, update_all, yes, skip_restart, version, verbose, force):
    """Update box code from GitHub repository"""
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

    # Validate options
    if update_all and box:
        click.secho('Error: Cannot use --box with --all', fg='red', err=True)
        ctx.exit(1)

    # Handle multi-box update
    if update_all:
        from ... import __version__ as cli_version

        saved_boxes = list_boxes()
        if not saved_boxes:
            click.echo("No boxes found. Add boxes with: lager boxes add --name <NAME> --ip <IP>")
            ctx.exit(0)

        # Build list of boxes to update
        boxes_to_update = []

        click.echo()
        click.secho('Checking boxes...', fg='blue', bold=True)
        click.echo(f'CLI version: {cli_version}')
        click.echo()

        for name, box_info in sorted(saved_boxes.items(), key=lambda x: natural_sort_key(x[0])):
            if isinstance(box_info, dict):
                ip = box_info.get('ip', 'unknown')
            else:
                ip = box_info

            if ip == 'unknown':
                click.echo(f"  {name}: ", nl=False)
                click.secho('SKIPPED (no IP)', fg='yellow')
                continue

            # Check if box is locked by another user
            from ...box_storage import _check_box_lock
            try:
                _check_box_lock(ip, name)
            except SystemExit:
                click.echo(f"  {name} ({ip}): ", nl=False)
                click.secho('SKIPPED (locked)', fg='yellow')
                continue

            # Query box version to determine if update is needed
            click.echo(f"  {name} ({ip}): ", nl=False)
            try:
                url = f'http://{ip}:5000/cli-version'
                response = requests.get(url, timeout=5)
                if response.status_code == 200:
                    data = response.json()
                    box_version = data.get('box_version')
                    if box_version:
                        version_cmp = compare_versions(box_version, cli_version)
                        if version_cmp < 0:
                            # Box is older - needs update
                            click.secho(f'{box_version} (will update)', fg='yellow')
                            boxes_to_update.append((name, ip))
                        elif version_cmp == 0:
                            click.secho(f'{box_version} (current)', fg='green')
                        else:
                            click.secho(f'{box_version} (newer)', fg='cyan')
                    else:
                        click.secho('unknown version (will update)', fg='yellow')
                        boxes_to_update.append((name, ip))
                else:
                    click.secho('could not query (will update)', fg='yellow')
                    boxes_to_update.append((name, ip))
            except Exception:
                click.secho('unreachable (skipped)', fg='red')

        if not boxes_to_update:
            click.echo()
            click.secho('All boxes are up to date!', fg='green', bold=True)
            ctx.exit(0)

        # Confirm before proceeding
        click.echo()
        click.secho(f'Will update {len(boxes_to_update)} box(es):', fg='blue', bold=True)
        for name, ip in boxes_to_update:
            click.echo(f"  - {name} ({ip})")
        click.echo()

        if not yes:
            if not click.confirm(f'Update {len(boxes_to_update)} box(es)? This may take several minutes per box.'):
                click.secho('Update cancelled.', fg='yellow')
                ctx.exit(0)

        # Update each box sequentially
        results = {'success': [], 'failed': []}
        total_start = time.time()

        for idx, (name, ip) in enumerate(boxes_to_update, 1):
            click.echo()
            click.secho(f'═══ Updating {name} ({idx}/{len(boxes_to_update)}) ═══', fg='blue', bold=True)

            # Call update recursively for this single box
            try:
                # Invoke the update command for this box
                result = ctx.invoke(
                    update,
                    box=name,
                    update_all=False,
                    yes=True,  # Already confirmed
                    skip_restart=skip_restart,
                    version=version,
                    verbose=verbose,
                    force=force,
                )
                results['success'].append(name)
            except SystemExit as e:
                if e.code == 0:
                    results['success'].append(name)
                else:
                    results['failed'].append(name)
                    click.secho(f'Update failed for {name}', fg='red')
            except Exception as e:
                results['failed'].append(name)
                click.secho(f'Update failed for {name}: {str(e)}', fg='red')

        # Print summary
        total_elapsed = int(time.time() - total_start)
        minutes = total_elapsed // 60
        seconds = total_elapsed % 60

        click.echo()
        click.secho('═══ Update Summary ═══', fg='blue', bold=True)
        click.echo(f'Total time: {minutes}m {seconds:02d}s')
        click.echo()
        click.secho(f'Successful: {len(results["success"])}', fg='green')
        for name in results['success']:
            click.echo(f'  - {name}')

        if results['failed']:
            click.echo()
            click.secho(f'Failed: {len(results["failed"])}', fg='red')
            for name in results['failed']:
                click.echo(f'  - {name}')

        click.echo()
        if results['failed']:
            click.secho(f'{len(results["failed"])} box(es) failed to update', fg='red')
            ctx.exit(1)
        else:
            click.secho('All boxes updated successfully!', fg='green', bold=True)
            ctx.exit(0)

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

    # Confirm before proceeding
    if not yes:
        if not click.confirm('This will update the box code and restart services. Continue?'):
            click.secho('Update cancelled.', fg='yellow')
            ctx.exit(0)

    # Initialize progress bar (only in non-verbose mode)
    # Total steps: connectivity, repo check, git state check, fetch, checkout/pull, flatten, udev, sudoers, docker stop, [force image removal], docker build, cleanup, /etc/lager, docker start, binaries, jlink, verify, version
    total_steps = 20 if force else 19
    progress = None if verbose else ProgressBar(total_steps=total_steps)

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
            ['ssh-copy-id', '-i', key_file, ssh_host],
            timeout=300  # 5 minutes - allow time for user to enter password
        )

        if copy_result.returncode == 0:
            # Verify key works
            verify_result = subprocess.run(
                ['ssh', '-i', key_file, '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=5',
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
                ['ssh', '-i', key_file, '-o', 'ConnectTimeout=5', '-o', 'BatchMode=yes',
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
                        progress = ProgressBar(total_steps=total_steps)
                        progress.current_step = 1
                else:
                    # Key setup failed, ask if they want to continue with password
                    click.echo()
                    if yes or click.confirm('SSH key setup failed. Continue with password authentication?'):
                        use_interactive_ssh = True
                        if not verbose:
                            progress = ProgressBar(total_steps=total_steps)
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

    # Helper function to run SSH commands
    def run_ssh_command_with_output(cmd, timeout_secs=120):
        """Run an SSH command and capture output."""
        ssh_cmd = ['ssh']
        if use_explicit_key:
            ssh_cmd.extend(['-i', key_file])
        if not use_interactive_ssh:
            ssh_cmd.extend(['-o', 'BatchMode=yes'])
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
        ssh_cmd.append(ssh_host)
        ssh_cmd.append(cmd)
        # Don't capture output - let it stream to terminal for interactive prompts
        return subprocess.run(ssh_cmd, timeout=timeout_secs)

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

    # Step 2.5: Check for and fix flattened sparse checkout state
    # After install with --sparse, files are moved from box/ to root, which breaks git tracking
    # Detect this by checking if lager/ exists at root but git expects it in box/
    if progress:
        progress.update("Checking git state...")
    log('Checking for flattened sparse checkout...', nl=False)

    # Check if we have files at root level that git thinks should be in box/
    flattened_check = run_ssh_command_with_output(
        'cd ~/box && '
        'test -d lager && '  # Files exist at root
        'test ! -d box && '  # No box/ subdirectory
        'git ls-tree HEAD box/ 2>/dev/null | grep -q .'  # Git expects files in box/
    )

    # Track if we need to re-flatten after update
    needs_flatten = False

    if flattened_check.returncode == 0:
        log_status('Checking for flattened sparse checkout...', 'DETECTED', 'yellow')
        log('Fixing flattened sparse checkout state...', nl=False)

        # Clean up untracked files (the flattened files) and reset to proper git state
        # This removes root-level files and restores box/ directory structure
        fix_result = run_ssh_command_with_output(
            'cd ~/box && '
            # Remove the flattened files at root (they're untracked)
            'rm -rf lager oscilloscope-daemon start_box.sh third_party udev_rules verify_restart_policy.sh README.md 2>/dev/null; '
            # Reset git to restore box/ directory
            f'git fetch origin {target_version} && '
            f'git reset --hard {git_ref}'
        )

        if fix_result.returncode != 0:
            log_status('Fixing flattened sparse checkout state...', 'FAILED', 'red')
            if verbose and fix_result.stderr:
                click.echo(f'  Error: {fix_result.stderr.strip()}', err=True)
            # Continue anyway - the regular flow might still work
        else:
            log_status('Fixing flattened sparse checkout state...', 'OK', 'green')
            needs_flatten = True  # We restored box/, need to flatten it
    else:
        # Check if box/ directory exists (needs flattening) or lager/ at root (already flat)
        box_dir_check = run_ssh_command_with_output('cd ~/box && test -d box')
        if box_dir_check.returncode == 0:
            # box/ exists, needs flattening
            needs_flatten = True
            log_status('Checking for flattened sparse checkout...', 'NEEDS FLATTEN', 'yellow')
        else:
            log_status('Checking for flattened sparse checkout...', 'OK', 'green')

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
        result = run_ssh_command_with_output(f'cd ~/box && git checkout {target_version}')
        if result.returncode != 0:
            if progress:
                progress.finish(success=False)
            log_error(f'Error: Failed to checkout version {target_version}')
            ctx.exit(1)
        log_status(f'Checking out version {target_version}...', 'OK', 'green')

        log(f'Updating to match {git_ref}...', nl=False)
        result = run_ssh_command_with_output(f'cd ~/box && git reset --hard {git_ref}')
        if result.returncode != 0:
            if progress:
                progress.finish(success=False)
            log_error('Error: Failed to update branch')
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

                # Use interactive mode for sudo commands - allows password prompts
                if not verbose and progress:
                    # Pause progress bar and inform user
                    sys.stdout.write('\n')
                    click.echo('Installing udev rules (may require sudo password)...')
                elif verbose:
                    click.echo()  # Add newline before potential sudo prompt

                result = run_ssh_command_interactive(install_cmd, allow_sudo_prompt=True)

                if not verbose and progress:
                    # Resume progress tracking after interactive command
                    pass  # Progress bar continues automatically
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
                sys.stdout.write('\n')
                click.echo('Fixing sudoers file ownership (may require sudo password)...')
            elif verbose:
                click.echo()

            fix_result = run_ssh_command_interactive(
                'sudo chown root:root /etc/sudoers.d/lagerdata-udev',
                allow_sudo_prompt=True
            )

            if not verbose and progress:
                pass  # Progress bar continues
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

    # No git updates and no box/→root flatten work: a second consecutive `lager update`
    # would otherwise still stop every Docker container on the box, remove them, and
    # rebuild — redundant after a successful run and a common source of flaky behavior.
    if git_sync_confirmed and not needs_pull and not needs_flatten and not force:
        if progress:
            progress.finish(success=True)
        import re as _re

        _vp = _re.match(r'^v?(\d+\.\d+\.\d+)$', target_version)
        _box_v = _vp.group(1) if _vp else cli_version
        if _box_v and box:
            update_box_version(box, _box_v)
        click.echo()
        click.secho(
            'Box code is already up to date; skipping container stop/rebuild.',
            fg='green',
            bold=True,
        )
        click.echo(f'Verify with: lager hello --box {box_name}')
        click.echo()
        ctx.exit(0)

    # Skip container restart if requested
    if skip_restart:
        if progress:
            progress.finish(success=True)
        click.echo()
        click.secho('Skipping container restart (--skip-restart flag set)', fg='yellow')
        click.echo(f'Run manually: ssh {ssh_host} "cd ~/box && ./start_box.sh"')
        ctx.exit(0)

    # Step 7: Stop containers
    if progress:
        progress.update("Stopping containers...")
    log('Stopping containers...', nl=False)

    run_ssh_command_with_output(
        'docker stop $(docker ps -aq) 2>/dev/null || true && '
        'docker rm $(docker ps -aq) 2>/dev/null || true',
        timeout_secs=30
    )
    log_status('Stopping containers...', 'OK', 'green')

    # Step 7.5: Remove Docker image if --force flag is set
    if force:
        if progress:
            progress.update("Removing cached image...")
        log('Removing cached Docker image (--force)...', nl=False)

        run_ssh_command_with_output(
            'docker rmi lager 2>/dev/null || true',
            timeout_secs=30
        )
        log_status('Removing cached Docker image (--force)...', 'OK', 'green')

    # Step 8: Rebuild Docker container (the slow part)
    if progress:
        progress.update("Building container...")
    log('Rebuilding Docker container (this may take several minutes)...')

    ssh_cmd = ['ssh']
    if not use_interactive_ssh:
        ssh_cmd.extend(['-o', 'BatchMode=yes'])
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
        version_content = f'{box_cli_version}|{cli_version}'

        version_written = False
        for attempt in range(3):
            version_write_result = run_ssh_command_with_output(
                f'echo "{version_content}" > /etc/lager/version',
                timeout_secs=30
            )
            if version_write_result.returncode == 0:
                version_written = True
                break
            if attempt < 2:
                time.sleep(5)

        if not version_written:
            if progress:
                progress.finish(success=False)
            log_error('Error: Failed to write version file to /etc/lager/version')
            click.echo('The code was updated but the version file could not be written.', err=True)
            if version_write_result.stderr:
                click.echo(f'Error: {version_write_result.stderr.strip()}', err=True)
            click.echo()
            click.echo('Manually fix with:', err=True)
            click.echo(f'  ssh {ssh_host} "echo \\"{version_content}\\" | sudo tee /etc/lager/version"', err=True)
            ctx.exit(1)
        log_status('Storing version information...', f'OK ({box_cli_version})', 'green')

    # Step 10: Start container
    if progress:
        progress.update("Starting container...")
    log('Starting lager container...', nl=False)

    try:
        result = run_ssh_command_with_output(
            'cd ~/box && chmod +x start_box.sh && ./start_box.sh',
            timeout_secs=300  # 5 minutes - some boxes are slow to start
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
        log_error('Error: Container startup timed out after 5 minutes')
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

    # Wait for services
    time.sleep(5)

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

    # Final success message
    click.echo()
    click.secho('Box update completed successfully!', fg='green', bold=True)
    click.echo(f'Verify with: lager hello --box {box_name}')
    click.echo()
