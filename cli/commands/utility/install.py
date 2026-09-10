# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

"""
    lager.commands.utility.install

    Install lager box code onto a new box
"""
import click
from click.exceptions import Abort, Exit
import re
import shlex
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
    default_install_timeout_seconds,
    install_lock_ttl_seconds,
)
from ...core.ssh_utils import host_in_known_hosts
from ...errors import ssh_error, LagerError
from ..box._host_ops import (
    BOXCFG_SUDOERS_MARKER,
    boxcfg_sudoers_bootstrap_cmd,
    is_valid_unix_username,
)
from ..box._ssh import (
    _LAGER_BOX_KEY,
    lager_box_key_if_present,
    probe_box_identity,
    ssh_identity_args,
)
from ..box.ssh_setup import provision_lager_box_key
# Reuse update's single answer to "does this version have a published image?"
# rather than re-deriving it. That agreement is load-bearing: the same test
# lives in setup_and_deploy_box.sh, and a third copy here is a third thing to
# drift.
from .update import _box_image_ref_for_version
# The same state-file writer and box readers `lager update` uses, so the two
# commands record a deploy identically.
from .update import (
    _read_box_head_sha,
    _read_box_source_version,
    _read_build_hash,
    _state_file_write_cmd,
    resolve_version_ref,
)


def no_usable_identity_error(ssh_host, ip):
    """The box accepted no SSH key this machine can offer.

    Reached only when the operator declines to set the key up now — install
    offers to do that itself, so this is the "no thanks" exit rather than a
    dead end.

    Deliberately *not* "password authentication failed". Install used to
    offer a password fallback here, but a hardened box sets
    `PasswordAuthentication no`, so ssh never sent the password it had just
    asked the operator for — and reported a password problem for what is an
    identity problem. That sent people hunting for a wrong password when
    the box had simply never authorized this machine's key.
    """
    return LagerError(
        f'{ssh_host} did not accept any SSH key this machine can offer.',
        cause=(
            'Key authentication failed for every identity ssh tried, including '
            f'{_LAGER_BOX_KEY} when it exists.'
        ),
        fixes=[
            f'Authorize this machine once, then re-run install: lager ssh-setup --box {ip}',
            f'Or copy a key yourself: ssh-copy-id {ssh_host}',
            'If the box also refuses passwords (PasswordAuthentication no), a key has '
            'to be installed on it out of band before either will work.',
        ],
    )


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


def _format_duration(seconds):
    """Render a timeout budget the way an operator would say it.

    Whole minutes read as minutes; anything else keeps the seconds so the
    message never rounds a 1750s budget to "29 minutes" and invites someone
    to go looking for a 30-minute literal that is no longer there.
    """
    if seconds % 60 == 0:
        minutes = seconds // 60
        return f"{minutes} minute{'s' if minutes != 1 else ''}"
    return f"{seconds} seconds"


# A release tag's own version number. Deliberately the same strict X.Y.Z shape
# `lager update` uses for its final version write, so a pre-release tag reads
# its number from the tree, exactly as update does.
_RELEASE_NUMBER_RE = re.compile(r'^v?(\d+\.\d+\.\d+)$')


def _install_state_runner(ssh_host, identity_args):
    """Non-interactive ssh runner for the post-deploy state writes.

    BatchMode and no `-t`: nothing here can prompt, so nothing here can sit
    waiting on an operator who stepped away during a long deploy -- which is
    how the old sudo-over-`ssh -t` writes came to fail unseen.
    """
    def run(cmd, timeout_secs=30):
        return subprocess.run(
            ["ssh", *identity_args, "-o", "BatchMode=yes", ssh_host, cmd],
            capture_output=True, text=True, timeout=timeout_secs,
        )
    return run


def _installed_box_version(runner, deployed_ref):
    """The version number the installed tree declares, or ''.

    The same rule as `lager update`'s final version write: a release tag's own
    number, else `__version__` from the installed tree's cli/__init__.py. Never
    the version of the CLI that ran the install -- a box that records the
    CLI's number instead of its own reports a release it is not running.
    """
    m = _RELEASE_NUMBER_RE.match(deployed_ref)
    if m:
        return m.group(1)
    return _read_box_source_version(runner)


def _last_line(text):
    """Last non-blank line of ``text``, stripped, or ''.

    ssh can put banner or known-hosts noise ahead of the line that matters.
    """
    lines = [line.strip() for line in (text or '').splitlines() if line.strip()]
    return lines[-1] if lines else ''


def _state_file_fixes(ssh_host, name, content):
    return [
        f'Log in to the box: ssh {ssh_host}',
        f"Then write the file by hand: printf '%s\\n' {shlex.quote(content)} "
        f"| sudo tee /etc/lager/{name}",
        f'Confirm the result: lager hello --box {ssh_host.split("@", 1)[-1]}',
    ]


