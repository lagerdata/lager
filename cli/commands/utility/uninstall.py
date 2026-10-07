# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

"""
    lager.commands.utility.uninstall

    Uninstall Lager box code from a box
"""
import click
from click.exceptions import Abort, Exit
from datetime import datetime, timezone
import os
import subprocess
from ...address_utils import validate_ip_or_hostname, VALID_FORMATS_CHEATSHEET
from ...box_storage import (
    auto_lock_around_command,
    delete_box,
    empty_box_name_error,
    get_box_ip,
    get_box_name_by_ip,
    get_box_user,
    project_files_defining_box,
)
from ...core.ssh_utils import host_in_known_hosts, get_ssh_connection_pool
from ..box._host_ops import ETC_LAGER_PERMS_HELPER
from ..box._ssh import (
    BOX_KEYS_DIR,
    probe_box_identity,
    registered_key_name,
    ssh_identity_args,
)

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
# host pip packages (including pyOCD, which older provisions installed), and
# apt packages from `lager box-config apply`.
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
        "etc_lager_perms_script",
        "Removing /etc/lager ownership helper script",
        f"sudo rm -f {ETC_LAGER_PERMS_HELPER}",
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

# /etc/lager is kept by default. It is not lager's alone: on a box fronted by
# a control plane, the gateway's credentials, the --no-publish marker that
# keeps lager off the gateway's ports, and the SSH key registrations live in
# it too. Deleting the directory took the box off its control plane, and the
# next install then published lager's ports over the gateway's and never
# finished. So removing anything here takes --purge-config, and removing the
# control plane's files as well takes --include-control-plane on top of that.
#
# Kept by every purge: the key registrations (deleting them revokes keys that
# other operators rely on, on the next key sync) and the --no-publish marker
# (the network mode the box was deliberately put in).
PURGE_ALWAYS_KEPT = ("authorized_keys.d", "no_publish")

# Kept by a purge when control_plane.json is present, unless
# --include-control-plane. These are written by the control plane or its box
# daemon, not by lager. org_secrets.json is pushed by the control plane on such
# a box, and its org_secrets.json.pre-* copy is the only record of a file a
# person placed there by hand. Entries are find(1) -name patterns.
CONTROL_PLANE_FILES = (
    "control_plane.json",
    "dashboard",
    "telemetry_buffer.jsonl",
    "org_secrets.json",
    "org_secrets.json.pre-*",
)

CONTROL_PLANE_CONFIG = "/etc/lager/control_plane.json"
SAVED_NETS_PATH = "/etc/lager/saved_nets.json"


def _purge_find_cmd(names):
    """find(1) arguments that delete every top-level /etc/lager entry except
    ``names``."""
    excludes = " ".join(f"! -name '{n}'" for n in names)
    return f"sudo find /etc/lager -mindepth 1 -maxdepth 1 {excludes} -exec rm -rf {{}} +"


def etc_lager_purge_step(backup_dir, include_control_plane):
    """The --purge-config privileged step.

    Runs only when the backup step left its completion marker, so a failed
    backup reports this step FAILED instead of deleting the only copy of the
    saved nets. ``backup_dir`` is a remote path that the box's shell expands.

    Without --include-control-plane the control plane's files are kept when
    control_plane.json exists; the check runs on the box, at removal time.
    With it, everything goes except the key registrations: the no_publish
    marker too, since it only meant something to the gateway being removed.
    """
    guard = f"[ -f {backup_dir}/.complete ]"
    if include_control_plane:
        return (
            "etc_lager",
            "Removing /etc/lager contents, control plane files included (keeping key registrations)",
            f"{guard} && if [ -d /etc/lager ]; then "
            f"{_purge_find_cmd(('authorized_keys.d',))}; fi",
        )
    kept = _purge_find_cmd(PURGE_ALWAYS_KEPT)
    kept_cp = _purge_find_cmd(PURGE_ALWAYS_KEPT + CONTROL_PLANE_FILES)
    return (
        "etc_lager",
        "Removing /etc/lager contents (keeping key registrations and control plane files)",
        f"{guard} && if [ ! -d /etc/lager ]; then true; "
        f"elif sudo test -e {CONTROL_PLANE_CONFIG}; then {kept_cp}; "
        f"else {kept}; fi",
    )


