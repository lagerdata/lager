# Copyright 2024-2026 Lager Data LLC
# SPDX-License-Identifier: Apache-2.0

"""
    lager.commands.utility.uninstall

    Uninstall Lager box code from a box
"""
import click
import subprocess
import ipaddress
from ...box_storage import get_box_ip, get_box_user, get_box_name_by_ip, delete_box
from ...core.ssh_utils import host_in_known_hosts, get_ssh_connection_pool


@click.command()
@click.pass_context
@click.option("--box", default=None, help="Box name (uses stored IP and username)")
@click.option("--ip", default=None, help="Target box IP address")
@click.option("--user", default=None, help="SSH username (default: lagerdata, or stored username if using --box)")
@click.option("--keep-config", is_flag=True, help="Keep /etc/lager directory (saved nets, etc.)")
@click.option("--keep-docker-images", is_flag=True, help="Keep Docker images (only remove containers)")
@click.option("--all", "remove_all", is_flag=True, help="Remove everything including udev rules, sudoers, third_party, and deploy keys")
@click.option("--yes", is_flag=True, help="Skip confirmation prompts")
@click.option("--dry-run", is_flag=True, help="Show what would be removed without making changes")
def uninstall(ctx, box, ip, user, keep_config, keep_docker_images, remove_all, yes, dry_run):
    """
    Uninstall Lager box code from a box.
    """
    # 1. Resolve box name to IP and username if --box is provided
    if box and ip:
        click.secho("Error: Cannot specify both --box and --ip", fg='red', err=True)
        ctx.exit(1)

    if box:
        stored_ip = get_box_ip(box)
        if not stored_ip:
            click.secho(f"Error: Box '{box}' not found in configuration", fg='red', err=True)
            click.secho("Use 'lager boxes' to see available boxes, or use --ip to specify directly.", fg='yellow', err=True)
            ctx.exit(1)
        ip = stored_ip

        from ...box_storage import _check_box_lock
        _check_box_lock(ip, box)

        if user is None:
            stored_user = get_box_user(box)
            user = stored_user or "lagerdata"
    elif ip is None:
        click.secho("Error: Either --box or --ip is required", fg='red', err=True)
        ctx.exit(1)
    else:
        if user is None:
            user = "lagerdata"

    # 2. Validate IP address
    try:
        ipaddress.ip_address(ip)
    except ValueError:
        click.secho(f"Error: '{ip}' is not a valid IP address", fg='red', err=True)
        ctx.exit(1)

    ssh_host = f"{user}@{ip}"

    # 3. Check SSH connectivity (with password fallback)
    click.echo(f"Checking SSH connectivity to {ssh_host}...")
    use_interactive_ssh = False
    use_multiplexing = False
    try:
        result = subprocess.run(
            ["ssh", "-o", "ConnectTimeout=10", "-o", "BatchMode=yes", ssh_host, "echo ok"],
            capture_output=True,
            text=True,
            timeout=15,
        )
        if result.returncode != 0:
            stderr = result.stderr.lower() if result.stderr else ""

            if "permission denied" in stderr or "publickey" in stderr:
                click.secho("SSH keys not configured", fg='yellow')
                click.echo()
                click.echo("SSH key authentication is not set up for this box.")
                click.echo("You can either:")
                click.echo(f"  1. Enter your password now (will be prompted for each SSH command)")
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
                    use_interactive_ssh = True
                else:
                    click.secho("Uninstall cancelled.", fg='yellow')
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
                if host_in_known_hosts(ip):
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
                    click.secho("New SSH host detected", fg='yellow')
                    click.echo()
                    click.echo(f"This is the first time connecting to {ip}.")
                    click.echo("The host key needs to be added to your known_hosts file.")
                    click.echo()

                    if yes or click.confirm("Do you want to accept the host key and continue?"):
                        click.echo()
                        click.echo("Accepting host key...")
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
                            accept_stderr = accept_result.stderr.lower() if accept_result.stderr else ""
                            if "permission denied" in accept_stderr or "publickey" in accept_stderr:
                                click.secho("Host key accepted!", fg='green')
                                click.echo()
                                click.secho("SSH keys not configured", fg='yellow')
                                click.echo()
                                click.echo("SSH key authentication is not set up for this box.")
                                click.echo("You can either:")
                                click.echo(f"  1. Enter your password now (will be prompted for each SSH command)")
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
                                    use_interactive_ssh = True
                                else:
                                    click.secho("Uninstall cancelled.", fg='yellow')
                                    ctx.exit(0)
                            else:
                                click.secho("Error: SSH connection failed after accepting host key", fg='red', err=True)
                                if accept_result.stderr:
                                    click.echo(f"Details: {accept_result.stderr.strip()}", err=True)
                                ctx.exit(1)
                    else:
                        click.secho("Uninstall cancelled.", fg='yellow')
                        ctx.exit(0)
            elif "could not resolve hostname" in stderr or "name or service not known" in stderr:
                click.secho("Error: Could not resolve hostname", fg='red', err=True)
                click.echo(err=True)
                click.echo(f"DNS lookup failed for {ip}.", err=True)
                click.echo("Check that the hostname or IP address is correct.", err=True)
                ctx.exit(1)
            else:
                click.secho("SSH key authentication failed", fg='yellow')
                click.echo()
                if result.stderr:
                    click.echo(f"SSH error: {result.stderr.strip()}", err=True)
                click.echo()
                click.echo("You can either:")
                click.echo(f"  1. Enter your password now (will be prompted for each SSH command)")
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
                    use_interactive_ssh = True
                else:
                    click.secho("Uninstall cancelled.", fg='yellow')
                    ctx.exit(0)
        else:
            click.secho("SSH connection OK", fg='green')
            use_multiplexing = True
    except subprocess.TimeoutExpired:
        click.secho(f"Error: SSH connection timed out", fg='red', err=True)
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
        click.secho("Error: SSH command not found", fg='red', err=True)
        click.secho("Please install OpenSSH client:", err=True)
        import platform
        if platform.system() == "Darwin":
            click.secho("  macOS: SSH should be pre-installed. Check your PATH.", err=True)
        elif platform.system() == "Windows":
            click.secho("  Windows: Install OpenSSH via Settings > Apps > Optional Features", err=True)
        else:
            click.secho("  Linux: sudo apt install openssh-client (Debian/Ubuntu)", err=True)
            click.secho("         sudo dnf install openssh-clients (Fedora/RHEL)", err=True)
        ctx.exit(1)
    except Exception as e:
        click.secho(f"Error: {e}", fg='red', err=True)
        ctx.exit(1)

    click.echo()

    # Set up SSH connection multiplexing for key-based auth
    ssh_pool = None
    if use_multiplexing:
        ssh_pool = get_ssh_connection_pool()
        if not ssh_pool.ensure_connection(ip, user):
            ssh_pool = None

    # Helper function to run SSH commands
    def run_ssh(cmd, description, allow_fail=False):
        """Run an SSH command and handle errors."""
        click.echo(f"  {description}...", nl=False)
        try:
            ssh_cmd = ["ssh"]
            if ssh_pool:
                ssh_cmd.extend(ssh_pool.get_ssh_options(ip))
            if not use_interactive_ssh:
                ssh_cmd.extend(["-o", "BatchMode=yes"])
            ssh_cmd.extend([ssh_host, cmd])

            if use_interactive_ssh:
                result = subprocess.run(
                    ssh_cmd,
                    timeout=120,
                )
            else:
                result = subprocess.run(
                    ssh_cmd,
                    capture_output=True,
                    text=True,
                    timeout=60,
                )
            if result.returncode == 0:
                click.secho(" done", fg='green')
                return True
            elif allow_fail:
                click.secho(" skipped", fg='yellow')
                return True
            else:
                click.secho(" failed", fg='red')
                if not use_interactive_ssh and hasattr(result, 'stderr') and result.stderr:
                    stderr_text = result.stderr.strip()
                    click.secho(f"    Error: {stderr_text}", fg='red', err=True)
                    if "Permission denied" in stderr_text:
                        click.secho("    Hint: This may require sudo permissions", err=True)
                    elif "No such file" in stderr_text:
                        click.secho("    Hint: File or directory does not exist", err=True)
                elif not use_interactive_ssh and hasattr(result, 'stdout') and result.stdout:
                    stdout_text = result.stdout.strip()
                    if stdout_text:
                        click.secho(f"    Output: {stdout_text}", fg='yellow', err=True)
                return False
        except subprocess.TimeoutExpired:
            click.secho(" timeout", fg='yellow')
            click.secho("    Command timed out. The box may be slow or unresponsive.", err=True)
            return False
        except Exception as e:
            click.secho(f" error: {e}", fg='red')
            return False

    # Helper to run SSH query commands (for --dry-run)
    def query_ssh(cmd):
        """Run an SSH command and return stdout, or None on failure."""
        try:
            ssh_cmd = ["ssh"]
            if ssh_pool:
                ssh_cmd.extend(ssh_pool.get_ssh_options(ip))
            if not use_interactive_ssh:
                ssh_cmd.extend(["-o", "BatchMode=yes"])
            ssh_cmd.extend([ssh_host, cmd])

            result = subprocess.run(
                ssh_cmd,
                capture_output=True,
                text=True,
                timeout=30,
            )
            if result.returncode == 0:
                return result.stdout.strip()
            return None
        except Exception:
            return None

    # --dry-run mode: query box state and display without changing anything
    if dry_run:
        if box:
            click.secho(f"Dry run: inspecting lager state on {box} ({ip})...", fg='cyan', bold=True)
        else:
            click.secho(f"Dry run: inspecting lager state on {ip}...", fg='cyan', bold=True)
        click.echo()

        # Docker containers
        click.secho("Docker containers:", fg='cyan')
        containers = query_ssh("docker ps -a --filter name=lager --filter name=pigpio --format '{{.Names}}\\t{{.Status}}' 2>/dev/null")
        if containers:
            for line in containers.splitlines():
                click.echo(f"  {line}")
        else:
            click.echo("  (none found)")

        # Docker images
        click.secho("Docker images:", fg='cyan')
        images = query_ssh("docker images --format '{{.Repository}}:{{.Tag}}\\t{{.Size}}' 2>/dev/null")
        if images:
            for line in images.splitlines():
                click.echo(f"  {line}")
        else:
            click.echo("  (none found)")

        # Docker network
        click.secho("Docker networks:", fg='cyan')
        networks = query_ssh("docker network ls --filter name=lagernet --format '{{.Name}}' 2>/dev/null")
        if networks:
            click.echo(f"  {networks}")
        else:
            click.echo("  lagernet: (not found)")

        # ~/box directory
        click.secho("Box directory:", fg='cyan')
        box_dir = query_ssh("du -sh ~/box 2>/dev/null")
        if box_dir:
            click.echo(f"  ~/box: {box_dir.split()[0]}")
        else:
            click.echo("  ~/box: (not found)")

        # /etc/lager directory
        click.secho("Config directory:", fg='cyan')
        etc_lager = query_ssh("sudo du -sh /etc/lager 2>/dev/null")
        if etc_lager:
            click.echo(f"  /etc/lager: {etc_lager.split()[0]}")
        else:
            click.echo("  /etc/lager: (not found)")

        if remove_all:
            click.echo()
            click.secho("Extended cleanup items (--all):", fg='cyan')

            # Udev rules
            udev = query_ssh("ls /etc/udev/rules.d/lager-*.rules /etc/udev/rules.d/*lager*.rules 2>/dev/null")
            click.echo(f"  Udev rules: {udev if udev else '(none found)'}")

            # Sudoers
            sudoers = query_ssh("ls /etc/sudoers.d/lagerdata-udev 2>/dev/null")
            click.echo(f"  Sudoers file: {'present' if sudoers else '(not found)'}")

            # Third party
            third_party = query_ssh("du -sh ~/third_party 2>/dev/null")
            if third_party:
                click.echo(f"  ~/third_party: {third_party.split()[0]}")
            else:
                click.echo("  ~/third_party: (not found)")

            # SSH keys (both legacy and current)
            legacy_key = query_ssh("ls ~/.ssh/lager_deploy_key 2>/dev/null")
            current_key = query_ssh("ls ~/.ssh/lager_box 2>/dev/null")
            click.echo(f"  Legacy deploy key (~/.ssh/lager_deploy_key): {'present' if legacy_key else '(not found)'}")
            click.echo(f"  Current SSH key (~/.ssh/lager_box): {'present' if current_key else '(not found)'}")

            # UFW status
            ufw_status = query_ssh("sudo ufw status 2>/dev/null | head -5")
            if ufw_status:
                click.echo(f"  UFW firewall: {ufw_status.splitlines()[0]}")
            else:
                click.echo("  UFW firewall: (not available)")

        click.echo()
        click.secho("No changes were made (dry run).", fg='yellow')

        # Clean up SSH multiplexing
        if ssh_pool:
            ssh_pool.close_connection(ip, user)
        return

    # 4. Display what will be removed and confirm
    if box:
        click.secho(f"Uninstalling lager from {box} ({ip})...", fg='cyan', bold=True)
    else:
        click.secho(f"Uninstalling lager from {ip}...", fg='cyan', bold=True)
    click.echo()
    click.secho("The following will be REMOVED:", fg='yellow', bold=True)
    click.echo("  - Docker containers (lager, pigpio)")
    click.echo("  - Docker network (lagernet)")
    if not keep_docker_images:
        click.echo("  - Docker images")
    click.echo("  - ~/box directory")

    if remove_all:
        click.echo("  - /etc/lager directory (saved nets)")
        click.echo("  - Udev rules (/etc/udev/rules.d/lager-*.rules)")
        click.echo("  - Sudoers file (/etc/sudoers.d/lagerdata-udev)")
        click.echo("  - ~/third_party directory")
        click.echo("  - SSH keys (~/.ssh/lager_box*, ~/.ssh/lager_deploy_key*)")
        click.echo("  - SSH config entries for lager")
        click.echo("  - UFW firewall rules (reset to SSH-only)")
    elif not keep_config:
        click.echo("  - /etc/lager directory (saved nets)")

    click.echo()

    if not yes:
        click.secho("WARNING: This action cannot be undone!", fg='red', bold=True)
        if not click.confirm("Are you sure you want to proceed?", default=False):
            click.echo("Uninstall cancelled.")
            if ssh_pool:
                ssh_pool.close_connection(ip, user)
            ctx.exit(0)

    click.echo()

    # 5. Stop and remove Docker containers (targeted to lager only)
    click.secho("[Step 1/5] Stopping Docker containers...", fg='cyan')
    run_ssh(
        "cd ~/box && docker compose down 2>/dev/null || true",
        "Running docker compose down",
        allow_fail=True
    )
    run_ssh("docker stop lager 2>/dev/null; docker rm -f lager 2>/dev/null || true", "Removing lager container", allow_fail=True)
    run_ssh("docker stop pigpio 2>/dev/null; docker rm -f pigpio 2>/dev/null || true", "Removing pigpio container", allow_fail=True)
    run_ssh("docker network rm lagernet 2>/dev/null || true", "Removing lagernet network", allow_fail=True)
    click.echo()

    # 6. Remove Docker images (unless --keep-docker-images)
    click.secho("[Step 2/5] Cleaning Docker...", fg='cyan')
    if not keep_docker_images:
        run_ssh("docker image prune -af 2>/dev/null || true", "Removing Docker images", allow_fail=True)
        run_ssh("docker builder prune -af 2>/dev/null || true", "Clearing Docker build cache", allow_fail=True)
    else:
        click.echo("  Skipping Docker image removal (--keep-docker-images)")
    click.echo()

    # 7. Remove ~/box directory
    click.secho("[Step 3/5] Removing box code...", fg='cyan')
    run_ssh("rm -rf ~/box", "Removing ~/box directory")
    click.echo()

    # 8. Remove /etc/lager (unless --keep-config, or with --all)
    click.secho("[Step 4/5] Removing configuration...", fg='cyan')
    if remove_all or not keep_config:
        run_ssh("sudo rm -rf /etc/lager 2>/dev/null || true", "Removing /etc/lager directory", allow_fail=True)
    else:
        click.echo("  Skipping /etc/lager removal (--keep-config)")
    click.echo()

    # 9. Remove additional components if --all
    click.secho("[Step 5/5] Cleaning up additional components...", fg='cyan')
    if remove_all:
        # Remove udev rules
        run_ssh(
            "sudo rm -f /etc/udev/rules.d/lager-*.rules /etc/udev/rules.d/*lager*.rules 2>/dev/null; "
            "sudo udevadm control --reload-rules 2>/dev/null || true",
            "Removing udev rules",
            allow_fail=True
        )

        # Remove sudoers file
        run_ssh(
            "sudo rm -f /etc/sudoers.d/lagerdata-udev 2>/dev/null || true",
            "Removing sudoers file",
            allow_fail=True
        )

        # Remove third_party directory
        run_ssh("rm -rf ~/third_party", "Removing ~/third_party directory", allow_fail=True)

        # Remove both legacy and current SSH keys
        run_ssh(
            "rm -f ~/.ssh/lager_deploy_key ~/.ssh/lager_deploy_key.pub "
            "~/.ssh/lager_box ~/.ssh/lager_box.pub",
            "Removing SSH keys (lager_deploy_key, lager_box)",
            allow_fail=True
        )

        # Clean up SSH config (remove both legacy and current lager entries)
        run_ssh(
            "sed -i '/# Lager deploy key/,/IdentityFile.*lager_deploy_key/d' ~/.ssh/config 2>/dev/null; "
            "sed -i '/# Lager box key/,/IdentityFile.*lager_box/d' ~/.ssh/config 2>/dev/null || true",
            "Cleaning SSH config",
            allow_fail=True
        )

        # Reset UFW firewall to defaults (SSH-only)
        run_ssh(
            "sudo ufw --force reset 2>/dev/null && "
            "sudo ufw default deny incoming 2>/dev/null && "
            "sudo ufw default allow outgoing 2>/dev/null && "
            "sudo ufw allow ssh 2>/dev/null && "
            "sudo ufw --force enable 2>/dev/null || true",
            "Resetting UFW firewall to defaults",
            allow_fail=True
        )
    else:
        click.echo("  Skipping additional cleanup (use --all for complete removal)")

    # Clean up SSH multiplexing
    if ssh_pool:
        ssh_pool.close_connection(ip, user)

    click.echo()
    click.secho("Uninstall complete!", fg='green', bold=True)
    click.echo()
    click.echo(f"The lager box code has been removed from {ip}.")

    if not remove_all and keep_config:
        click.echo()
        click.secho("Note: /etc/lager directory was preserved (contains saved nets).", fg='yellow')

    if not remove_all:
        click.echo()
        click.echo("To completely remove all lager components, run:")
        if box:
            click.secho(f"  lager uninstall --box {box} --all", fg='cyan')
        else:
            click.secho(f"  lager uninstall --ip {ip} --all", fg='cyan')

    if remove_all:
        click.echo()
        click.secho("Note: ~/.ssh/lager_box key pair is shared across boxes and was removed", fg='yellow')
        click.secho("from this box. If you have other boxes using it, you may need to", fg='yellow')
        click.secho("re-deploy the key with: ssh-copy-id -i ~/.ssh/lager_box <user>@<ip>", fg='yellow')

    # 10. Local config cleanup - offer to remove box from .lager config
    box_name = box
    if not box_name:
        box_name = get_box_name_by_ip(ip)

    if box_name:
        click.echo()
        if yes or click.confirm(f"Remove '{box_name}' from local .lager configuration?", default=True):
            if delete_box(box_name):
                click.secho(f"Removed '{box_name}' from .lager config.", fg='green')
            else:
                click.secho(f"'{box_name}' was not found in .lager config.", fg='yellow')
        else:
            click.echo(f"Kept '{box_name}' in .lager config.")