def _record_install_state(runner, ssh_host, *, version, cli_version):
    """Record what was installed in /etc/lager, and prove the version landed.

    Writes /etc/lager/version, /etc/lager/ref and /etc/lager/build-hash with
    `lager update`'s own writer and readers, checks every exit code, and reads
    the version file back. Raises LagerError naming the file on any failure.

    The version file is what `lager hello` and `lager boxes` report. install
    used to write it through sudo over `ssh -t`, ignore the result, and print
    success regardless, so a failed write left an older release's number on a
    box that had just been installed from a newer one.

    Returns ``(box_version, ref_content, build_hash)``; ``build_hash`` is ''
    when the box reported none, in which case that file is not written.
    """
    deployed_ref, _reset, _fetch = resolve_version_ref(version)
    box_version = _installed_box_version(runner, deployed_ref)
    if not box_version:
        raise LagerError(
            'The CLI did not find the version of the code it installed.',
            cause=(
                f'{deployed_ref} is not a release tag, and cli/__init__.py in '
                'the box checkout declares no __version__.'
            ),
            fixes=[
                f"Inspect the checkout on the box: ssh {ssh_host} 'git -C ~/box log -1'",
                'Then run the install again.',
            ],
        )

    version_content = f'{box_version}|{cli_version}'
    result = runner(_state_file_write_cmd('version', version_content))
    if result.returncode != 0:
        raise LagerError(
            'The CLI did not write /etc/lager/version on the box.',
            cause=_last_line(result.stderr) or f'ssh exited with status {result.returncode}.',
            fixes=_state_file_fixes(ssh_host, 'version', version_content),
        )
    readback = runner('cat /etc/lager/version')
    found = _last_line(readback.stdout) if readback.returncode == 0 else ''
    if found != version_content:
        raise LagerError(
            'The version file on the box does not hold the version the CLI wrote.',
            cause=f'The CLI wrote {version_content!r} and read back {found!r}.',
            fixes=_state_file_fixes(ssh_host, 'version', version_content),
        )

    head_sha = _read_box_head_sha(runner)
    ref_content = f'{deployed_ref}@{head_sha}' if head_sha else deployed_ref
    build_hash = _read_build_hash(runner)
    writes = [('ref', ref_content)]
    if build_hash:
        writes.append(('build-hash', build_hash))
    for name, content in writes:
        result = runner(_state_file_write_cmd(name, content))
        if result.returncode != 0:
            raise LagerError(
                f'The CLI did not write /etc/lager/{name} on the box.',
                cause=_last_line(result.stderr) or f'ssh exited with status {result.returncode}.',
                fixes=_state_file_fixes(ssh_host, name, content),
            )
    return box_version, ref_content, build_hash


