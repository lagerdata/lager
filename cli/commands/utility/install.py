# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

"""
    lager.commands.utility.install

    Install lager box code onto a new box
"""
import click
import subprocess
import tempfile
import shutil
from pathlib import Path
from importlib import resources
from ...address_utils import validate_ip_or_hostname, VALID_FORMATS_CHEATSHEET
from ...box_storage import (
    add_box,
    auto_lock_around_command,
    get_box_ip,
    get_box_user,
)
from ...core.ssh_utils import host_in_known_hosts
from ...errors import ssh_error, LagerError
from ..box._host_ops import boxcfg_sudoers_bootstrap_cmd, is_valid_unix_username


def get_script_path(script_name: str, subdir: str = "scripts") -> Path:
    """Get path to deployment script from package resources.

    This function finds deployment scripts that are packaged with the CLI,
    allowing `lager install` to work from pip-installed versions.

    Args:
        script_name: Name of the script file (e.g., "setup_and_deploy_box.sh")
        subdir: Subdirectory within deployment ("scripts" or "security")

    Returns:
        Path to the script file
    """
    if subdir == "scripts":
        package = "cli.deployment.scripts"
    elif subdir == "security":
        package = "cli.deployment.security"
    else:
        raise ValueError(f"Unknown subdir: {subdir}")

    # Try importlib.resources first (works for pip-installed package)
    try:
        script_files = resources.files(package)
        script_traversable = script_files.joinpath(script_name)

        # For regular directory installs, we can get the path directly
        # by converting the Traversable to a string and checking if it exists
        potential_path = Path(str(script_traversable))
        if potential_path.exists():
            return potential_path

        # For zip/wheel imports, extract to temp directory
        temp_dir = Path(tempfile.gettempdir()) / "lager_deployment" / subdir
        temp_dir.mkdir(parents=True, exist_ok=True)
        dest = temp_dir / script_name

        # Read content and write to temp file
        content = script_traversable.read_bytes()
        dest.write_bytes(content)
        dest.chmod(0o755)  # Make executable
        return dest

    except (ModuleNotFoundError, FileNotFoundError, TypeError, AttributeError):
        pass

    # Fallback: check if scripts are in cli/deployment (dev mode)
    cli_root = Path(__file__).parent.parent.parent
    dev_path = cli_root / "deployment" / subdir / script_name
    if dev_path.exists():
        return dev_path

    raise FileNotFoundError(f"Deployment script not found: {script_name}")


