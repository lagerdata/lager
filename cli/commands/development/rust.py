# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

"""
Run a pre-compiled binary on the box.

`lager rust <binary>` uploads an already-compiled executable (e.g. a Rust ELF)
to the box and runs it directly, streaming stdout/stderr/exit code back to the
terminal — the binary analog of `lager python <script.py>`. It shares the whole
execution engine with `lager python` (see `_runner.py`), so `--env`, `--detach`,
`--reattach`, `--kill`, `--download`, `--add-file`, `--port` and SIGINT-to-kill
all behave identically. The Python-interpreter-only features (`--continue`,
`--console`, `lager.pause()` breakpoints) have no binary equivalent and are
omitted.

The binary must be a Linux executable for the box's CPU architecture (the box
runs aarch64 or x86_64 Linux in a container). Cross-compile a statically linked
binary, e.g.:

    cargo build --release --target aarch64-unknown-linux-musl

Since a binary cannot ``import lager``, it interacts with hardware over plain
HTTP to the in-container box services:

    POST http://localhost:8080/invoke          (low-level device calls)
    POST http://localhost:9000/supply/command  (high-level commands)
    GET  http://localhost:9000/nets/list        (net discovery)

Companion data files staged with ``--add-file`` land in the binary's working
directory, so they're reachable by relative path.
"""
import io
import os

import click

from ._runner import (
    RunnerSpec,
    run_internal,
    handle_reattach,
    _SIGNAL_CHOICES,
    _get_signal_number,
    collect_output_callback,
)
from ...core.param_types import EnvVarType, PortForwardType

ELF_MAGIC = b'\x7fELF'


def _build_rust_runnable(ctx, runnable, post_data, extra_files):
    """Append the binary (and any --add-file companions) to the multipart body."""
    with open(runnable, 'rb') as f:
        binary_content = f.read()

    # Pre-flight: a box-targeted binary is a Linux ELF. A macOS-native build
    # (Mach-O) or a Windows PE would upload fine but fail to exec on the box, so
    # warn early rather than after a confusing remote error.
    if binary_content[:4] != ELF_MAGIC:
        click.secho(
            f"Warning: '{os.path.basename(runnable)}' is not a Linux ELF binary. "
            "Cross-compile for the box (e.g. --target aarch64-unknown-linux-musl).",
            fg='yellow', err=True,
        )

    post_data.append(('binary', (os.path.basename(runnable), io.BytesIO(binary_content), 'application/octet-stream')))

    for path in (extra_files or []):
        with open(path, 'rb') as f:
            post_data.append(('add_file', (os.path.basename(path), io.BytesIO(f.read()), 'application/octet-stream')))


_RUST_SPEC = RunnerSpec(
    session_method='run_exec',
    build_runnable=_build_rust_runnable,
    cli_name='rust',
    watch_stdin_resume=False,
)


def run_rust_internal(ctx, runnable, box, env, passenv, kill, download, allow_overwrite, signum, timeout, detach, port, org, args, extra_files=None, callback=None, dut_name=None):
    return run_internal(_RUST_SPEC, ctx, runnable, box, env, passenv, kill, download, allow_overwrite, signum, timeout, detach, port, org, args, extra_files=extra_files, callback=callback, dut_name=dut_name)


def run_rust_internal_get_output(ctx, runnable, box, env, passenv, kill, download, allow_overwrite, signum, timeout, detach, port, org, args, extra_files=None):
    return run_rust_internal(ctx, runnable, box, env, passenv, kill, download, allow_overwrite, signum, timeout, detach, port, org, args, extra_files=extra_files, callback=collect_output_callback)


@click.command()
@click.pass_context
@click.argument('runnable', required=False, type=click.Path(exists=True, dir_okay=False))
@click.option("--box", required=False, help="Lagerbox name or IP")
@click.option(
    '--env',
    multiple=True, type=EnvVarType(), help='Environment variable (FOO=BAR)')
@click.option(
    '--passenv',
    multiple=True, help='Environment variable to inherit')
@click.option('--kill', default=None, help='Kill a specific process by process ID')
@click.option('--kill-all', is_flag=True, default=False, help='Kill all running binaries')
@click.option('--download', type=click.Path(exists=False, dir_okay=False), multiple=True, help='File to download after completion')
@click.option('--allow-overwrite', is_flag=True, default=False, help='Overwrite existing files when downloading')
@click.option('--signal', 'signum', default='SIGTERM', type=_SIGNAL_CHOICES, help='Signal to use with --kill/--kill-all', show_default=True)
@click.option('--timeout', type=click.IntRange(min=0), default=0, required=False, help='Max runtime in seconds (0=no timeout)')
@click.option('--detach', '-d', is_flag=True, required=False, default=False, help='Detach')
@click.option('--port', '-p', multiple=True, help='Port forwarding (SRC_PORT[:DST_PORT][/PROTOCOL])', type=PortForwardType())
@click.option('--org', default=None, hidden=True)
@click.option('--add-file', type=click.Path(exists=True, dir_okay=False), multiple=True, help='Companion file to upload alongside the binary')
@click.option('--reattach', default=None, help='Reattach to detached process by process ID')
@click.argument('args', nargs=-1)
def rust(ctx, runnable, box, env, passenv, kill, kill_all, download, allow_overwrite, signum, timeout, detach, port, org, add_file, reattach, args):
    """Run a pre-compiled binary on box"""
    from ...box_storage import resolve_and_validate_box

    # --kill, --kill-all, --reattach are management ops: skip lock check
    skip_lock = bool(kill or kill_all or reattach)

    box_name = box
    box_ip = resolve_and_validate_box(ctx, box_name, _skip_lock_check=skip_lock)

    if not runnable and not kill and not kill_all and not reattach:
        raise click.UsageError('Please supply a RUNNABLE, --kill, --kill-all, or --reattach option')

    if kill:
        session = ctx.obj.get_session_for_box(box_ip, box_name=box_name)
        signum_val = _get_signal_number(signum)
        resp = session.kill_python(box_ip, kill, signum_val)
        if resp.status_code == 422:
            try:
                error_data = resp.json()
                click.secho(error_data.get('error', 'Invalid request'), fg='red', err=True)
            except Exception:
                click.secho(f'Invalid process ID: {kill}', fg='red', err=True)
            ctx.exit(1)
        resp.raise_for_status()
        click.echo(f'Process {kill} killed')
        return

    if kill_all:
        session = ctx.obj.get_session_for_box(box_ip, box_name=box_name)
        signum_val = _get_signal_number(signum)
        resp = session.kill_python(box_ip, None, signum_val)
        resp.raise_for_status()
        click.echo('All processes killed')
        return

    if reattach:
        session = ctx.obj.get_session_for_box(box_ip, box_name=box_name)
        handle_reattach(ctx, box_ip, reattach, session, box_name, cli_name='rust')
        return

    if not allow_overwrite:
        import pathlib
        for filename in download:
            basename = os.path.basename(filename)
            file = pathlib.Path(basename)
            if file.exists():
                raise click.UsageError(f'File {basename} exists; please rename it or use the --allow-overwrite flag')

    # Pass the box name as an environment variable (parity with `lager python`)
    env = list(env) if env else []
    if box_name:
        env.append(f'LAGER_BOX={box_name}')

    run_rust_internal(ctx, runnable, box_ip, env, passenv, False, download, allow_overwrite, signum, timeout, detach, port, org, args, add_file, dut_name=box_name)
