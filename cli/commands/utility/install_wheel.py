# Copyright 2024-2026 Lager Data LLC
# SPDX-License-Identifier: Apache-2.0

"""
    lager.commands.utility.install_wheel

    Command for installing a local Python wheel file on a lagerbox.
"""
import os
import sys
import tempfile
import click


@click.command()
@click.pass_context
@click.option('--box', required=False, help='Lagerbox name or IP')
@click.argument('wheel_path')
def install_wheel(ctx, box, wheel_path):
    """Install a local Python wheel file on a lagerbox"""
    from ...box_storage import resolve_and_validate_box
    from ..development.python import run_python_internal, MAX_ZIP_SIZE

    if not os.path.isfile(wheel_path):
        click.secho(f'Error: Wheel file not found: {wheel_path}', fg='red', err=True)
        sys.exit(1)
    if not wheel_path.endswith('.whl'):
        click.secho(f'Error: File does not appear to be a wheel (.whl): {wheel_path}', fg='red', err=True)
        sys.exit(1)
    if not os.access(wheel_path, os.R_OK):
        click.secho(f'Error: Permission denied reading wheel file: {wheel_path}', fg='red', err=True)
        sys.exit(1)

    wheel_size = os.path.getsize(wheel_path)
    if wheel_size > MAX_ZIP_SIZE:
        click.secho(
            f'Error: Wheel file is too large to upload ({wheel_size / 1_000_000:.1f}MB, max {MAX_ZIP_SIZE // 1_000_000}MB): {os.path.basename(wheel_path)}',
            fg='red', err=True,
        )
        sys.exit(1)

    wheel_basename = os.path.basename(wheel_path)
    package_name = wheel_basename.split('-')[0].replace('_', '-')

    target = resolve_and_validate_box(ctx, box)

    click.secho(f'Installing {wheel_basename} on {box or target}...', fg='blue')

    script_content = f"""import subprocess
import sys

wheel_file = {repr(wheel_basename)}
package_name = {repr(package_name)}

# Uninstall previous version (best effort - ignore failure if not installed)
result = subprocess.run(['pip3', 'uninstall', '-y', package_name], capture_output=True, text=True)
if 'Successfully uninstalled' in result.stdout:
    print(f'Uninstalled previous version of {{package_name}}', flush=True)

# Install from the wheel file
result = subprocess.run(['pip3', 'install', '-q', '--force-reinstall', wheel_file])
if result.returncode != 0:
    print('Error: pip install failed', flush=True)
    sys.exit(1)
print(f'Successfully installed {{package_name}}', flush=True)
"""

    with tempfile.NamedTemporaryFile(mode='w', delete=False, suffix='.py') as f:
        f.write(script_content)
        temp_script = f.name

    try:
        run_python_internal(
            ctx,
            temp_script,
            target,
            env=(),
            passenv=(),
            kill=False,
            download=(),
            allow_overwrite=False,
            signum='SIGTERM',
            timeout=300,
            detach=False,
            port=(),
            org=None,
            args=(),
            extra_files=[wheel_path],
        )
    finally:
        os.unlink(temp_script)
