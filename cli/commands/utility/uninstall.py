# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

"""
    lager.commands.utility.uninstall

    Uninstall Lager box code from a box
"""
import click
import os
import subprocess
from ...address_utils import validate_ip_or_hostname, VALID_FORMATS_CHEATSHEET
from ...box_storage import (
    auto_lock_around_command,
    delete_box,
    get_box_ip,
    get_box_name_by_ip,
    get_box_user,
)
from ...core.ssh_utils import host_in_known_hosts, get_ssh_connection_pool

# --- Privileged removal spec -------------------------------------------------
#
# Everything `lager install` (and `lager box-config apply`) creates on the box
# that needs root to remove, as (name, description, remote command). The
# confirmation listing, --dry-run inspection, the privileged session, and the
# unit tests all share this single source of truth, so the artifact list can't
# silently drift from what install creates again.
#
# All steps run in ONE interactive `ssh -t` session so sudo can prompt on
# boxes whose login user has no broad passwordless grant (the old per-command
# BatchMode + `|| true` pattern made every one of these fail silently there
# while reporting "done"). Order matters: the sudoers files are removed LAST,
# so the earlier steps can still ride a NOPASSWD grant or the sudo timestamp
# cached by the session's first prompt.
#
# Deliberately NOT removed (the box keeps working infrastructure): docker apt
# packages, the buildx plugin (including the /usr/local/lib/docker/cli-plugins
# fallback binary), the `dns` key lager merged into /etc/docker/daemon.json,
# pip packages (pyOCD etc.), and apt packages from `lager box-config apply`.
UNINSTALL_ALL_PRIV_STEPS = [
    (
        "udev_rules",
        "Removing instrument udev rules",
        "sudo rm -f /etc/udev/rules.d/99-instrument.rules "
        "/etc/udev/rules.d/99-lager-user.rules /etc/udev/rules.d/lager-*.rules "
        "&& sudo udevadm control --reload-rules && sudo udevadm trigger",
    ),
    (
        "modprobe",
        "Removing usbtmc blacklist",
        "sudo rm -f /etc/modprobe.d/blacklist-usbtmc.conf",
    ),
    (
        "sysctl",
        "Removing lager sysctl config",
        "sudo rm -f /etc/sysctl.d/99-lager-box-config.conf && sudo sysctl --system >/dev/null",
    ),
    (
        "firewall_script",
        "Removing firewall helper script",
        "sudo rm -f /usr/local/lib/lager/secure_box_firewall.sh",
    ),
    (
        "ufw_reset",
        "Resetting UFW firewall to defaults (SSH-only)",
        "if command -v ufw >/dev/null; then "
        "sudo ufw --force reset && sudo ufw default deny incoming && "
        "sudo ufw default allow outgoing && sudo ufw allow ssh && "
        "sudo ufw --force enable; fi",
    ),
    (
        "lager_group",
        "Removing 'lager' group",
        "if getent group lager >/dev/null; then sudo groupdel lager; fi",
    ),
    # LAST: removing these grants first would break the steps above on boxes
    # that rely on them.
    (
        "sudoers",
        "Removing lager sudoers files",
        "sudo rm -f /etc/sudoers.d/lagerdata-udev /etc/sudoers.d/lager-box-config "
        "/etc/sudoers.d/lager-bench-json",
    ),
]

# /etc/lager is www-data-owned on modern boxes, so its removal needs the same
# privileged session — but it is governed by --keep-config rather than --all,
# so it's kept out of UNINSTALL_ALL_PRIV_STEPS and prepended when applicable.
ETC_LAGER_PRIV_STEP = (
    "etc_lager",
    "Removing /etc/lager directory",
    "sudo rm -rf /etc/lager",
)

# Home-dir, not /tmp: a fixed name in the world-writable /tmp would let
# another user on the box pre-create or symlink the path and swallow (or
# poison) the per-step results. The remote shell expands the ~.
_PRIV_RESULTS_PATH = "~/.lager-uninstall-results.txt"

