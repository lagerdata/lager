# Copyright 2024-2026 Lager Data LLC
# SPDX-License-Identifier: Apache-2.0

"""
    lager.commands.box.ssh

    SSH into boxes
"""
import click
from click.exceptions import Exit
import subprocess
import platform
from ...box_storage import resolve_and_validate_box
from ...context import get_default_box


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


@click.command()
@click.pass_context
@click.option("--box", required=False, help="Lagerbox name or IP")
def ssh(ctx, box):
    """SSH into a box"""
    from ...box_storage import get_box_user

    # Use default box if none specified
    if not box:
        box = get_default_box(ctx)

    # Resolve and validate the box (handles both names and IPs)
    resolved_box = resolve_and_validate_box(ctx, box)

    # Get username from box storage (defaults to 'lagerdata' if not found)
    username = get_box_user(box) or 'lagerdata'

    # Build SSH command
    ssh_host = f'{username}@{resolved_box}'

    try:
        # Run SSH as a child process (not exec) so Click ctx.call_on_close hooks run.
        try:
            ret = subprocess.call(['ssh', ssh_host])
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
