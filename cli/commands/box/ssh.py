# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

"""
    lager.commands.box.ssh

    SSH into boxes
"""
import click
from click.exceptions import Exit
import shlex
import subprocess
import platform
from ...box_storage import resolve_and_validate_box
from ...context import get_default_box
from ._ssh import widened_identity_args


def _get_ssh_install_hint() -> str:
    """Get platform-specific SSH installation instructions."""
    system = platform.system().lower()

    if system == 'darwin':  # macOS
        return (
            "On macOS, SSH is usually pre-installed.\n"
            "If missing, install Xcode command line tools:\n"
            "  xcode-select --install"
        )
    elif system == 'windows':
        return (
            "On Windows, you can install SSH using:\n"
            "  Option 1: Enable OpenSSH in Windows Settings > Apps > Optional Features\n"
            "  Option 2: Install Git for Windows (includes SSH): https://git-scm.com/downloads\n"
            "  Option 3: Use Windows Subsystem for Linux (WSL)"
        )
    elif system == 'linux':
        return (
            "On Linux, install OpenSSH client:\n"
            "  Debian/Ubuntu: sudo apt install openssh-client\n"
            "  Fedora/RHEL:   sudo dnf install openssh-clients\n"
            "  Arch:          sudo pacman -S openssh"
        )
    else:
        return "Please install an SSH client for your operating system."


def _identity_args() -> list:
    """``-i`` flags for the interactive session, or ``[]``.

    The lager_box key (installed by `lager ssh-setup`) is not one of ssh's
    default identity filenames, so without ``-i`` it is never tried and a
    box that authorizes only it drops to a password prompt. It goes first,
    so such a box still connects without one.

    But ``-i`` does not append: naming any identity discards ssh's built-in
    default list (id_rsa, id_ed25519, ...). Offering lager_box alone
    therefore locked `lager ssh` out of every box the operator had
    authorized with one of their own keys -- `ssh user@box` worked,
    `lager ssh` said "Permission denied (publickey)", and the only cure was
    deleting ~/.ssh/lager_box. So each default identity that exists is named
    again after lager_box, restoring the list ssh would have used on its own.
    ~/.ssh/config identities and agent keys stay on offer either way (no
    IdentitiesOnly), and with no lager_box key nothing is passed at all, so
    ssh's own behavior is untouched.

    The non-interactive commands (install, uninstall, box-config) take a
    different route -- probe with the key, retry without it -- because they
    run under BatchMode and need to know which identity worked; see
    _ssh.probe_box_identity. That is not available here: a retry would mean
    a second interactive attempt, so everything is offered at once.
    """
    return widened_identity_args()


@click.command(context_settings=dict(ignore_unknown_options=True))
@click.pass_context
@click.option("--box", required=False, help="Lager Box name or IP")
@click.argument("command", nargs=-1, type=click.UNPROCESSED)
def ssh(ctx, box, command):
    """SSH into a box, or run a command on it.

    With no COMMAND, opens an interactive shell on the box. With a COMMAND,
    runs it on the box (like `ssh user@host <command>`) and returns its output
    and exit code — handy for scripting and one-off checks:

        lager ssh --box my-box -- ls -la /etc/lager

    Use `--` to separate lager's options from the remote command when the
    command contains its own dashed flags.
    """
    from ...box_storage import get_box_user

    # Use default box if none specified
    if not box:
        box = get_default_box(ctx)

    # Resolve and validate the box (handles both names and IPs)
    resolved_box = resolve_and_validate_box(ctx, box)

    # Get username from box storage (defaults to 'lagerdata' if not found)
    username = get_box_user(box) or 'lagerdata'

    # Build SSH command: lager_box first, then the operator's own default
    # identities, so a box authorized on either connects (see _identity_args).
    ssh_host = f'{username}@{resolved_box}'
    ssh_cmd = ['ssh', *_identity_args(), ssh_host]

    # When a command is supplied, append it so ssh runs it on the box and exits
    # (non-interactive). With no command, ssh opens an interactive shell as before.
    #
    # ssh joins multiple remote arguments with spaces and the box's shell
    # re-parses the result, so passing the tokens as-is loses the quoting of any
    # argument containing spaces or shell metacharacters (e.g. `sh -c 'exit 7'`
    # arrives at the box as `sh -c exit 7`). Re-quote each token and hand ssh a
    # single remote command string so the box reconstructs the original argv.
    if command:
        ssh_cmd.append(' '.join(shlex.quote(arg) for arg in command))

    try:
        # Run SSH as a child process (not exec) so Click ctx.call_on_close hooks run.
        try:
            ret = subprocess.call(ssh_cmd)
        except KeyboardInterrupt:
            ret = 130
        ctx.exit(ret if ret is not None else 0)
    except FileNotFoundError:
        click.secho('Error: SSH client not found', fg='red', err=True)
        click.secho(_get_ssh_install_hint(), err=True)
        ctx.exit(1)
    except PermissionError:
        click.secho('Error: Permission denied executing SSH', fg='red', err=True)
        click.secho('Check that SSH is installed and executable.', err=True)
        ctx.exit(1)
    except OSError as e:
        error_str = str(e).lower()
        if 'no such file' in error_str:
            click.secho('Error: SSH client not found', fg='red', err=True)
            click.secho(_get_ssh_install_hint(), err=True)
        else:
            click.secho(f'Error: System error running SSH: {e}', fg='red', err=True)
        ctx.exit(1)
    except Exit:
        raise
    except Exception as e:
        click.secho(f'Error connecting to {ssh_host}: {e}', fg='red', err=True)
        click.secho('Troubleshooting tips:', err=True)
        click.secho(f'  1. Verify box is online: lager hello --box {box}', err=True)
        click.secho(f'  2. Check SSH key is set up: ssh-copy-id {ssh_host}', err=True)
        click.secho(f'  3. Test direct connection: ssh {ssh_host}', err=True)
        ctx.exit(1)
