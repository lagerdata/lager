# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

"""`lager install` / `update` / `uninstall` when the target over SSH is a Mac.

The Mac is provisioned by the same steps as a local install
(``cli.deployment.macos_box``), run on that Mac: the installer module is
copied over and started there by ``cli.deployment.macos_remote``. The one
difference from a local install is that the Box's ports are published on
every interface, as a Linux Box's are, because other machines must reach it.

Nothing here runs for a Linux target. `lager install` asks the target for
``uname -s`` once; any answer but ``Darwin``, including no answer, keeps the
Linux path exactly as it was. `update` and `uninstall` ask nothing: they go
by the ``platform`` recorded with the saved Box at install.
"""
import json
import os
import re
import shlex
import subprocess
from pathlib import Path

import click

from ...box_storage import add_box, delete_box, get_box_name_by_ip, load_boxes
from ...errors import LagerError

PLATFORM = "macos"
# Outside Application Support/Lager, which an uninstall removes while the
# runner in it is still executing.
REMOTE_INSTALLER_DIR = '"$HOME/Library/Caches/com.lagerdata.installer"'
REMOTE_SOURCE_DIR = '"$HOME/Library/Application Support/Lager/src/shipped"'
NO_PYTHON_STATUS = 97
# /usr/bin/python3 is a stub that opens a dialog unless the command line
# developer tools are installed, so it is used only when they are.
_FIND_PYTHON = (
    'PY=; for p in /opt/homebrew/bin/python3 /usr/local/bin/python3; do '
    'if [ -x "$p" ]; then PY="$p"; break; fi; done; '
    'if [ -z "$PY" ] && xcode-select -p >/dev/null 2>&1; then PY=/usr/bin/python3; fi; '
    f'if [ -z "$PY" ]; then exit {NO_PYTHON_STATUS}; fi; ')
SHIPPED_FILES = ("macos_box.py", "macos_remote.py")


def _ssh(ssh_host, identity_args, command, *, tty=False, run=None, **kwargs):
    # subprocess.run is looked up when called, not when this module is
    # imported, so a test that replaces it replaces it here too.
    argv = ["ssh", *identity_args]
    argv += ["-t"] if tty else ["-o", "BatchMode=yes"]
    return (run or subprocess.run)([*argv, ssh_host, command], **kwargs)


def remote_is_macos(ssh_host, identity_args, run=None):
    """True only when the target answers ``uname -s`` with ``Darwin``.

    Never raises and never waits long: a target that does not answer, or
    answers anything unexpected, is not a Mac as far as the caller is
    concerned, and continues down the Linux path unchanged.
    """
    try:
        result = _ssh(ssh_host, [*identity_args, "-o", "ConnectTimeout=10"], "uname -s",
                      run=run, stdin=subprocess.DEVNULL, capture_output=True, text=True,
                      timeout=20)
        if result.returncode != 0 or not isinstance(result.stdout, str):
            return False
        lines = result.stdout.strip().splitlines()
        return bool(lines) and lines[-1].strip() == "Darwin"
    except Exception:  # noqa: BLE001 - any failure means "not a Mac"
        return False


def saved_macos_box(ip):
    """True when a saved Box at ``ip`` was installed as a Mac. Reads no network."""
    try:
        boxes = load_boxes()
    except Exception:  # noqa: BLE001 - an unreadable file is not a Mac
        return False
    return any(isinstance(info, dict) and info.get("ip") == ip
               and info.get("platform") == PLATFORM for info in boxes.values())


def _checked(result, what, ssh_host):
    if result.returncode != 0:
        raise LagerError(
            f"The CLI did not {what} on {ssh_host}.",
            cause=((getattr(result, "stderr", "") or "").strip().splitlines() or
                   [f"ssh exited with status {result.returncode}."])[-1],
            fixes=[f"Confirm the Mac answers: ssh {ssh_host} true"])


