# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

"""
    cli.context.error_handlers

    Error handling utilities for CLI context
"""
import json
import click


# Error code sets for categorizing different error types
DOCKER_ERROR_CODES = set()

CANBUS_ERROR_CODES = {
    'canbus_up_failed',
}


class ElfHashMismatch(Exception):
    """Exception raised when ELF file hash doesn't match expected value"""
    pass


def print_docker_error(ctx, error):
    """
    Parse a docker error and print the output
    """
    if not error:
        return
    parsed = json.loads(error)
    stdout = parsed['stdout']
    stderr = parsed['stderr']
    click.echo(stdout, nl=False)
    click.secho(stderr, fg='red', err=True, nl=False)
    ctx.exit(parsed['returncode'])


def print_canbus_error(ctx, error):
    """
    Parse a CAN bus error and print helpful messages
    """
    if not error:
        return
    parsed = json.loads(error)
    if parsed['stdout']:
        click.secho(parsed['stdout'], fg='red', nl=False)
    if parsed['stderr']:
        click.secho(parsed['stderr'], fg='red', err=True, nl=False)
        if parsed['stderr'] == 'Cannot find device "can0"\n':
            click.secho('Please check adapter connection', fg='red', err=True)