# The pubkey comment ssh-keygen -C sets when setup_and_deploy_box.sh generates
# ~/.ssh/lager_box; used as the authorized_keys fallback matcher when the
# local pubkey file is missing.
_LAGER_KEY_COMMENT = "lager-box-access"


def lager_key_matcher():
    """String identifying this machine's lager key in a box's authorized_keys.

    The base64 key blob from the local ~/.ssh/lager_box.pub when available
    (exact — key comments vary across old installs; the blob is [A-Za-z0-9+/=]
    so it is single-quote-safe in a shell command), else the default comment
    ssh-keygen was invoked with.
    """
    pub_path = os.path.expanduser("~/.ssh/lager_box.pub")
    if os.path.isfile(pub_path):
        try:
            with open(pub_path, "r", encoding="utf-8") as fh:
                fields = fh.read().strip().split()
            if len(fields) >= 2:
                return fields[1]
        except (OSError, UnicodeDecodeError):
            pass
    return _LAGER_KEY_COMMENT


def authorized_keys_cleanup_cmd():
    """Remote command that strips this machine's lager key from the box's
    ~/.ssh/authorized_keys (user-owned; no sudo needed).

    The `|| true` guards grep's exit-1 when every line matches (an
    authorized_keys that only held the lager key becomes empty, which is the
    correct result).
    """
    return (
        "if [ -f ~/.ssh/authorized_keys ]; then "
        f"{{ grep -vF '{lager_key_matcher()}' ~/.ssh/authorized_keys || true; }} > ~/.ssh/.lager-ak-tmp "
        "&& mv ~/.ssh/.lager-ak-tmp ~/.ssh/authorized_keys "
        "&& chmod 600 ~/.ssh/authorized_keys; fi"
    )