@click.command()
@click.pass_context
@click.option("--box", default=None, help="Box name (uses stored IP and username)")
@click.option("--ip", default=None, help="Target box IP address or DNS hostname")
@click.option("--user", default=None, help="SSH username (default: lagerdata, or stored username if using --box)")
@click.option("--version", "version", default="main", help="Version to deploy: a release tag (e.g. v0.21.3) or a branch (main, staging; default: main)")
@click.option("--skip-jlink", is_flag=True, help="Skip J-Link installation")
@click.option("--skip-firewall", is_flag=True, help="Skip UFW firewall configuration")
@click.option("--skip-verify", is_flag=True, help="Skip post-deployment verification")
@click.option("--corporate-vpn", default=None, help="Corporate VPN interface name (e.g., tun0)")
@click.option("--yes", is_flag=True, help="Skip confirmation prompts")
def install(ctx, box, ip, user, version, skip_jlink, skip_firewall, skip_verify, corporate_vpn, yes):
    """
    Install lager box code onto a new box
    """
    # 1. Resolve box name to IP and username if --box is provided
    if box and ip:
        click.secho("Error: Cannot specify both --box and --ip", fg='red', err=True)
        ctx.exit(1)

    if box:
        # Look up IP from box storage
        stored_ip = get_box_ip(box)
        if not stored_ip:
            click.secho(f"Error: Box '{box}' not found in configuration", fg='red', err=True)
            click.secho("Use 'lager boxes' to see available boxes, or use --ip to specify directly.", fg='yellow', err=True)
            ctx.exit(1)
        ip = stored_ip

        # Look up username from box storage (if not explicitly provided)
        if user is None:
            stored_user = get_box_user(box)
            user = stored_user or "lagerdata"
    elif ip is None:
        click.secho("Error: Either --box or --ip is required", fg='red', err=True)
        ctx.exit(1)
    else:
        # Default username if not provided
        if user is None:
            user = "lagerdata"

    # 2. Validate address (IP or hostname)
    try:
        ip = validate_ip_or_hostname(ip)
    except ValueError as e:
        click.secho(f"Error: {e}", fg='red', err=True)
        for line in VALID_FORMATS_CHEATSHEET:
            click.echo(line, err=True)
        ctx.exit(1)

    ssh_host = f"{user}@{ip}"

    # 3. Verify deploy script exists (check before SSH to avoid wasted effort)
    try:
        deploy_script = get_script_path("setup_and_deploy_box.sh")
        if not deploy_script.exists():
            raise FileNotFoundError(f"Script not found at {deploy_script}")
    except FileNotFoundError as e:
        click.secho("Error: Deployment script not found", fg='red', err=True)
        click.secho(f"Details: {e}", fg='yellow', err=True)
        click.secho("Try reinstalling lager-cli: pip install --upgrade lager-cli", fg='yellow', err=True)
        ctx.exit(1)

    # 4. Check SSH connectivity (with password fallback)
    click.echo(f"Checking SSH connectivity to {ssh_host}...")
    try:
        result = subprocess.run(
            ["ssh", "-o", "ConnectTimeout=10", "-o", "BatchMode=yes", ssh_host, "echo ok"],
            capture_output=True,
            text=True,
            timeout=15,
        )
        if result.returncode != 0:
            stderr = result.stderr.lower() if result.stderr else ""

            # Check for specific SSH error types
            if "permission denied" in stderr or "publickey" in stderr:
                # SSH keys not configured - offer password authentication
                click.secho("SSH keys not configured", fg='yellow')
                click.echo()
                click.echo("SSH key authentication is not set up for this box.")
                click.echo("You can either:")
                click.echo(f"  1. Enter your password now (will be prompted during installation)")
                click.echo(f"  2. Set up SSH keys first with: ssh-copy-id {ssh_host}")
                click.echo()

                if yes or click.confirm("Would you like to continue with password authentication?"):
                    click.echo()
                    click.echo("Please enter your password to verify connectivity:")
                    test_result = subprocess.run(
                        ["ssh", "-o", "ConnectTimeout=10", "-o", "NumberOfPasswordPrompts=1",
                         ssh_host, "echo ok"],
                        timeout=60
                    )
                    if test_result.returncode != 0:
                        LagerError(
                            'Password authentication failed.',
                            cause='The box did not accept that password.',
                            fixes=[
                                'Double-check the password and try again.',
                                'The default box login is the user "lagerdata".',
                            ],
                        ).die()
                    click.secho("Password authentication successful!", fg='green')
                    click.echo()
                    click.secho("Note: You may be prompted for your password multiple times during installation.", fg='yellow')
                else:
                    click.secho("Installation cancelled.", fg='yellow')
                    ctx.exit(0)
            elif "connection refused" in stderr or "no route to host" in stderr:
                ssh_error(result.stderr, ip).die()
            elif "host key verification failed" in stderr:
                # Distinguish between new host (not in known_hosts) vs changed key
                if host_in_known_hosts(ip):
                    # Changed key - security concern, require manual intervention.
                    ssh_error("host key verification failed", ip).die()
                else:
                    # New host - offer to accept the key
                    click.secho("New SSH host detected", fg='yellow')
                    click.echo()
                    click.echo(f"This is the first time connecting to {ip}.")
                    click.echo("The host key needs to be added to your known_hosts file.")
                    click.echo()

                    if yes or click.confirm("Do you want to accept the host key and continue?"):
                        click.echo()
                        click.echo("Accepting host key...")
                        # Use StrictHostKeyChecking=accept-new to accept new keys only
                        accept_result = subprocess.run(
                            ["ssh", "-o", "ConnectTimeout=10", "-o", "StrictHostKeyChecking=accept-new",
                             "-o", "BatchMode=yes", ssh_host, "echo ok"],
                            capture_output=True,
                            text=True,
                            timeout=15
                        )
                        if accept_result.returncode == 0:
                            click.secho("Host key accepted!", fg='green')
                        else:
                            # Key accepted but auth failed - likely needs password
                            accept_stderr = accept_result.stderr.lower() if accept_result.stderr else ""
                            if "permission denied" in accept_stderr or "publickey" in accept_stderr:
                                click.secho("Host key accepted!", fg='green')
                                click.echo()
                                click.secho("SSH keys not configured", fg='yellow')
                                click.echo()
                                click.echo("SSH key authentication is not set up for this box.")
                                click.echo("You can either:")
                                click.echo(f"  1. Enter your password now (will be prompted during installation)")
                                click.echo(f"  2. Set up SSH keys first with: ssh-copy-id {ssh_host}")
                                click.echo()

                                if yes or click.confirm("Would you like to continue with password authentication?"):
                                    click.echo()
                                    click.echo("Please enter your password to verify connectivity:")
                                    test_result = subprocess.run(
                                        ["ssh", "-o", "ConnectTimeout=10", "-o", "NumberOfPasswordPrompts=1",
                                         ssh_host, "echo ok"],
                                        timeout=60
                                    )
                                    if test_result.returncode != 0:
                                        LagerError(
                                            'Password authentication failed.',
                                            cause='The box did not accept that password.',
                                            fixes=[
                                                'Double-check the password and try again.',
                                                'The default box login is the user "lagerdata".',
                                            ],
                                        ).die()
                                    click.secho("Password authentication successful!", fg='green')
                                    click.echo()
                                    click.secho("Note: You may be prompted for your password multiple times during installation.", fg='yellow')
                                else:
                                    click.secho("Installation cancelled.", fg='yellow')
                                    ctx.exit(0)
                            else:
                                click.secho(f"Error: SSH connection failed after accepting host key", fg='red', err=True)
                                if accept_result.stderr:
                                    click.echo(f"Details: {accept_result.stderr.strip()}", err=True)
                                ctx.exit(1)
                    else:
                        click.secho("Installation cancelled.", fg='yellow')
                        ctx.exit(0)
            elif "could not resolve hostname" in stderr or "name or service not known" in stderr:
                ssh_error(result.stderr, ip).die()
            else:
                # Generic SSH failure - still offer password auth as fallback
                click.secho("SSH key authentication failed", fg='yellow')
                click.echo()
                if result.stderr:
                    click.echo(f"SSH error: {result.stderr.strip()}", err=True)
                click.echo()
                click.echo("You can either:")
                click.echo(f"  1. Enter your password now (will be prompted during installation)")
                click.echo(f"  2. Set up SSH keys first with: ssh-copy-id {ssh_host}")
                click.echo()

                if yes or click.confirm("Would you like to continue with password authentication?"):
                    click.echo()
                    click.echo("Please enter your password to verify connectivity:")
                    test_result = subprocess.run(
                        ["ssh", "-o", "ConnectTimeout=10", "-o", "NumberOfPasswordPrompts=1",
                         ssh_host, "echo ok"],
                        timeout=60
                    )
                    if test_result.returncode != 0:
                        LagerError(
                            'Password authentication failed.',
                            cause='The box did not accept that password.',
                            fixes=[
                                'Double-check the password and try again.',
                                'The default box login is the user "lagerdata".',
                            ],
                        ).die()
                    click.secho("Password authentication successful!", fg='green')
                    click.echo()
                    click.secho("Note: You may be prompted for your password multiple times during installation.", fg='yellow')
                else:
                    click.secho("Installation cancelled.", fg='yellow')
                    ctx.exit(0)
        else:
            click.secho("SSH connection OK", fg='green')
    except subprocess.TimeoutExpired:
        LagerError(
            f'SSH connection to {ssh_host} timed out after 15 seconds.',
            cause='The box did not respond — it may be offline, or packets are being dropped.',
            fixes=[
                f'Confirm the box is online: ping {ip}',
                'Check your network / VPN connection, then retry.',
            ],
        ).die()
    except FileNotFoundError:
        LagerError(
            "The 'ssh' command was not found on this machine.",
            cause='An SSH client is required to install onto a box.',
            fixes=[
                'macOS/Linux: ssh is usually preinstalled — check with: which ssh',
                'Windows: install OpenSSH, or run this from Git Bash.',
            ],
        ).die()
    except Exception as e:
        LagerError(
            'SSH connectivity check failed.',
            cause=str(e),
            fixes=[f'Verify the box is online and reachable: lager hello --box {ip}'],
            raw=e,
        ).die()

    click.echo()

    # 5. Display summary and confirm
    click.echo()
    if box:
        click.secho(f"Installing lager to {box} ({ip})...", fg='cyan', bold=True)
    else:
        click.secho(f"Installing lager to {ip}...", fg='cyan', bold=True)
    click.echo(f"  Version: {version}")
    click.echo(f"  User: {user}")
    click.echo(f"  Mode: Git sparse checkout (enables 'lager update')")
    if skip_jlink:
        click.echo(f"  Skip J-Link: Yes")
    if skip_firewall:
        click.echo(f"  Skip Firewall: Yes")
    if corporate_vpn:
        click.echo(f"  Corporate VPN: {corporate_vpn}")
    click.echo()

    if not yes:
        if not click.confirm("Proceed with installation?", default=True):
            click.echo("Installation cancelled.")
            ctx.exit(0)

    click.echo()

    # 6. Run setup_and_deploy_box.sh with --sparse
    #
    # The deploy script restarts the on-box docker container, which would
    # clobber a `lager python` test mid-run if one were active. Acquire
    # the auto-lock for the duration so a concurrent test fail-fasts
    # (dev) or queues (CI) instead of getting killed.
    click.secho("Running box deployment...", fg='cyan')
    click.echo("This may take several minutes.\n")

    deploy_args = [str(deploy_script), ip, "--user", user, "--version", version, "--skip-add-box"]

    if skip_jlink:
        deploy_args.append("--skip-jlink")
    if skip_firewall:
        deploy_args.append("--skip-firewall")
    if skip_verify:
        deploy_args.append("--skip-verify")
    if corporate_vpn:
        deploy_args.extend(["--corporate-vpn", corporate_vpn])

    with auto_lock_around_command(ip, box or ip, 'install'):
        try:
            # Run the deploy script, streaming output to the terminal
            result = subprocess.run(
                deploy_args,
                check=False,
                timeout=1800,  # 30 minute timeout
            )

            if result.returncode != 0:
                click.echo()
                click.secho("Deployment failed!", fg='red', err=True)
                click.secho("Check the output above for details.", fg='yellow', err=True)
                ctx.exit(1)

        except subprocess.TimeoutExpired:
            click.echo()
            click.secho("Deployment timed out after 30 minutes.", fg='red', err=True)
            ctx.exit(1)
        except Exception as e:
            click.echo()
            click.secho("Deployment failed!", fg='red', err=True)
            ctx.exit(1)

    click.echo()
    click.secho("Box deployment complete!", fg='green', bold=True)
    click.echo()

    # 6.5. Store version information on the box
    from ... import __version__ as cli_version
    from ...box_storage import update_box_version

    click.echo("Storing version information...")
    click.echo("(May require sudo password if passwordless sudo is not configured)")
    click.echo()

    # Read CLI version from deployed cli/__init__.py
    read_version_cmd = (
        'cd ~/box && '
        'if [ -f cli/__init__.py ]; then '
        'grep -E "^__version__\\s*=\\s*" cli/__init__.py 2>/dev/null | '
        'sed -E "s/__version__\\s*=\\s*[\'\\"]([^\'\\\"]+)[\'\\\"]/\\1/"; '
        'elif [ -f box/cli/__init__.py ]; then '
        'grep -E "^__version__\\s*=\\s*" box/cli/__init__.py 2>/dev/null | '
        'sed -E "s/__version__\\s*=\\s*[\'\\"]([^\'\\\"]+)[\'\\\"]/\\1/"; '
        'fi'
    )

    try:
        result = subprocess.run(
            ["ssh", ssh_host, read_version_cmd],
            capture_output=True,
            text=True,
            timeout=10
        )

        if result.returncode == 0 and result.stdout.strip():
            box_cli_version = result.stdout.strip()
        else:
            # Fallback to local CLI version
            box_cli_version = cli_version

        version_content = f'{box_cli_version}|{cli_version}'

        # Write version file using sudo (may prompt for password)
        write_version_cmd = (
            f'echo "{version_content}" > /tmp/lager_version_tmp && '
            'sudo rm -f /etc/lager/version && '
            'sudo mv /tmp/lager_version_tmp /etc/lager/version && '
            'sudo chmod 666 /etc/lager/version'
        )

        subprocess.run(
            ["ssh", "-t", ssh_host, write_version_cmd],
            timeout=120,  # Increased from 30 to match update.py timeout
            stderr=subprocess.DEVNULL,  # Suppress "Shared connection closed" noise
        )

        click.secho(f"Version {box_cli_version} stored on box", fg='green')

    except Exception as e:
        click.secho(f"Warning: Could not store version information: {e}", fg='yellow')
        box_cli_version = version  # Fallback to requested version

    click.echo()

    # 6.7. Bootstrap passwordless sudo for `lager box-config apply`.
    #
    # `lager box-config apply` needs root on the host for apt-get install,
    # sysctl writes, and mount-path mkdir/chown. Those run over SSH in a
    # non-interactive context (no TTY for sudo to prompt against), so the
    # rule must grant NOPASSWD up front. The rule content lives in
    # _host_ops.boxcfg_sudoers_rules — it must name the actual login user
    # (it previously hardcoded `lagerdata`, so on boxes with a different
    # login user the grant never matched and the verify below always
    # warned "sudo -n apt-get still fails").
    #
    # Idempotent: re-running install overwrites the file with the same
    # content. Failure here is a warning, not fatal — the box is otherwise    # installed; the operator can apply the rule manually later.
    click.echo()
    click.secho("Configuring passwordless sudo for `lager box-config apply`...", fg='cyan')
    click.echo("(One-time setup. You'll be prompted for the sudo password on the box.)")
    click.echo()

    if not is_valid_unix_username(user):
        # The username lands inside a root-owned sudoers file; refuse to
        # interpolate anything that isn't a plain unix username.
        click.secho(
            f"Warning: username {user!r} is not a plain unix username; skipping the "
            "passwordless-sudo bootstrap. `lager box-config apply` will require "
            "manual sudoers setup on this box. See `lager box-config apply --help` "
            "for the snippet to paste.",
            fg='yellow', err=True,
        )
    else:
        # Skip the bootstrap (and its sudo password prompt) when the grant is
        # already live — marker file present AND `sudo -n apt-get` actually
        # works as this user, the same functional probe `lager update` uses.
        # Re-installs then never prompt here at all. This matters because the
        # prompt lands at the very end of a long install, when the operator
        # may have stepped away.
        try:
            precheck = subprocess.run(
                ["ssh", "-o", "BatchMode=yes", ssh_host,
                 "test -f /etc/lager/.boxcfg-sudoers-v2 "
                 "&& sudo -n DEBIAN_FRONTEND=noninteractive apt-get --version >/dev/null 2>&1"],
                capture_output=True, timeout=15,
            )
            already_configured = precheck.returncode == 0
        except Exception:
            already_configured = False

        if already_configured:
            click.secho("Passwordless sudo for `lager box-config` already configured", fg='green')
        else:
            sudoers_cmd = boxcfg_sudoers_bootstrap_cmd(user)

            try:
                # Interactive: waits on a human typing the box's sudo password
                # at the end of a long install. A 120s timeout here killed the
                # bootstrap mid-prompt for a slow (or absent) operator, so give
                # them 10 minutes; the timeout only guards a genuine hang.
                bootstrap_result = subprocess.run(
                    ["ssh", "-t", ssh_host, sudoers_cmd],
                    timeout=600,
                )
                if bootstrap_result.returncode != 0:
                    click.secho(
                        "Warning: Sudoers rule could not be installed. `lager box-config apply` "
                        "will require manual sudoers setup on this box. See `lager box-config "
                        "apply --help` for the snippet to paste.",
                        fg='yellow', err=True,
                    )
                else:
                    # Verify: marker file written by the bootstrap above (means the
                    # current rule shape was installed) + functional apt-get probe
                    # (means the NOPASSWD/SETENV grant is live). Marker name carries
                    # a version suffix so older boxes upgrading to a future rule
                    # shape re-bootstrap automatically.
                    verify_result = subprocess.run(
                        ["ssh", "-o", "BatchMode=yes", ssh_host,
                         "test -f /etc/lager/.boxcfg-sudoers-v2 "
                         "&& sudo -n DEBIAN_FRONTEND=noninteractive apt-get --version >/dev/null 2>&1"],
                        capture_output=True, timeout=15,
                    )
                    if verify_result.returncode == 0:
                        click.secho("Passwordless sudo for `lager box-config` configured", fg='green')
                    else:
                        click.secho(
                            "Warning: Sudoers file installed but `sudo -n apt-get` still fails. "
                            "Check /etc/sudoers.d/lager-box-config on the box for syntax issues.",
                            fg='yellow', err=True,
                        )
            except (subprocess.TimeoutExpired, Exception) as e:
                click.secho(
                    f"Warning: Sudoers bootstrap failed: {e}. `lager box-config apply` "
                    "will require manual sudoers setup.",
                    fg='yellow', err=True,
                )

    click.echo()

    # 7. Prompt to add box to .lager config (skip if --box was used since it's already configured)
    if not box and not yes:
        if click.confirm("Add this box to your configuration?", default=True):
            box_name = click.prompt("Box name", type=str)
            if box_name and box_name.strip():
                add_box(box_name.strip(), ip, user=user, version=box_cli_version)
                click.secho(f"Added '{box_name}' -> {ip} to .lager config", fg='green')
                click.echo()
                click.secho(f"You can now use: lager hello --box {box_name}", fg='cyan')
            else:
                click.secho("Skipped adding box to config (empty name)", fg='yellow')
    elif box:
        # Update existing box with correct version
        update_box_version(box, box_cli_version)

    click.echo()
    click.secho("Installation complete!", fg='green', bold=True)
    click.echo()
    click.secho("Next steps:", fg='cyan')
    click.echo("  - Verify the box is working: lager hello --box [BOX_NAME]")
    click.echo("  - Please run 'lager update --box [BOX_NAME]' to update the box to the latest version")