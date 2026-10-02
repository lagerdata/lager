# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0
"""
    lager.commands.box._gateway

    A gateway in front of lager: a container on lagernet, usually run by a
    control plane, that publishes lager's host ports and forwards to the lager
    container. Named by role only; lager does not know which product runs it.
"""
import shlex

from ._ssh import CONTROL_PLANE_CONFIG

# start_box.sh's exit status for a host port another container holds (its
# --preflight, or a refused `docker run`). setup_and_deploy_box.sh passes it
# through to `lager install`.
START_BOX_PORT_CONFLICT = 5

# Containers lager itself runs on lagernet.
_LAGER_CONTAINERS = ("lager", "pigpio", "controller")

# Prints "config" when control_plane.json is present, else a "lagernet:"
# header and then the containers on lagernet, one per line. The header makes
# an unrelated reply impossible to read as a container list.
GATEWAY_QUERY = (
    f"if [ -s {shlex.quote(CONTROL_PLANE_CONFIG)} ]; then echo config; else "
    "echo lagernet:; docker ps --filter network=lagernet --format '{{.Names}}' 2>/dev/null; fi"
)


def gateway_query_argv(ssh_host, identity_args, *, timeout=20):
    """ssh argv that runs GATEWAY_QUERY non-interactively. The caller runs
    it, so its own subprocess (and its tests' fake) is the one used."""
    return ["ssh", *identity_args, "-o", "BatchMode=yes",
            "-o", f"ConnectTimeout={timeout}", ssh_host, GATEWAY_QUERY]


def parse_gateway_query(stdout):
    """Names of non-lager containers on lagernet when the box has no
    control_plane.json, else an empty list.

    That pairing is what a box looks like after something deleted /etc/lager
    under a control plane's gateway: the gateway container still runs, but
    has no configuration, so it refuses every connection until the box is
    re-linked. Anything unexpected answers an empty list: this only decides
    whether to print a warning.
    """
    lines = [line.strip() for line in (stdout or "").splitlines() if line.strip()]
    if not lines or lines[0] != "lagernet:":
        return []
    return [n for n in lines[1:] if n not in _LAGER_CONTAINERS]


def warn_gateway_without_config(names, echo):
    """Print the re-link warning for parse_gateway_query's result."""
    if not names:
        return
    joined = ", ".join(f"'{n}'" for n in names)
    echo(f"Warning: {joined} runs on lagernet, but the box has no {CONTROL_PLANE_CONFIG}.", fg='yellow')
    echo("If that is a control plane's gateway in front of lager, the box is no longer", fg='yellow')
    echo("linked to it and refuses connections. lager cannot restore that file: re-link", fg='yellow')
    echo("the box from its control plane after this finishes.", fg='yellow')