@click.command()
@click.pass_context
@click.option("--box", default=None, help="Box name (uses stored IP and username)")
@click.option("--ip", default=None, help="Target box IP address or DNS hostname")
@click.option("--user", default=None, help="SSH username (default: lagerdata, or stored username if using --box)")
@click.option("--version", "version", default="main", help="Version to deploy: a release tag (e.g. v0.21.3), a branch (main, staging), or a full 40-character commit SHA (default: main)")
@click.option("--skip-jlink", is_flag=True, help="Skip J-Link installation")
@click.option("--skip-firewall", is_flag=True, help="Skip UFW firewall configuration")
@click.option("--skip-verify", is_flag=True, help="Skip post-deployment verification")
@click.option("--corporate-vpn", default=None, help="Corporate VPN interface name (e.g., tun0)")
@click.option("--yes", is_flag=True, help="Skip confirmation prompts")
@click.option("--pull", is_flag=True, help="Use the pre-built box image for a release tag instead of building it on the box (the default; falls back to a local build on any miss)")
@click.option("--no-pull", "no_pull", is_flag=True, help="Never use a pre-built image; always build on the box")
@click.option("--timeout", type=click.IntRange(min=0), default=None, help="Max seconds for the deploy step, which includes the container build (0=no timeout). Defaults to LAGER_INSTALL_TIMEOUT, or 1800. Raise it on slow hardware -- an emulated guest or a throttled VM can exceed the default on a healthy build.")
def install(ctx, box, ip, user, version, skip_jlink, skip_firewall, skip_verify, corporate_vpn, yes, pull, no_pull, timeout):
    """
    Install lager box code onto a new box
    """
    if pull and no_pull:
        raise click.UsageError('--pull and --no-pull are mutually exclusive')

    # Flag wins over the env var, which wins over the default. `--timeout 0`
    # is a real value (no timeout), so test for None rather than falsiness.
    deploy_timeout = timeout if timeout is not None else default_install_timeout_seconds()

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

    # 4. Check SSH connectivity, and settle which identity the rest of this
    #    command offers the box.
    #
    #    ~/.ssh/lager_box is not one of ssh's default identity filenames, so
    #    without an explicit -i it is never tried — and on a box where it is
    #    the only authorized key, every bare `ssh` below fails. probe_box_identity
    #    offers it first and falls back to ssh's defaults when the box rejects
    #    it, so a box authorized by the operator's own key, or a fresh box that
    #    has never had `lager ssh-setup` run, is unaffected.
    click.echo(f"Checking SSH connectivity to {ssh_host}...")
    identity = None
    try:
        identity, result = probe_box_identity(ssh_host)
        if result.returncode != 0:
            stderr = result.stderr.lower() if result.stderr else ""

            # Check for specific SSH error types
            if "permission denied" in stderr or "publickey" in stderr:
                # No identity this machine holds is authorized. Set the key up
                # here rather than dead-ending on "run lager ssh-setup first":
                # that is one command doing what this one could, and the box
                # password it asks for is the same password install would have
                # needed anyway. `lager ssh-setup` still exists and is still
                # the fix when any OTHER command hits this — it just is not a
                # prerequisite for install.
                click.secho("No SSH key on this machine is authorized on the box.", fg='yellow')
                click.echo()
                if not (yes or click.confirm(
                    "Set up the lager_box key now? (one box-password prompt, "
                    "then the rest of the install runs unattended)",
                    default=True,
                )):
                    no_usable_identity_error(ssh_host, ip).die()
                click.echo()
                # .die() rather than letting the LagerError propagate: this
                # sits inside the broad `except Exception` below, which would
                # otherwise re-wrap it as a generic "SSH connectivity check
                # failed" and discard the guidance it carries. SystemExit is a
                # BaseException and passes straight through — the convention
                # LagerError.die() exists for.
                try:
                    provision_lager_box_key(ssh_host)
                except LagerError as exc:
                    exc.die()
                identity = lager_box_key_if_present()
                click.secho("SSH connection OK", fg='green')
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
                        # Use StrictHostKeyChecking=accept-new to accept new keys only.
                        # Re-probe rather than reuse the first answer: the first
                        # attempt never got far enough to learn which identity the
                        # box accepts.
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
                                no_usable_identity_error(ssh_host, ip).die()
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
                LagerError(
                    f'SSH connection to {ssh_host} failed.',
                    cause=(result.stderr or "").strip() or 'ssh exited without explaining why.',
                    fixes=[
                        f'Confirm the box is online: ping {ip}',
                        f'Authorize this machine if it has not been: lager ssh-setup --box {ip}',
                        f'Reproduce it directly: ssh {ssh_host}',
                    ],
                    raw=result.stderr,
                ).die()
        else:
            click.secho("SSH connection OK", fg='green')
    except subprocess.TimeoutExpired:
        LagerError(
            f'SSH connection to {ssh_host} timed out after 15 seconds.',
            cause='The box did not answer. It is offline, or the network drops the packets.',
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
    except (Exit, Abort):
        raise
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
    click.echo("  Host CLI: ~/.lager_venv (installed from the box checkout)")
    # The image step dominates the wait, so say up front where it will come
    # from. Only release tags have a published image; a branch always builds.
    if no_pull:
        click.echo("  Box image: build on the box (--no-pull)")
    elif _box_image_ref_for_version(version):
        click.echo(f"  Box image: pre-built {version} if published, else build on the box")
    else:
        click.echo(f"  Box image: build on the box ('{version}' is not a release tag)")
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
    click.echo("This can take several minutes.\n")

    deploy_args = [str(deploy_script), ip, "--user", user, "--version", version, "--skip-add-box"]

    if skip_jlink:
        deploy_args.append("--skip-jlink")
    if skip_firewall:
        deploy_args.append("--skip-firewall")
    if skip_verify:
        deploy_args.append("--skip-verify")
    # Only ever pass the flag that OVERRIDES a default, so the script keeps
    # ownership of what the default is.
    if no_pull:
        deploy_args.append("--no-pull")
    elif pull:
        deploy_args.append("--pull")
    if corporate_vpn:
        deploy_args.extend(["--corporate-vpn", corporate_vpn])

    # ttl_seconds: the deploy script below tears down the container serving
    # the :9000 lock API and spends most of its run rebuilding it, so this
    # lock survives on its TTL rather than on renewals — it must outlast the
    # script's own timeout. Derived from that timeout rather than fixed, so
    # raising --timeout cannot leave the install's own lock reaped mid-deploy.
    # See install_lock_ttl_seconds.
    with auto_lock_around_command(
        ip, box or ip, 'install', ttl_seconds=install_lock_ttl_seconds(deploy_timeout),
    ) as lock_session:
        try:
            # Run the deploy script, streaming output to the terminal.
            #
            # Suspended: step 8 of that script removes the lager container
            # and rebuilds the image, which on a cold cache is fifteen-plus
            # minutes with nothing listening on :9000. Renewals across that
            # window cannot succeed, and counting them as failures is how a
            # perfectly healthy install came to print a warning that its
            # lock was about to expire.
            with lock_session.suspended():
                result = subprocess.run(
                    deploy_args,
                    check=False,
                    # None = no timeout, which is what --timeout 0 asks for.
                    timeout=deploy_timeout or None,
                )

            if result.returncode != 0:
                click.echo()
                click.secho("Deployment failed!", fg='red', err=True)
                click.secho("Check the output above for details.", fg='yellow', err=True)
                ctx.exit(1)

        except subprocess.TimeoutExpired:
            # Say what the budget actually was, not a literal that no longer
            # has to be 30 minutes, and name the way out. The timeout fires
            # during the build, after the old container is already gone, so
            # the operator needs to know a re-run is both safe and cheaper.
            click.echo()
            click.secho(
                f"Deployment timed out after {_format_duration(deploy_timeout)}.",
                fg='red', err=True,
            )
            click.secho(
                "The build can still be healthy -- this budget is not "
                "a verdict on the box.",
                fg='yellow', err=True,
            )
            click.secho(
                f"  Raise it with:  lager install --timeout {deploy_timeout * 2} ...\n"
                f"  or:             LAGER_INSTALL_TIMEOUT={deploy_timeout * 2} lager install ...\n"
                "  (--timeout 0 disables the budget entirely.)",
                fg='yellow', err=True,
            )
            click.secho(
                "Re-running is safe, and reuses whatever layers the interrupted "
                "build already cached.",
                fg='yellow', err=True,
            )
            ctx.exit(1)
        except (Exit, Abort):
            raise
        except Exception as e:
            click.echo()
            click.secho("Deployment failed!", fg='red', err=True)
            ctx.exit(1)

    click.echo()
    click.secho("Box deployment complete!", fg='green', bold=True)
    click.echo()

    # 6.5. Record what was installed, in /etc/lager.
    #
    # `lager hello` and `lager boxes` report the first field of
    # /etc/lager/version, so this write is what an operator sees. It goes
    # through the writer `lager update` uses, over non-interactive ssh, and a
    # failure ends the install. Printing success over a write that did not
    # happen is how a freshly installed box came to report an older release.
    # The deployment has already made /etc/lager group-writable by this login
    # user, which is all the writer needs -- no sudo. See _record_install_state.
    #
    # The ref is recorded as well (#266): the version file holds only a
    # number, and a branch that has not been bumped declares the same number
    # as the release tag it came from.
    from ... import __version__ as cli_version
    from ...box_storage import update_box_version

    click.echo("Recording the installed version on the box...")
    identity_args = ssh_identity_args(identity)
    try:
        box_cli_version, ref_content, build_hash = _record_install_state(
            _install_state_runner(ssh_host, identity_args),
            ssh_host,
            version=version,
            cli_version=cli_version,
        )
    except LagerError as exc:
        exc.die()
    except (subprocess.SubprocessError, OSError) as exc:
        LagerError(
            'The SSH connection failed while the CLI recorded the installed version.',
            cause=str(exc) or type(exc).__name__,
            fixes=[
                f'Confirm that the box answers: ssh {ssh_host} true',
                'Then run the install again.',
            ],
            raw=exc,
        ).die()
    click.secho(f"Recorded version {box_cli_version} ({ref_content}) on the box", fg='green')
    if not build_hash:
        click.secho(
            "Warning: the box reported no build hash, so the CLI did not write "
            "/etc/lager/build-hash. The next `lager update` has no build inputs to compare.",
            fg='yellow', err=True,
        )

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
                ["ssh", *identity_args, "-o", "BatchMode=yes", ssh_host,
                 f"test -f {BOXCFG_SUDOERS_MARKER} "
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
                    ["ssh", "-t", *identity_args, ssh_host, sudoers_cmd],
                    timeout=600,
                )
                if bootstrap_result.returncode != 0:
                    click.secho(
                        "Warning: The sudoers rule did not install. `lager box-config apply` "
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
                        ["ssh", *identity_args, "-o", "BatchMode=yes", ssh_host,
                         f"test -f {BOXCFG_SUDOERS_MARKER} "
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
    click.echo("  - Verify that the box works: lager hello --box [BOX_NAME]")
    click.echo("  - Please run 'lager update --box [BOX_NAME]' to update the box to the latest version")