@click.command()
@click.pass_context
@click.option("--box", default=None, help="Box name (uses stored IP and username)")
@click.option("--ip", default=None, help="Target box IP address or DNS hostname")
@click.option("--user", default=None, help="SSH username (default: lagerdata, or stored username if using --box)")
@click.option("--keep-config", is_flag=True, help="Keep /etc/lager directory (saved nets, etc.)")
@click.option("--keep-docker-images", is_flag=True, help="Keep Docker images (only remove containers)")
@click.option("--all", "remove_all", is_flag=True, help="Remove everything including udev rules, sudoers, third_party, and deploy keys")
@click.option("--yes", is_flag=True, help="Skip confirmation prompts")
@click.option("--dry-run", is_flag=True, help="Show what would be removed without making changes")
def uninstall(ctx, box, ip, user, keep_config, keep_docker_images, remove_all, yes, dry_run):
    """
    Uninstall Lager box code from a box
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

        if user is None:
            stored_user = get_box_user(box)
            user = stored_user or "lagerdata"
    elif ip is None:
        click.secho("Error: Either --box or --ip is required", fg='red', err=True)
        ctx.exit(1)
    else:
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

        # /etc/lager directory. Presence-check without sudo (the directory is
        # world-readable); `sudo du` under BatchMode fails on boxes without a
        # NOPASSWD grant and misreported "(not found)" for a directory that
        # was very much there.
        click.secho("Config directory:", fg='cyan')
        etc_lager = query_ssh("du -sh /etc/lager 2>/dev/null || ls -d /etc/lager 2>/dev/null")
        if etc_lager:
            click.echo(f"  /etc/lager: {etc_lager.split()[0]}")
        else:
            click.echo("  /etc/lager: (not found)")

        if remove_all:
            click.echo()
            click.secho("Extended cleanup items (--all):", fg='cyan')

            # Udev rules (the shipped 99-instrument.rules, box-config user
            # rules, and legacy lager-*.rules). Trailing `; true`: a
            # multi-path ls exits non-zero when ANY path is missing, and
            # query_ssh treats non-zero as no-result — without it, one absent
            # legacy file hid the files that WERE present.
            udev = query_ssh(
                "ls /etc/udev/rules.d/99-instrument.rules "
                "/etc/udev/rules.d/99-lager-user.rules "
                "/etc/udev/rules.d/lager-*.rules 2>/dev/null; true"
            )
            click.echo(f"  Udev rules: {' '.join(udev.split()) if udev else '(none found)'}")

            # usbtmc modprobe blacklist
            modprobe = query_ssh("ls /etc/modprobe.d/blacklist-usbtmc.conf 2>/dev/null")
            click.echo(f"  usbtmc blacklist: {'present' if modprobe else '(not found)'}")

            # Sudoers files (`; true` for the same multi-path ls reason as
            # the udev query above)
            sudoers = query_ssh(
                "ls /etc/sudoers.d/lagerdata-udev /etc/sudoers.d/lager-box-config "
                "/etc/sudoers.d/lager-bench-json 2>/dev/null; true"
            )
            click.echo(f"  Sudoers files: {' '.join(sudoers.split()) if sudoers else '(none found)'}")

            # sysctl config (from `lager box-config apply`)
            sysctl_conf = query_ssh("ls /etc/sysctl.d/99-lager-box-config.conf 2>/dev/null")
            click.echo(f"  Sysctl config: {'present' if sysctl_conf else '(not found)'}")

            # Firewall helper script
            fw_script = query_ssh("ls /usr/local/lib/lager/secure_box_firewall.sh 2>/dev/null")
            click.echo(f"  Firewall helper script: {'present' if fw_script else '(not found)'}")

            # lager group (instrument device access)
            lager_group = query_ssh("getent group lager 2>/dev/null")
            click.echo(f"  'lager' group: {'present' if lager_group else '(not found)'}")

            # Third party
            third_party = query_ssh("du -sh ~/third_party 2>/dev/null")
            if third_party:
                click.echo(f"  ~/third_party: {third_party.split()[0]}")
            else:
                click.echo("  ~/third_party: (not found)")

            # This machine's key in the box's authorized_keys
            ak = query_ssh(f"grep -cF '{lager_key_matcher()}' ~/.ssh/authorized_keys 2>/dev/null")
            click.echo(f"  This machine's key in authorized_keys: {'present' if ak and ak != '0' else '(not found)'}")

            # SSH keys (both legacy and current)
            legacy_key = query_ssh("ls ~/.ssh/lager_deploy_key 2>/dev/null")
            current_key = query_ssh("ls ~/.ssh/lager_box 2>/dev/null")
            click.echo(f"  Legacy deploy key (~/.ssh/lager_deploy_key): {'present' if legacy_key else '(not found)'}")
            click.echo(f"  Box-side SSH key (~/.ssh/lager_box): {'present' if current_key else '(not found)'}")

            # UFW status (no sudo under BatchMode; status read may need root,
            # so fall back to reporting availability only)
            ufw_status = query_ssh("sudo -n ufw status 2>/dev/null | head -1")
            if ufw_status:
                click.echo(f"  UFW firewall: {ufw_status.splitlines()[0]}")
            elif query_ssh("command -v ufw 2>/dev/null"):
                click.echo("  UFW firewall: installed (status needs sudo)")
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
        click.echo("  - Docker images (ALL unused images on the box, not only lager's)")
    click.echo("  - ~/box directory")
    if not keep_config:
        click.echo("  - /etc/lager directory (saved nets)")

    if remove_all:
        click.echo("  - Instrument udev rules (99-instrument.rules, 99-lager-user.rules, lager-*.rules)")
        click.echo("  - usbtmc modprobe blacklist (/etc/modprobe.d/blacklist-usbtmc.conf)")
        click.echo("  - Lager sysctl config (/etc/sysctl.d/99-lager-box-config.conf)")
        click.echo("  - Sudoers files (lagerdata-udev, lager-box-config, lager-bench-json)")
        click.echo("  - Firewall helper script + UFW rules (reset to SSH-only)")
        click.echo("  - 'lager' group")
        click.echo("  - ~/third_party directory")
        click.echo("  - This machine's key from the box's authorized_keys")
        click.echo("  - Legacy box-side SSH keys and SSH config entries")

    click.echo()
    if not keep_config or remove_all:
        click.echo("Privileged removals run in one session; you may be prompted for the")
        click.echo("box's sudo password once if the login user has no passwordless grant.")
        click.echo()

    if not yes:
        click.secho("WARNING: This action cannot be undone!", fg='red', bold=True)
        if not click.confirm("Are you sure you want to proceed?", default=False):
            click.echo("Uninstall cancelled.")
            if ssh_pool:
                ssh_pool.close_connection(ip, user)
            ctx.exit(0)

    click.echo()

    # 5. Assemble the privileged removal steps. /etc/lager is governed by
    # --keep-config (now honored even with --all); the system artifacts by
    # --all.
    priv_results = {}
    priv_steps = []
    if not keep_config:
        priv_steps.append(ETC_LAGER_PRIV_STEP)
    if remove_all:
        priv_steps.extend(UNINSTALL_ALL_PRIV_STEPS)

    def run_priv_session(steps):
        """Run the sudo removal steps in ONE interactive `ssh -t` session.

        Each step runs in a subshell and records name=OK|FAIL to a results
        file, read back afterward over the captured channel — so sudo can
        prompt (at most once, thanks to timestamp caching) on boxes whose
        login user has no passwordless grant, and each step's outcome is
        reported honestly instead of being masked by BatchMode + `|| true`.
        """
        wrapped = [f"rm -f {_PRIV_RESULTS_PATH}"]
        for name, _desc, snippet in steps:
            wrapped.append(
                f'if ( {snippet} ); then echo "{name}=OK" >> {_PRIV_RESULTS_PATH}; '
                f'else echo "{name}=FAIL" >> {_PRIV_RESULTS_PATH}; fi'
            )
        ssh_cmd = ["ssh", "-t"]
        if ssh_pool:
            ssh_cmd.extend(ssh_pool.get_ssh_options(ip))
        # One `;`-joined command line (each element is a complete compound
        # statement), keeping the -t session's payload a single line.
        ssh_cmd.extend([ssh_host, "; ".join(wrapped)])
        try:
            # Interactive: may wait on a human typing the box's sudo
            # password. The timeout only guards a genuine hang.
            subprocess.run(ssh_cmd, timeout=600)
        except subprocess.TimeoutExpired:
            click.secho("  Privileged session timed out.", fg='yellow', err=True)
        except Exception as e:
            click.secho(f"  Privileged session failed: {e}", fg='red', err=True)
        results_raw = query_ssh(
            f"cat {_PRIV_RESULTS_PATH} 2>/dev/null; rm -f {_PRIV_RESULTS_PATH}"
        )
        results = {}
        for line in (results_raw or "").splitlines():
            if "=" in line:
                k, _, v = line.partition("=")
                results[k.strip()] = v.strip()
        for name, desc, _snippet in steps:
            click.echo(f"  {desc}...", nl=False)
            if results.get(name) == "OK":
                click.secho(" done", fg='green')
            else:
                click.secho(" FAILED", fg='red')
        return results

    # Acquire the auto-lock for the duration of the destructive steps
    # below — stopping the lager container, removing images, wiping
    # ~/box, /etc/lager, etc. all clobber a running `lager python` test.
    # A concurrent test fail-fasts (dev) or queues (CI) on the box lock
    # rather than getting killed mid-run.
    with auto_lock_around_command(ip, box or ip, 'uninstall'):
        click.secho("[Step 1/5] Stopping Docker containers...", fg='cyan')
        run_ssh(
            "cd ~/box && docker compose down 2>/dev/null",
            "Running docker compose down",
            allow_fail=True
        )
        run_ssh("docker stop lager 2>/dev/null; docker rm -f lager 2>/dev/null", "Removing lager container", allow_fail=True)
        run_ssh("docker stop pigpio 2>/dev/null; docker rm -f pigpio 2>/dev/null", "Removing pigpio container", allow_fail=True)
        run_ssh("docker network rm lagernet 2>/dev/null", "Removing lagernet network", allow_fail=True)
        click.echo()

        # Remove Docker images (unless --keep-docker-images)
        click.secho("[Step 2/5] Cleaning Docker...", fg='cyan')
        if not keep_docker_images:
            run_ssh("docker image prune -af 2>/dev/null", "Removing Docker images", allow_fail=True)
            run_ssh("docker builder prune -af 2>/dev/null", "Clearing Docker build cache", allow_fail=True)
        else:
            click.echo("  Skipping Docker image removal (--keep-docker-images)")
        click.echo()

        # Remove ~/box directory
        click.secho("[Step 3/5] Removing box code...", fg='cyan')
        run_ssh("rm -rf ~/box", "Removing ~/box directory")
        click.echo()

        # Privileged removals — /etc/lager plus (with --all) the system
        # artifacts install creates — in one interactive session.
        click.secho("[Step 4/5] Removing system configuration...", fg='cyan')
        if priv_steps:
            priv_results = run_priv_session(priv_steps)
        else:
            click.echo("  Skipping /etc/lager removal (--keep-config)")
        click.echo()

        # Unprivileged --all extras. The authorized_keys strip goes LAST of
        # all remote operations: once this machine's key is gone, further
        # BatchMode SSH to the box would need a password.
        click.secho("[Step 5/5] Cleaning up additional components...", fg='cyan')
        if remove_all:
            run_ssh("rm -rf ~/third_party", "Removing ~/third_party directory", allow_fail=True)

            # Legacy artifacts: old installs kept a deploy key (and sometimes
            # a lager_box key) on the box itself; the modern install puts no
            # private keys there.
            run_ssh(
                "rm -f ~/.ssh/lager_deploy_key ~/.ssh/lager_deploy_key.pub "
                "~/.ssh/lager_box ~/.ssh/lager_box.pub",
                "Removing legacy box-side SSH keys",
                allow_fail=True
            )
            run_ssh(
                "sed -i '/# Lager deploy key/,/IdentityFile.*lager_deploy_key/d' ~/.ssh/config 2>/dev/null; "
                "sed -i '/# Lager box key/,/IdentityFile.*lager_box/d' ~/.ssh/config 2>/dev/null",
                "Cleaning box-side SSH config",
                allow_fail=True
            )

            run_ssh(
                authorized_keys_cleanup_cmd(),
                "Removing this machine's key from authorized_keys",
                allow_fail=True
            )
        else:
            click.echo("  Skipping additional cleanup (use --all for complete removal)")

    # Clean up SSH multiplexing
    if ssh_pool:
        ssh_pool.close_connection(ip, user)

    click.echo()
    failed_steps = [desc for name, desc, _s in priv_steps if priv_results.get(name) != "OK"]
    if failed_steps:
        click.secho("Uninstall finished with FAILED steps:", fg='red', bold=True)
        for desc in failed_steps:
            click.echo(f"  - {desc}")
        click.echo()
        click.echo("Re-run the uninstall, or perform these manually on the box with sudo.")
    else:
        click.secho("Uninstall complete!", fg='green', bold=True)
    click.echo()
    click.echo(f"The lager box code has been removed from {ip}.")

    if keep_config:
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
        click.secho("Left in place by design: docker itself (packages, the buildx plugin,", fg='yellow')
        click.secho("the DNS entry in /etc/docker/daemon.json) and pip/apt packages that were", fg='yellow')
        click.secho("installed for lager workflows.", fg='yellow')
        click.echo()
        click.secho("This machine's SSH key was removed from the box's authorized_keys —", fg='yellow')
        click.secho("the next SSH connection to this box will require a password.", fg='yellow')

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
