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
import sys
import time
import subprocess
from pathlib import Path

import click

from ...box_storage import add_box, delete_box, get_box_name_by_ip, load_boxes
from ...errors import LagerError

PLATFORM = "macos"
# Outside Application Support/Lager, which an uninstall removes while the
# runner in it is still executing.
REMOTE_INSTALLER_DIR = '"$HOME/Library/Caches/com.lagerdata.installer"'
# A copied checkout goes to a new directory each time, under this one. The
# running Box mounts the previous copy, so that copy is removed only after
# the new install has succeeded.
REMOTE_SOURCE_PARENT = '"$HOME/Library/Application Support/Lager/src"'
SHIPPED_PREFIX = "shipped-"
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


def shipped_dir(name):
    """The remote path, quoted for the remote shell, of one copied checkout."""
    return f'"$HOME/Library/Application Support/Lager/src/{name}"'


def ship_source(checkout, ssh_host, identity_args, run=None,
                popen=subprocess.Popen, name=None):
    """Copy the checkout's Box source to a new directory on the Mac.

    A development version exists nowhere the Mac could download it from.
    Tracked files only, as they are on disk now. The earlier copy, which the
    running Box still mounts, is left in place: see :func:`prune_shipped`.
    Returns the new directory's name under the source directory.
    """
    name = name or f"{SHIPPED_PREFIX}{time.strftime('%Y%m%dT%H%M%S')}-{os.getpid()}"
    run = run or subprocess.run
    listed = run(["git", "-C", str(checkout), "ls-files", "-z", "--", "box", "cli/__init__.py"],
                 capture_output=True, timeout=120)
    if listed.returncode != 0 or not listed.stdout:
        raise LagerError("The CLI did not list the Box source to copy.",
                         cause=f"`git ls-files` failed in {checkout}.")
    names = b"\0".join(name for name in listed.stdout.split(b"\0")
                       if name and os.path.lexists(os.path.join(str(checkout).encode(), name)))
    _pipe_tar(["tar", "-cf", "-", "--null", "-T", "-"], ssh_host, identity_args,
              shipped_dir(name), cwd=checkout, stdin=names + b"\0", popen=popen)
    return name


def prune_shipped(ssh_host, identity_args, keep, run=None):
    """Remove the copies of earlier checkouts, now that ``keep`` is running.
    Best effort: a copy left behind costs disk space, not a working Box."""
    run = run or subprocess.run
    command = (f"cd {REMOTE_SOURCE_PARENT} 2>/dev/null || exit 0; "
               f"for d in {SHIPPED_PREFIX}* shipped; do "
               f'[ -e "$d" ] && [ "$d" != {shlex.quote(keep)} ] && rm -rf "$d"; done; exit 0')
    try:
        _ssh(ssh_host, identity_args, command, run=run,
             capture_output=True, text=True, timeout=120)
    except Exception:  # noqa: BLE001 - cleanup only
        pass


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


def mac_sudo_needs_a_terminal(ssh_host, identity_args, isatty=None, run=None) -> bool:
    """True when the install will need the Mac's password and cannot ask.

    The only sudo step on a Mac creates /etc/lager. So: False whenever stdin
    is a terminal, when /etc/lager already exists and is writable, and when
    sudo needs no password. An ssh failure (255) counts as "no": the
    connection was just checked, and the install reports a real failure.
    """
    isatty = isatty or (lambda: sys.stdin.isatty())
    if isatty():
        return False
    from ...deployment import macos_box

    run = run or subprocess.run
    try:
        probe = _ssh(ssh_host, [*identity_args, "-o", "ConnectTimeout=10"],
                     f"{{ test -d {macos_box.ETC_DIR} && test -w {macos_box.ETC_DIR}; }} "
                     "|| sudo -n true",
                     run=run, stdin=subprocess.DEVNULL, capture_output=True, timeout=20)
    except (OSError, subprocess.SubprocessError):
        return False
    return probe.returncode not in (0, 255)