def _pipe_tar(tar_argv, ssh_host, identity_args, remote_dir, *, cwd, stdin=None,
              popen=subprocess.Popen):
    """``tar`` here, piped into ``tar -x`` in ``remote_dir`` on the Mac."""
    # No AppleDouble (._name) entries when this side is a Mac too.
    env = dict(os.environ, COPYFILE_DISABLE="1")
    tar = popen(tar_argv, cwd=str(cwd), env=env, stdout=subprocess.PIPE,
                stdin=subprocess.PIPE if stdin is not None else None)
    remote = f"mkdir -p {remote_dir} && tar -xf - -C {remote_dir}"
    ssh = popen(["ssh", *identity_args, "-o", "BatchMode=yes", ssh_host, remote],
                stdin=tar.stdout, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    tar.stdout.close()
    if stdin is not None:
        tar.stdin.write(stdin)
        tar.stdin.close()
    _out, err = ssh.communicate(timeout=900)
    tar.wait(timeout=60)
    if tar.returncode != 0 or ssh.returncode != 0:
        raise LagerError(
            f"The CLI did not copy the installer to {ssh_host}.",
            cause=((err or "").strip().splitlines() or ["tar or ssh failed."])[-1])


def ship_installer(ssh_host, identity_args, popen=subprocess.Popen):
    from ... import deployment
    _pipe_tar(["tar", "-cf", "-", *SHIPPED_FILES], ssh_host, identity_args,
              REMOTE_INSTALLER_DIR, cwd=Path(deployment.__file__).parent, popen=popen)


def ship_source(checkout, ssh_host, identity_args, run=None,
                popen=subprocess.Popen):
    """Copy the checkout's Box source to the Mac, replacing an earlier copy.

    A development version exists nowhere the Mac could download it from.
    Tracked files only, as they are on disk now.
    """
    run = run or subprocess.run
    listed = run(["git", "-C", str(checkout), "ls-files", "-z", "--", "box", "cli/__init__.py"],
                 capture_output=True, timeout=120)
    if listed.returncode != 0 or not listed.stdout:
        raise LagerError("The CLI did not list the Box source to copy.",
                         cause=f"`git ls-files` failed in {checkout}.")
    names = b"\0".join(name for name in listed.stdout.split(b"\0")
                       if name and os.path.lexists(os.path.join(str(checkout).encode(), name)))
    _checked(_ssh(ssh_host, identity_args, f"rm -rf {REMOTE_SOURCE_DIR}", run=run,
                  capture_output=True, text=True, timeout=120),
             "clear the previous Box source", ssh_host)
    _pipe_tar(["tar", "-cf", "-", "--null", "-T", "-"], ssh_host, identity_args,
              REMOTE_SOURCE_DIR, cwd=checkout, stdin=names + b"\0", popen=popen)


def run_remote(ssh_host, identity_args, arguments, run=None):
    """Run the installer on the Mac with a terminal; its JSON outcome."""
    result_file = f"{REMOTE_INSTALLER_DIR}/result.json"
    command = (f"rm -f {result_file}; {_FIND_PYTHON}"
               f'exec "$PY" {REMOTE_INSTALLER_DIR}/macos_remote.py '
               f"--result {result_file} {arguments}")
    status = _ssh(ssh_host, identity_args, command, tty=True, run=run).returncode
    if status == NO_PYTHON_STATUS:
        raise LagerError(
            f"There is no Python 3 on {ssh_host}.",
            fixes=["On the Mac: brew install python@3.12 (or `xcode-select --install`)",
                   "Then run this command again."])
    fetched = _ssh(ssh_host, identity_args, f"cat {result_file}", run=run,
                   capture_output=True, text=True, timeout=60)
    try:
        outcome = json.loads((fetched.stdout or "").strip().splitlines()[-1])
    except (ValueError, IndexError):
        raise LagerError(
            f"The installer on {ssh_host} did not report an outcome.",
            cause=f"It exited with status {status}.",
            fixes=["Run the command again; the output above shows where it stopped."])
    if not outcome.get("ok"):
        raise LagerError(outcome.get("problem") or "The installer failed on the Mac.",
                         cause=outcome.get("cause"), fixes=outcome.get("fixes") or [])
    return outcome


def install_remote_macos(ctx, *, ssh_host, ip, user, box, identity_args, version, yes,
                         fork=None):
    if fork:
        raise click.UsageError("--fork is not supported for a Box on a Mac yet.")
    from ... import __version__ as cli_version
    from ...deployment import macos_box

    checkout = macos_box.running_from_checkout()
    click.echo()
    click.secho(f"{ip} is a Mac.", fg='cyan', bold=True)
    if checkout is not None:
        click.echo(f"Installing a Lager Box on it from the checkout at {checkout}.")
    else:
        click.echo(f"Installing Lager Box {version} on it.")
    click.echo("This sets up a background agent for the instruments plugged "
               "into that Mac and a container for the Box. The Mac's password "
               f"is asked for once, to create {macos_box.ETC_DIR}.")
    if not yes and not click.confirm("Continue?", default=True):
        click.secho("Installation cancelled.", fg='yellow')
        ctx.exit(1)

    click.echo("Copying the installer...")
    ship_installer(ssh_host, identity_args)
    arguments = (f"install --version {shlex.quote(version)} "
                 f"--cli-version {shlex.quote(cli_version)} --bind 0.0.0.0")
    if checkout is not None:
        ship_source(checkout, ssh_host, identity_args)
        arguments += f" --source {REMOTE_SOURCE_DIR}"
    outcome = run_remote(ssh_host, identity_args, arguments)

    # Always saved, unlike a Linux Box: the saved record is what tells every
    # later command which port this Box's script service is on.
    name = box if box is not None else get_box_name_by_ip(ip)
    if name is None:
        name = default_name(ip)
        if not yes:
            name = click.prompt("Box name", type=str, default=name).strip() or name
    add_box(name, ip, user=user, version=outcome.get("box_version"),
            python_port=outcome.get("python_port") or macos_box.PYTHON_SERVICE_PORT,
            platform=PLATFORM)

    click.echo()
    for line in outcome.get("skipped") or []:
        click.secho(f"Not set up -- {line}", fg='yellow')
    if outcome.get("skipped"):
        click.echo("Install what is missing on the Mac and run this command "
                   "again to add it.")
        click.echo()
    click.secho("Installation complete!", fg='green', bold=True)
    click.secho(f"Saved as '{name}'. You can now use: lager hello --box {name}", fg='cyan')


def default_name(ip):
    """A Box name for ``ip`` when none was given: letters, digits and dashes."""
    return "mac-" + re.sub(r"[^A-Za-z0-9]+", "-", ip).strip("-")


def _connect(ip, user):
    """``(ssh_host, identity_args)`` for a Mac that must already accept a key."""
    from ..box._ssh import probe_box_identity, ssh_identity_args

    ssh_host = f"{user}@{ip}"
    identity, result = probe_box_identity(ssh_host)
    _checked(result, "reach the Mac", ssh_host)
    return ssh_host, ssh_identity_args(identity)


def uninstall_remote_macos(ctx, *, ip, user, purge_config, yes, dry_run):
    click.echo(f"This removes from the Mac at {ip}: the Box container, the host "
               "agent and its launchd job.")
    click.echo("Everything in /etc/lager on it is removed too, after a copy is "
               "left in that user's home directory." if purge_config
               else "/etc/lager on it (saved nets, Box configuration) is kept.")
    if dry_run:
        click.echo("Dry run: nothing was changed.")
        return
    if not yes and not click.confirm("Continue?", default=False):
        click.secho("Uninstall cancelled.", fg='yellow')
        ctx.exit(1)
    ssh_host, identity_args = _connect(ip, user)
    ship_installer(ssh_host, identity_args)
    run_remote(ssh_host, identity_args,
               "uninstall" + (" --purge-config" if purge_config else ""))
    name = get_box_name_by_ip(ip)
    if name and (yes or click.confirm(
            f"Remove '{name}' from local .lager configuration?", default=True)):
        delete_box(name)
    click.secho("Uninstall complete.", fg='green', bold=True)


def update_remote_macos(ctx, *, ip, user, version, yes, check=False, fork=None):
    """`lager update` for a Box on a Mac: the install, run again."""
    if check:
        click.echo("`lager update --check` is not available for a Box on a Mac "
                   "yet. Run `lager update` to bring it to this CLI's version.")
        ctx.exit(2)
    from .update import default_box_version

    ssh_host, identity_args = _connect(ip, user)
    install_remote_macos(ctx, ssh_host=ssh_host, ip=ip, user=user,
                         box=get_box_name_by_ip(ip), identity_args=identity_args,
                         version=version or default_box_version(), yes=yes, fork=fork)