def etc_lager_backup_step(backup_dir):
    """Copy /etc/lager into the login user's home before a purge.

    ~ survives the uninstall (only ~/box is removed). The tarball holds
    everything, secrets included, so it is mode 600; saved_nets.json is also
    copied loose because it is what people restore. The .complete marker is
    written last and gates the purge step.
    """
    return (
        "config_backup",
        f"Backing up /etc/lager to {backup_dir}",
        f"mkdir -p {backup_dir} && chmod 700 {backup_dir} && "
        f"if [ -d /etc/lager ]; then "
        f"sudo tar -C /etc -czf {backup_dir}/etc-lager.tgz lager && "
        f"sudo chown \"$(id -u):$(id -g)\" {backup_dir}/etc-lager.tgz && "
        f"chmod 600 {backup_dir}/etc-lager.tgz && "
        f"if sudo test -f {SAVED_NETS_PATH}; then "
        f"sudo cat {SAVED_NETS_PATH} > {backup_dir}/saved_nets.json; fi; fi && "
        f"touch {backup_dir}/.complete",
    )


# The default (no --purge-config) step. Lock state is not config: /etc/lager/lock.json
# records a live claim on a box whose lock server this command is deleting.
# The dissolve in Step 1 deliberately skips the release (there is nobody left
# to tell), so the file is left saying locked:true — and a lock written with
# ttl_seconds null is never reaped, because the box's _is_expired() returns
# False outright on a null TTL. The result is a tombstone: reinstall the box
# and it comes up holding a lock for a holder that no longer exists, which
# nothing on the box will ever clear.
#
# Under --purge-config the lock files go with the rest of the directory; this
# keeps the two paths consistent rather than making "keep my saved nets"
# quietly also mean "keep a dead lock".
LOCK_STATE_PRIV_STEP = (
    "lock_state",
    "Clearing box lock state",
    "sudo rm -f /etc/lager/lock.json /etc/lager/lock.json.flock",
)

