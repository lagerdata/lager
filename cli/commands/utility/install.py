# Copyright 2024-2026 Lager Data LLC
# SPDX-License-Identifier: Apache-2.0

"""
    lager.commands.utility.install

    Install lager box code onto a new box
"""
import click
import subprocess
import ipaddress
import tempfile
import shutil
from pathlib import Path
from importlib import resources
from ...box_storage import add_box, get_box_ip, get_box_user
from ...core.ssh_utils import host_in_known_hosts


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
@click.option("--ip", default=None, help="Target box IP address")
@click.option("--user", default=None, help="SSH username (default: lagerdata, or stored username if using --box)")
@click.option("--branch", default="main", help="Git branch to deploy (default: main)")
@click.option("--skip-jlink", is_flag=True, help="Skip J-Link installation")
@click.option("--skip-firewall", is_flag=True, help="Skip UFW firewall configuration")
@click.option("--skip-verify", is_flag=True, help="Skip post-deployment verification")
@click.option("--corporate-vpn", default=None, help="Corporate VPN interface name (e.g., tun0)")
@click.option("--yes", is_flag=True, help="Skip confirmation prompts")
def install(ctx, box, ip, user, branch, skip_jlink, skip_firewall, skip_verify, corporate_vpn, yes):
    """
    Install lager box code onto a new box.
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

    # 2. Validate IP address
    try:
        ipaddress.ip_address(ip)
    except ValueError:
        click.secho(f"Error: '{ip}' is not a valid IP address", fg='red', err=True)
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
                        click.secho("Error: Password authentication failed", fg='red', err=True)
                        click.echo("Please verify your password and try again.", err=True)
                        ctx.exit(1)
                    click.secho("Password authentication successful!", fg='green')
                    click.echo()
                    click.secho("Note: You may be prompted for your password multiple times during installation.", fg='yellow')
                else:
                    click.secho("Installation cancelled.", fg='yellow')
                    ctx.exit(0)
            elif "connection refused" in stderr:
                click.secho("Error: SSH connection refused", fg='red', err=True)
                click.echo(err=True)
                click.echo("The box is reachable but SSH service is not running on port 22.", err=True)
                click.echo(err=True)
                click.echo("Possible causes:", err=True)
                click.echo("  - SSH server is not installed or running", err=True)
                click.echo("  - SSH is running on a non-standard port", err=True)
                click.echo("  - Firewall is blocking port 22", err=True)
                ctx.exit(1)
            elif "no route to host" in stderr:
                click.secho("Error: No route to host", fg='red', err=True)
                click.echo(err=True)
                click.echo(f"Cannot reach {ip} - network path does not exist.", err=True)
                click.echo(err=True)
                click.echo("Possible causes:", err=True)
                click.echo("  - Box is on a different network", err=True)
                click.echo("  - VPN is not connected", err=True)
                click.echo("  - IP address is incorrect", err=True)
                ctx.exit(1)
            elif "host key verification failed" in stderr:
                # Distinguish between new host (not in known_hosts) vs changed key
                if host_in_known_hosts(ip):
                    # Changed key - security concern, require manual intervention
                    click.secho("Error: Host key verification failed", fg='red', err=True)
                    click.echo(err=True)
                    click.echo("The SSH host key has changed, which could indicate:", err=True)
                    click.echo("  - The box was reinstalled or reimaged", err=True)
                    click.echo("  - A different device is using this IP address", err=True)
                    click.echo(err=True)
                    click.echo("If you trust this device, remove the old key with:", err=True)
                    click.echo(f"  ssh-keygen -R {ip}", err=True)
                    ctx.exit(1)
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
                                        click.secho("Error: Password authentication failed", fg='red', err=True)
                                        click.echo("Please verify your password and try again.", err=True)
                                        ctx.exit(1)
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
                click.secho("Error: Could not resolve hostname", fg='red', err=True)
                click.echo(err=True)
                click.echo(f"DNS lookup failed for {ip}.", err=True)
                click.echo("Check that the hostname or IP address is correct.", err=True)
                ctx.exit(1)
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
                        click.secho("Error: Password authentication failed", fg='red', err=True)
                        click.echo("Please verify your password and try again.", err=True)
                        ctx.exit(1)
                    click.secho("Password authentication successful!", fg='green')
                    click.echo()
                    click.secho("Note: You may be prompted for your password multiple times during installation.", fg='yellow')
                else:
                    click.secho("Installation cancelled.", fg='yellow')
                    ctx.exit(0)
        else:
            click.secho("SSH connection OK", fg='green')
    except subprocess.TimeoutExpired:
        click.secho("Error: SSH connection timed out", fg='red', err=True)
        click.echo(err=True)
        click.echo(f"Could not connect to {ssh_host} within 15 seconds.", err=True)
        click.echo(err=True)
        click.echo("Possible causes:", err=True)
        click.echo("  - Box is offline or powered down", err=True)
        click.echo("  - Network connectivity issue", err=True)
        click.echo("  - Firewall is dropping packets (not rejecting)", err=True)
        click.echo(err=True)
        click.echo("Verify the box is online and try: ping " + ip, err=True)
        ctx.exit(1)
    except FileNotFoundError:
        click.secho("Error: SSH client not found", fg='red', err=True)
        click.echo(err=True)
        click.echo("The 'ssh' command is not installed or not in PATH.", err=True)
        click.echo(err=True)
        click.echo("Install SSH client:", err=True)
        click.echo("  macOS/Linux: Usually pre-installed, try 'which ssh'", err=True)
        click.echo("  Windows: Install OpenSSH or use Git Bash", err=True)
        ctx.exit(1)
    except Exception as e:
        click.secho(f"Error: {e}", fg='red', err=True)
        ctx.exit(1)

    click.echo()

    # 5. Display summary and confirm
    click.echo()
    if box:
        click.secho(f"Installing lager to {box} ({ip})...", fg='cyan', bold=True)
    else:
        click.secho(f"Installing lager to {ip}...", fg='cyan', bold=True)
    click.echo(f"  Branch: {branch}")
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
    click.secho("Running box deployment...", fg='cyan')
    click.echo("This may take several minutes.\n")

    deploy_args = [str(deploy_script), ip, "--user", user, "--branch", branch, "--skip-add-box"]

    if skip_jlink:
        deploy_args.append("--skip-jlink")
    if skip_firewall:
        deploy_args.append("--skip-firewall")
    if skip_verify:
        deploy_args.append("--skip-verify")
    if corporate_vpn:
        deploy_args.extend(["--corporate-vpn", corporate_vpn])

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
        box_cli_version = branch  # Fallback to branch name

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
    click.echo("  - Verify the box is working: lager hello --box <box-name>")
    click.echo("  - Please run 'lager update --box <box-name>' to update the box to the latest version")