def install_remote_macos(ctx, *, ssh_host, ip, user, box, identity_args, version, yes,
                         fork=None, skip_jlink=False, skip_picoscope=False, update=False,
                         version_defaulted=True):
    if fork:
        raise click.UsageError("--fork is not supported for a Box on a Mac yet.")
    from ... import __version__ as cli_version
    from ...deployment import macos_box

    # Asked before anything is copied: the sudo step comes minutes in, and
    # without a terminal it can only fail there.
    if mac_sudo_needs_a_terminal(ssh_host, identity_args):
        raise LagerError(
            f"Installing on {ip} needs the Mac's password once, to create "
            f"{macos_box.ETC_DIR}, and there is no terminal to ask in.",
            cause="That Mac's sudo needs a password, and this command's input "
                  "is a pipe or a script.",
            fixes=["Run the same command in a terminal window, or create the "
                   "directory on that Mac first:"] + macos_box.config_dir_fixes(user)[:-1])

    # A version named on the command line wins over the checkout this CLI
    # runs from: the Mac then downloads that version itself.
    checkout = macos_box.running_from_checkout() if version_defaulted else None
    click.echo()
    click.secho(f"{ip} is a Mac.", fg='cyan', bold=True)
    if checkout is not None:
        click.echo(f"Installing a Lager Box on it from the checkout at {checkout}.")
    else:
        click.echo(f"Installing Lager Box {version} on it.")
    click.echo("This sets up a background agent for the instruments plugged "
               "into that Mac and a container for the Box. The Mac's password "
               f"is asked for once, if {macos_box.ETC_DIR} does not exist there yet.")
    # A Box reached from other machines listens on every interface. A Linux
    # Box is installed with a firewall in front of those ports; a Mac is not,
    # so say plainly that anyone on that Mac's network can reach the Box.
    ports = ", ".join(str(host) for host, _container in macos_box.PUBLISHED_PORTS)
    click.secho(f"The Box's ports ({ports}) will be reachable from any machine on "
                f"{ip}'s network, with no firewall in front of them. Install on a "
                "trusted network only.", fg='yellow')
    from ._local_install import LINUX_ONLY_INSTALL_OPTIONS, ignored_options
    # --user is the SSH user here, so it is not ignored.
    for flag, why in ignored_options(ctx, [option for option in LINUX_ONLY_INSTALL_OPTIONS
                                           if option[0] != "user"]):
        click.secho(f"Ignoring {flag}: {why}.", fg='yellow')
    if not yes and not click.confirm("Continue?", default=True):
        click.secho("Installation cancelled.", fg='yellow')
        ctx.exit(1)

    click.echo("Copying the installer...")
    ship_installer(ssh_host, identity_args)
    arguments = (f"install --version {shlex.quote(version)} "
                 f"--cli-version {shlex.quote(cli_version)} --bind 0.0.0.0")
    if skip_jlink:
        arguments += " --skip jlink"
    if skip_picoscope:
        arguments += " --skip picoscope"
    if update and not (skip_jlink or skip_picoscope):
        arguments += " --reuse-skips"
    shipped = None
    if checkout is not None:
        shipped = ship_source(checkout, ssh_host, identity_args)
        arguments += f" --source {shipped_dir(shipped)}"
    outcome = run_remote(ssh_host, identity_args, arguments)
    if shipped is not None:
        prune_shipped(ssh_host, identity_args, shipped)

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
    if outcome.get("ready"):
        click.echo("Set up: " + ", ".join(outcome["ready"]) + ".")
    for line in outcome.get("skipped") or []:
        click.secho(f"Not set up -- {line}", fg='yellow')
    if outcome.get("skipped"):
        click.echo("Install the missing parts on the Mac and run this command "
                   "again to add them.")
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


def uninstall_remote_macos(ctx, *, ip, user, purge_config, yes, dry_run,
                           keep_docker_images=False):
    click.echo(f"This removes from the Mac at {ip}: the Box container"
               + ("" if keep_docker_images else ", the Box images")
               + ", the host agent and its launchd job.")
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
               "uninstall" + (" --purge-config" if purge_config else "")
               + (" --keep-images" if keep_docker_images else ""))
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
                         version=version or default_box_version(), yes=yes, fork=fork,
                         update=True, version_defaulted=not version)