# start_box.sh runs a background poller that rebuilds lager's block in
# ~/.ssh/authorized_keys from /etc/lager/authorized_keys.d every 5 s. It is
# disowned, so it outlives start_box.sh, the container and ~/box, and runs from
# a deleted script until the box reboots. Left running, it resumes the moment
# the key directory reappears: if /etc/lager is recreated holding only the
# reinstalling machine's key, it revokes every other managed key within 5 s.
#
# The poller runs under the argv[0] below (start_box.sh, `_SSH_SYNC_MARKER`).
# Pollers started before that marker existed are plain subshells of
# start_box.sh: bash running the script file. The pattern matches only that
# shape, never a process that merely names the script -- such as an ssh client
# whose remote command runs it, which is visible here when this command runs on
# the box itself (start_box.sh, `_SSH_SYNC_LEGACY_PATTERN`). This command's own
# remote shell is `bash -c ...`, which the shape excludes, and the bracket in
# `[s]tart_box` keeps the pattern text from matching itself anywhere else.
# Nothing else of lager's is running by now: the box lock is held, and the
# lager container is stopped before this runs.
#
# pkill exits 1 when it matched nothing, which is the normal case on a box
# with no leftover poller, so the step reports success either way.
SSH_SYNC_MARKER = "lager-ssh-sync"
SSH_SYNC_LEGACY_PATTERN = "^([^ ]*/)?bash [^ -][^ ]*[s]tart_box[.]sh( |$)"
SSH_SYNC_STOP_CMD = (
    f"pkill -u \"$(id -u)\" -f '^{SSH_SYNC_MARKER}( |$)'; "
    f"pkill -u \"$(id -u)\" -f '{SSH_SYNC_LEGACY_PATTERN}'; "
    "exit 0"
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


# start_box.sh's sentinels. Only lines between them were put there by lager.
_AK_BEGIN = "# BEGIN LAGER MANAGED KEYS (managed by start_box.sh — do not edit by hand)"
_AK_END = "# END LAGER MANAGED KEYS"


def authorized_keys_cleanup_cmd():
    """Remote command that revokes this machine's lager key on the box
    (user-owned file; no sudo needed). Prints one status word.

    Only the copy inside lager's managed block is removed. A loose copy
    elsewhere in the file was put there by something else (ssh-copy-id, a
    person, another key manager), and on a fleet that shares one lager_box
    key, stripping it revoked the key for every operator, not just this one.
    The key is also left alone while any registration in the key directory
    still holds it: the next key sync would only publish it again, and the
    other registrant still relies on it.

    Matches only on the exact key blob. With no local pubkey there is nothing
    exact to match, and the comment is shared by every lager_box key, so the
    command does nothing and says so.

    Status words: ``revoked``, ``still-registered``, ``not-found``,
    ``no-local-key``.
    """
    blob = lager_key_matcher()
    if blob == _LAGER_KEY_COMMENT:
        return "echo no-local-key"
    return (
        "ak=~/.ssh/authorized_keys; "
        f"d={BOX_KEYS_DIR}; "
        "if ls \"$d\"/*.pub >/dev/null 2>&1 "
        f"&& grep -qF '{blob}' \"$d\"/*.pub 2>/dev/null; then echo still-registered; "
        "elif [ ! -f \"$ak\" ]; then echo not-found; "
        f"elif ! awk -v b='{_AK_BEGIN}' -v e='{_AK_END}' -v k='{blob}' "
        "'$0 == b { inb = 1 } $0 == e { inb = 0 } inb && index($0, k) { found = 1 } "
        "END { exit !found }' \"$ak\"; then echo not-found; "
        f"else awk -v b='{_AK_BEGIN}' -v e='{_AK_END}' -v k='{blob}' "
        "'$0 == b { inb = 1; print; next } $0 == e { inb = 0; print; next } "
        "inb && index($0, k) { next } { print }' \"$ak\" > ~/.ssh/.lager-ak-tmp "
        "&& chmod 600 ~/.ssh/.lager-ak-tmp "
        "&& mv -f ~/.ssh/.lager-ak-tmp \"$ak\" && echo revoked; fi"
    )


def loose_key_count_cmd():
    """Remote command counting copies of this machine's key OUTSIDE lager's
    managed block. --all leaves those in place and says so."""
    blob = lager_key_matcher()
    if blob == _LAGER_KEY_COMMENT:
        return "echo 0"
    return (
        f"awk -v b='{_AK_BEGIN}' -v e='{_AK_END}' -v k='{blob}' "
        "'$0 == b { inb = 1; next } $0 == e { inb = 0; next } "
        "!inb && index($0, k) { n++ } END { print n + 0 }' "
        "~/.ssh/authorized_keys 2>/dev/null || echo 0"
    )


# One round trip, key=value lines. No sudo: /etc/lager and saved_nets.json are
# world-readable, and control_plane.json's existence (not its content) is all
# that is checked.
CONFIG_STATE_QUERY = (
    "echo \"etc=$([ -d /etc/lager ] && echo 1 || echo 0)\"; "
    f"echo \"cp=$([ -e {CONTROL_PLANE_CONFIG} ] && echo 1 || echo 0)\"; "
    "echo \"others=$(docker ps --filter network=lagernet --format '{{.Names}}' 2>/dev/null "
    "| grep -vx -e lager -e pigpio | paste -sd, -)\"; "
    f"echo \"nets=$(if [ -f {SAVED_NETS_PATH} ]; then "
    "python3 -c 'import json,sys; print(len(json.load(open(sys.argv[1]))))' "
    f"{SAVED_NETS_PATH} 2>/dev/null || echo '?'; else echo 0; fi)\""
)


def inspect_config_state(query):
    """Parse CONFIG_STATE_QUERY's output. ``query`` runs a remote command and
    returns its stdout or None.

    Returns None when the query failed, else a dict with:
    ``etc_lager`` (bool), ``control_plane`` (control_plane.json present),
    ``other_containers`` (names of running containers on lagernet that are
    not lager's, such as a control plane's gateway), and ``nets`` (int, or
    None when the file could not be parsed).
    """
    raw = query(CONFIG_STATE_QUERY)
    if raw is None:
        return None
    fields = {}
    for line in raw.splitlines():
        k, sep, v = line.partition("=")
        if sep:
            fields[k.strip()] = v.strip()
    if "etc" not in fields:
        return None
    nets = fields.get("nets", "?")
    return {
        "etc_lager": fields.get("etc") == "1",
        "control_plane": fields.get("cp") == "1",
        "other_containers": [n for n in fields.get("others", "").split(",") if n],
        "nets": int(nets) if nets.isdigit() else None,
    }


@click.command()
@click.pass_context
@click.option("--box", default=None, help="Box name (uses stored IP and username)")
@click.option("--ip", default=None, help="Target box IP address or DNS hostname")
@click.option("--user", default=None, help="SSH username (default: lagerdata, or stored username if using --box)")
@click.option("--purge-config", is_flag=True,
              help="Also delete /etc/lager (saved nets, box config). Backs it up first. "
                   "Keeps key registrations, and a control plane's files.")
@click.option("--include-control-plane", is_flag=True,
              help="With --purge-config, also delete the control plane's files. The box "
                   "drops off its control plane until it is re-linked.")
@click.option("--keep-config", is_flag=True,
              help="No effect: keeping /etc/lager is now the default. Accepted so existing scripts still run.")
@click.option("--keep-docker-images", is_flag=True, help="Keep Docker images (only remove containers)")
@click.option("--all", "remove_all", is_flag=True,
              help="Also remove udev rules, sudoers, third_party, and this machine's key. "
                   "Does not imply --purge-config.")
@click.option("--yes", is_flag=True, help="Skip confirmation prompts")
@click.option("--dry-run", is_flag=True, help="List what the command removes. Make no changes.")
def uninstall(ctx, box, ip, user, purge_config, include_control_plane, keep_config,
              keep_docker_images, remove_all, yes, dry_run):
    """
    Uninstall Lager box code from a box.

    Keeps /etc/lager (saved nets, box config, a control plane's files) unless
    --purge-config is given.
    """
    # 0. Flag combinations
    if keep_config and purge_config:
        click.secho("Error: --keep-config and --purge-config contradict each other.",
                    fg='red', err=True)
        click.echo("Keeping /etc/lager is the default; drop --keep-config.", err=True)
        ctx.exit(2)
    if include_control_plane and not purge_config:
        click.secho("Error: --include-control-plane only applies together with --purge-config.",
                    fg='red', err=True)
        ctx.exit(2)
    # Remote path; the box's shell expands the ~. Named here so the dry run,
    # the confirmation and the purge all quote the same directory.
    backup_dir = f"~/lager-backup-{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}"

    # 1. Resolve box name to IP and username if --box is provided
    if box is not None and not box.strip():
        raise empty_box_name_error()
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

    # 3. Check SSH connectivity (with password fallback), and settle which
    #    identity the rest of this command offers the box.
    #
    #    ~/.ssh/lager_box is not one of ssh's default identity filenames, so
    #    without an explicit -i it is never tried — and on a box where it is
    #    the only authorized key, every bare `ssh` below fails. Probing it
    #    here answers the question once for the whole teardown;
    #    probe_box_identity falls back to ssh's own identities when the box
    #    rejects the key, so nothing that worked before stops working.
    click.echo(f"Checking SSH connectivity to {ssh_host}...")
    use_interactive_ssh = False
    use_multiplexing = False
    identity = None
    try:
        identity, result = probe_box_identity(ssh_host)
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
                click.echo("The box answers, but no SSH service runs on port 22.", err=True)
                click.echo(err=True)
                click.echo("Possible causes:", err=True)
                click.echo("  - SSH server is not installed or running", err=True)
                click.echo("  - SSH runs on a non-standard port", err=True)
                click.echo("  - A firewall blocks port 22", err=True)
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
                    click.echo("The SSH host key changed. This means one of:", err=True)
                    click.echo("  - The box was reinstalled or reimaged", err=True)
                    click.echo("  - A different device uses this IP address", err=True)
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
                        # Re-probe rather than reuse the first answer: that
                        # attempt never got past the host key, so it never
                        # learned which identity the box accepts.
                        identity, accept_result = probe_box_identity(
                            ssh_host,
                            extra_args=("-o", "StrictHostKeyChecking=accept-new"),
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
                click.secho("Error: The hostname did not resolve", fg='red', err=True)
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
        click.echo(f"The box at {ssh_host} did not answer within 15 seconds.", err=True)
        click.echo(err=True)
        click.echo("Possible causes:", err=True)
        click.echo("  - Box is offline or powered down", err=True)
        click.echo("  - Network connectivity issue", err=True)
        click.echo("  - A firewall drops packets (it does not reject them)", err=True)
        click.echo(err=True)
        click.echo("Verify the box is online and try: ping " + ip, err=True)
        ctx.exit(1)
    except FileNotFoundError:
        click.secho("Error: SSH command not found", fg='red', err=True)
        click.secho("Please install OpenSSH client:", err=True)
        import platform
        if platform.system() == "Darwin":
            click.secho("  macOS: SSH is pre-installed by default. Check your PATH.", err=True)
        elif platform.system() == "Windows":
            click.secho("  Windows: Install OpenSSH via Settings > Apps > Optional Features", err=True)
        else:
            click.secho("  Linux: sudo apt install openssh-client (Debian/Ubuntu)", err=True)
            click.secho("         sudo dnf install openssh-clients (Fedora/RHEL)", err=True)
        ctx.exit(1)
    except (Exit, Abort):
        raise
    except Exception as e:
        click.secho(f"Error: {e}", fg='red', err=True)
        ctx.exit(1)

    click.echo()

    # Set up SSH connection multiplexing for key-based auth. The master gets
    # the identity settled above, and the pool re-offers it on any command
    # that has to open its own connection because the socket went away.
    ssh_pool = None
    if use_multiplexing:
        ssh_pool = get_ssh_connection_pool()
        if not ssh_pool.ensure_connection(ip, user, identity_file=identity):
            ssh_pool = None

    # Identity for the non-multiplexed calls. When the pool is live its
    # options already carry the -i, so adding it here too would offer the
    # same key twice and burn an authentication attempt against sshd's
    # MaxAuthTries.
    def identity_args():
        return [] if ssh_pool else ssh_identity_args(identity)

    # Helper function to run SSH commands
    def run_ssh(cmd, description, allow_fail=False):
        """Run an SSH command and handle errors.

        Returns True only when the remote command actually succeeded.
        ``allow_fail`` softens how the failure is REPORTED (yellow
        "skipped" instead of red "failed"), not what is returned — the
        lock-dissolve decision below needs the honest answer.
        """
        click.echo(f"  {description}...", nl=False)
        try:
            ssh_cmd = ["ssh"]
            ssh_cmd.extend(identity_args())
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
                return False
            else:
                click.secho(" failed", fg='red')
                if not use_interactive_ssh and hasattr(result, 'stderr') and result.stderr:
                    stderr_text = result.stderr.strip()
                    click.secho(f"    Error: {stderr_text}", fg='red', err=True)
                    if "Permission denied" in stderr_text:
                        click.secho("    Hint: This step can need sudo permissions", err=True)
                    elif "No such file" in stderr_text:
                        click.secho("    Hint: File or directory does not exist", err=True)
                elif not use_interactive_ssh and hasattr(result, 'stdout') and result.stdout:
                    stdout_text = result.stdout.strip()
                    if stdout_text:
                        click.secho(f"    Output: {stdout_text}", fg='yellow', err=True)
                return False
        except subprocess.TimeoutExpired:
            click.secho(" timeout", fg='yellow')
            click.secho("    Command timed out. The box can be slow or unresponsive.", err=True)
            return False
        except Exception as e:
            click.secho(f" error: {e}", fg='red')
            return False

    # Helper to run SSH query commands (for --dry-run and reading the
    # privileged session's results file)
    def query_ssh(cmd):
        """Run an SSH command and return stdout, or None on failure.

        Two things differ on the password path (``use_interactive_ssh``),
        both because a human is in the loop:

        * stderr is left attached to the terminal instead of captured.
          ssh writes its password prompt to /dev/tty, but its diagnostics go
          to stderr, and swallowing those while the user is being asked to
          type something is how "nothing happened for thirty seconds" gets
          produced.
        * the timeout matches ``run_ssh``'s interactive branch (120s, not
          30s). 30s is shorter than a person finding and typing a password,
          so the call was reliably killed mid-prompt.

        A timeout is reported rather than swallowed. It used to land in the
        blanket ``except Exception`` and return None, which ``--dry-run``
        renders identically to "the box does not have this" -- so a stalled
        prompt printed a clean, confident, entirely wrong inventory.
        """
        try:
            ssh_cmd = ["ssh"]
            ssh_cmd.extend(identity_args())
            if ssh_pool:
                ssh_cmd.extend(ssh_pool.get_ssh_options(ip))
            if use_interactive_ssh:
                # One prompt, like the connectivity check: ssh's default of
                # three turns a wrong password into three stalled minutes.
                ssh_cmd.extend(["-o", "NumberOfPasswordPrompts=1"])
            else:
                ssh_cmd.extend(["-o", "BatchMode=yes"])
            ssh_cmd.extend([ssh_host, cmd])

            result = subprocess.run(
                ssh_cmd,
                stdout=subprocess.PIPE,
                stderr=None if use_interactive_ssh else subprocess.PIPE,
                text=True,
                timeout=120 if use_interactive_ssh else 30,
            )
            if result.returncode == 0:
                return result.stdout.strip()
            return None
        except subprocess.TimeoutExpired:
            click.secho(
                f"  (query timed out after {120 if use_interactive_ssh else 30}s; "
                "result unknown, not necessarily absent)",
                fg='yellow', err=True,
            )
            return None
        except Exception:
            return None

    # What the confirmation, the dry run and the purge all need to know about
    # /etc/lager, in one round trip. None when the query itself failed.
    config_state = inspect_config_state(query_ssh)

    def describe_config_plan():
        """Lines saying what happens to /etc/lager under the chosen flags."""
        if not purge_config:
            return [
                "/etc/lager is kept (saved nets, box config, key registrations"
                + (", control plane files)" if config_state and config_state["control_plane"] else ")"),
                "  only its stale lock state (lock.json) is cleared",
            ]
        lines = [f"/etc/lager is purged after a backup to {backup_dir}"]
        if include_control_plane:
            lines.append("  kept: key registrations (authorized_keys.d) only")
            lines.append("  DELETED: control plane files and the no_publish marker")
        else:
            lines.append("  kept: key registrations (authorized_keys.d), the no_publish marker")
            if config_state and config_state["control_plane"]:
                lines.append("  kept: control plane files (" + ", ".join(CONTROL_PLANE_FILES) + ")")
        nets = config_state["nets"] if config_state else None
        if nets is None:
            lines.append("  saved nets: count unknown")
        else:
            lines.append(f"  saved nets that will be deleted: {nets}")
        return lines

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
        for name in (config_state or {}).get("other_containers", []):
            click.echo(f"  {name}: left running (not lager's; uninstall never touches it)")

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
            if config_state is None:
                click.echo("  (its contents were not readable; the plan below assumes defaults)")
            elif config_state["control_plane"]:
                click.echo("  Managed by a control plane: control_plane.json present")
            for line in describe_config_plan():
                click.echo(f"  {line}")
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

            # /etc/lager ownership helper (installed by `lager install`)
            perms_script = query_ssh(f"ls {ETC_LAGER_PERMS_HELPER} 2>/dev/null")
            click.echo(f"  /etc/lager helper script: {'present' if perms_script else '(not found)'}")

            # lager group (instrument device access)
            lager_group = query_ssh("getent group lager 2>/dev/null")
            click.echo(f"  'lager' group: {'present' if lager_group else '(not found)'}")

            # Third party
            third_party = query_ssh("du -sh ~/third_party 2>/dev/null")
            if third_party:
                click.echo(f"  ~/third_party: {third_party.split()[0]}")
            else:
                click.echo("  ~/third_party: (not found)")

            # This machine's key in the box's authorized_keys. Only the copy in
            # lager's managed block is revoked; loose copies stay.
            if lager_key_matcher() == _LAGER_KEY_COMMENT:
                click.echo("  This machine's key in authorized_keys: (no local ~/.ssh/lager_box.pub; left alone)")
            else:
                ak = query_ssh(
                    f"grep -cF '{lager_key_matcher()}' ~/.ssh/authorized_keys 2>/dev/null"
                )
                click.echo(f"  This machine's key in authorized_keys: {'present' if ak and ak != '0' else '(not found)'}")
                loose = query_ssh(loose_key_count_cmd())
                if loose and loose != "0":
                    click.echo(f"    {loose} copy(ies) outside lager's managed block: left in place")

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
        click.echo("  - Docker images (the lager image and dangling layers only)")
    click.echo("  - ~/box directory")
    for i, line in enumerate(describe_config_plan()):
        click.echo(f"  - {line}" if i == 0 else f"    {line.strip()}")

    if remove_all:
        click.echo("  - Instrument udev rules (99-instrument.rules, 99-lager-user.rules, lager-*.rules)")
        click.echo("  - usbtmc modprobe blacklist (/etc/modprobe.d/blacklist-usbtmc.conf)")
        click.echo("  - Lager sysctl config (/etc/sysctl.d/99-lager-box-config.conf)")
        click.echo("  - Sudoers files (lagerdata-udev, lager-box-config, lager-bench-json)")
        click.echo("  - Firewall helper script + UFW rules (reset to SSH-only)")
        click.echo("  - 'lager' group")
        click.echo("  - ~/third_party directory")
        click.echo("  - This machine's key registration, and its line in lager's managed")
        click.echo("    block of authorized_keys (copies placed by anything else stay)")
        click.echo("  - Legacy box-side SSH keys and SSH config entries")

    others = (config_state or {}).get("other_containers", [])
    if others:
        click.echo()
        click.echo(f"Left running (not lager's): {', '.join(others)}")

    if purge_config and include_control_plane and config_state and config_state["control_plane"]:
        click.echo()
        click.secho("WARNING: --include-control-plane deletes control_plane.json.", fg='red', bold=True)
        click.secho("The box drops off its control plane: a gateway in front of lager refuses", fg='red')
        click.secho("every connection until the box is re-linked from the control plane.", fg='red')

    if purge_config and config_state and config_state["nets"]:
        click.echo()
        click.secho(f"{config_state['nets']} saved net(s) will be deleted. They are backed up first to",
                    fg='yellow')
        click.secho(f"{backup_dir}/ on the box.", fg='yellow')

    click.echo()
    # Always at least one privileged step: the lock state, or (with
    # --purge-config) the backup and the purge.
    click.echo("Privileged removals run in one session. If the login user has no")
    click.echo("passwordless grant, the box asks for its sudo password once.")
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
    # --purge-config (and --include-control-plane); the system artifacts by --all,
    # which implies neither.
    priv_results = {}
    priv_steps = []
    if purge_config:
        priv_steps.append(etc_lager_backup_step(backup_dir))
        priv_steps.append(etc_lager_purge_step(backup_dir, include_control_plane))
    else:
        priv_steps.append(LOCK_STATE_PRIV_STEP)
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
        ssh_cmd.extend(identity_args())
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
    with auto_lock_around_command(ip, box or ip, 'uninstall') as lock_session:
        click.secho("[Step 1/5] Stopping Docker containers...", fg='cyan')
        run_ssh(
            "cd ~/box && docker compose down 2>/dev/null",
            "Running docker compose down",
            allow_fail=True
        )
        lager_container_removed = run_ssh("docker stop lager 2>/dev/null; docker rm -f lager 2>/dev/null", "Removing lager container", allow_fail=True)
        if lager_container_removed:
            # That container served the :9000 lock API our own lock lives
            # in, so the lock is gone with it. Dissolve the session rather
            # than heartbeat and release against a server this command just
            # deleted — those POSTs cannot succeed, and the heartbeat's
            # "relying on server TTL" warning fired on every single
            # successful uninstall because of it.
            #
            # Only on a CONFIRMED removal: if the step failed, the container
            # may still be up, and a heartbeat failure is real signal again.
            lock_session.dissolve()
        run_ssh("docker stop pigpio 2>/dev/null; docker rm -f pigpio 2>/dev/null", "Removing pigpio container", allow_fail=True)
        run_ssh("docker network rm lagernet 2>/dev/null", "Removing lagernet network", allow_fail=True)
        # Nothing else stops it: it outlives the containers and ~/box.
        run_ssh(SSH_SYNC_STOP_CMD, "Stopping the SSH key-sync poller", allow_fail=True)
        click.echo()

        # Remove Docker images (unless --keep-docker-images). Scoped to the
        # lager image plus dangling layers: 'prune -af' would also delete
        # images belonging to any OTHER stopped container on the box (a
        # management agent, a user's own services) — infrastructure this
        # command cannot restore.
        click.secho("[Step 2/5] Cleaning Docker...", fg='cyan')
        if not keep_docker_images:
            run_ssh("docker rmi -f lager 2>/dev/null", "Removing lager image", allow_fail=True)
            run_ssh("docker image prune -f 2>/dev/null", "Removing dangling images", allow_fail=True)
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
        if not purge_config:
            click.echo("  Keeping /etc/lager (use --purge-config to delete it)")
        priv_results = run_priv_session(priv_steps)
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

            # De-register before revoking the authorized_keys line: the
            # registration is the durable half. Every purge keeps the key
            # directory, so a .pub left behind would let the next
            # start_box.sh sync re-publish the key this step just removed.
            # sudo -n fallback for the same reason registration needs one: a
            # hardened box's key directory is root-owned, so the login user
            # cannot unlink from it unaided.
            _reg_path = f"{BOX_KEYS_DIR}/{registered_key_name()}"
            run_ssh(
                f"rm -f {_reg_path} 2>/dev/null || sudo -n rm -f {_reg_path}",
                "De-registering this machine's key",
                allow_fail=True
            )
            click.echo("  Revoking this machine's key in lager's managed block...", nl=False)
            ak_status = query_ssh(authorized_keys_cleanup_cmd())
            ak_messages = {
                "revoked": (" done", 'green'),
                "not-found": (" not in lager's block (nothing to revoke)", 'yellow'),
                "still-registered": (" kept: another registration still holds this key", 'yellow'),
                "no-local-key": (" skipped: no local ~/.ssh/lager_box.pub to match", 'yellow'),
            }
            msg, color = ak_messages.get(ak_status or "", (" FAILED", 'red'))
            click.secho(msg, fg=color)
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
    click.echo(f"The CLI removed the Lager Box software from {ip}.")

    if not purge_config:
        click.echo()
        click.secho("Note: /etc/lager was kept (saved nets, box config, key registrations).", fg='yellow')
        click.secho("Its lock.json was cleared — the lock server it described is gone.", fg='yellow')
    elif priv_results.get("config_backup") == "OK":
        click.echo()
        click.secho(f"/etc/lager was backed up to {backup_dir}/ on the box:", fg='yellow')
        click.echo("  etc-lager.tgz      everything, mode 600 (holds secrets)")
        click.echo("  saved_nets.json    the saved nets")
        click.echo("To restore the nets after reinstalling, on the box run:")
        click.secho(f"  sudo install -o 33 -g 33 -m 644 {backup_dir}/saved_nets.json {SAVED_NETS_PATH}",
                    fg='cyan')
    else:
        click.echo()
        click.secho("The backup did not complete, so /etc/lager was NOT purged.", fg='red')

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
        # Only lager_box is removed, so "you will need a password now" is a
        # claim about every credential made from a fact about one. Any key the
        # operator installed themselves, and any key another manager renders
        # into this file, is untouched and still works — saying otherwise sends
        # someone hunting for a box password they do not need.
        click.secho("This machine's lager_box key was de-registered and removed from lager's", fg='yellow')
        click.secho("managed block of authorized_keys. Copies placed by anything else, and", fg='yellow')
        click.secho("every other key, still work.", fg='yellow')

    # 10. Local config cleanup - offer to remove box from .lager config
    box_name = box
    if not box_name:
        box_name = get_box_name_by_ip(ip)

    if box_name:
        click.echo()
        if yes or click.confirm(f"Remove '{box_name}' from local .lager configuration?", default=True):
            deleted = delete_box(box_name)
            # delete_box writes the global ~/.lager only, but every read
            # merges the global file with each project .lager found walking up
            # from the cwd. A name defined in both is deleted AND still
            # resolves — so report on what survived, not on what we wrote.
            # Saying "Removed" about a box that still answers to its name sent
            # people looking for a bug in the box rather than in their config.
            survivors = project_files_defining_box(box_name)
            if deleted:
                click.secho(f"Removed '{box_name}' from the global ~/.lager config.", fg='green')
            elif not survivors:
                click.secho(f"'{box_name}' was not found in .lager config.", fg='yellow')
            if survivors:
                click.echo()
                click.secho(
                    f"Note: '{box_name}' is still defined in "
                    + ", ".join(str(p) for p in survivors),
                    fg='yellow',
                )
                click.secho(
                    "Those are project-level config files and were left alone; "
                    "edit them to retire the name entirely.",
                    fg='yellow',
                )
        else:
            click.echo(f"Kept '{box_name}' in .lager config.")
