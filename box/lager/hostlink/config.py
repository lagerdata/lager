# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

"""The endpoint record shared by the host agent and the Box container.

One JSON file, written by whoever provisions the agent and read by both
sides. The agent reads where to listen and which token to require; the relay
in the container reads where to connect and which token to present.

The file holds a secret. It is replaced atomically, so a reader sees the old
record or the new one, never a partial write. See ``save_endpoint`` for why
the directory, not the file mode, is what keeps the token private.
"""
from __future__ import annotations

import ipaddress
import json
import os
import secrets
import socket
import tempfile
from dataclasses import asdict, dataclass
from typing import Optional

ENDPOINT_FILENAME = "host_agent.json"

# Inside the container the Box's config directory is always here.
CONTAINER_CONFIG_DIR = "/etc/lager"
ENDPOINT_ENV = "LAGER_HOSTLINK_CONFIG"

# The agent listens on loopback only. Hardware control must never be
# reachable from the LAN through the agent; the Box's published ports are the
# only network-facing surface, exactly as on Linux.
DEFAULT_BIND = "127.0.0.1"
# The name both Docker Desktop and OrbStack give the host from a container.
DEFAULT_CONNECT_HOST = "host.docker.internal"

RECORD_VERSION = 1
_MIN_TOKEN_CHARS = 32


class EndpointError(Exception):
    """The endpoint record is missing, unreadable, or not usable."""


@dataclass(frozen=True)
class Endpoint:
    """Where the agent listens and how the container reaches it."""

    token: str
    port: int
    bind: str = DEFAULT_BIND
    connect_host: str = DEFAULT_CONNECT_HOST
    version: int = RECORD_VERSION

    def base_url(self) -> str:
        """URL the container connects to."""
        host = self.connect_host
        if ":" in host and not host.startswith("["):
            host = f"[{host}]"
        return f"http://{host}:{self.port}"


def endpoint_path(config_dir: Optional[str] = None) -> str:
    """Path of the endpoint record.

    ``LAGER_HOSTLINK_CONFIG`` wins; otherwise the file sits in ``config_dir``,
    which defaults to the container's config directory.
    """
    override = os.environ.get(ENDPOINT_ENV)
    if override:
        return override
    return os.path.join(config_dir or CONTAINER_CONFIG_DIR, ENDPOINT_FILENAME)


def new_token() -> str:
    """256 bits from the OS, URL-safe so it survives a header unescaped."""
    return secrets.token_urlsafe(32)


def free_loopback_port(bind: str = DEFAULT_BIND) -> int:
    """A port that was free on ``bind`` a moment ago."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind((bind, 0))
        return probe.getsockname()[1]


def is_loopback(host: str) -> bool:
    """True for ``localhost`` and any loopback IP address."""
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _validate(data: object, source: str) -> Endpoint:
    if not isinstance(data, dict):
        raise EndpointError(f"{source}: expected a JSON object")
    token = data.get("token")
    port = data.get("port")
    if not isinstance(token, str) or len(token) < _MIN_TOKEN_CHARS:
        raise EndpointError(f"{source}: 'token' is missing or too short")
    if isinstance(port, bool) or not isinstance(port, int) or not 0 < port < 65536:
        raise EndpointError(f"{source}: 'port' must be an integer from 1 to 65535")
    bind = data.get("bind", DEFAULT_BIND)
    connect_host = data.get("connect_host", DEFAULT_CONNECT_HOST)
    if not isinstance(bind, str) or not bind:
        raise EndpointError(f"{source}: 'bind' must be a non-empty string")
    if not is_loopback(bind):
        # The record sits in a directory the Box container can write. A bind
        # address taken from it must not put the agent, which runs as the
        # Mac's user, on the network.
        raise EndpointError(f"{source}: 'bind' must be a loopback address, not {bind!r}")
    if not isinstance(connect_host, str) or not connect_host:
        raise EndpointError(f"{source}: 'connect_host' must be a non-empty string")
    version = data.get("version", RECORD_VERSION)
    if version != RECORD_VERSION:
        raise EndpointError(
            f"{source}: endpoint record version {version!r} is not supported "
            f"(this build reads version {RECORD_VERSION})"
        )
    return Endpoint(token=token, port=port, bind=bind,
                    connect_host=connect_host, version=version)


def load_endpoint(path: Optional[str] = None) -> Endpoint:
    """Read and validate the endpoint record."""
    path = path or endpoint_path()
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except FileNotFoundError:
        raise EndpointError(f"{path}: no host agent endpoint record") from None
    except (OSError, ValueError) as exc:
        raise EndpointError(f"{path}: cannot read endpoint record ({exc})") from exc
    return _validate(data, path)


def save_endpoint(endpoint: Endpoint, path: str) -> None:
    """Write the record atomically.

    Mode 0644 rather than 0600, deliberately: the container reads this file
    as a different user (``www-data``) than the one that wrote it, and a file
    share between a Mac and a Linux VM does not carry ownership across. What
    keeps the token private is the directory it lives in, which the
    provisioner creates mode 0700 on the host.
    """
    directory = os.path.dirname(os.path.abspath(path))
    os.makedirs(directory, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".host_agent.", suffix=".tmp", dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(asdict(endpoint), fh, indent=2, sort_keys=True)
            fh.write("\n")
        os.chmod(tmp, 0o644)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def ensure_endpoint(path: str, *, port: Optional[int] = None,
                    bind: str = DEFAULT_BIND,
                    connect_host: str = DEFAULT_CONNECT_HOST) -> Endpoint:
    """Return the record at ``path``, creating it if there is none.

    Idempotent: an existing valid record is returned untouched, so running
    the provisioner twice does not rotate the token under a live container.
    A record that exists but cannot be used is an error, not something to
    overwrite silently.
    """
    if os.path.exists(path):
        return load_endpoint(path)
    endpoint = Endpoint(
        token=new_token(),
        port=port if port is not None else free_loopback_port(bind),
        bind=bind,
        connect_host=connect_host,
    )
    save_endpoint(endpoint, path)
    return endpoint


# Where the host runs the services a Linux Box runs inside its container on
# 8765 and 8085. They cannot use those numbers on the host: the container
# publishes them there. Loopback only, like the agent.
HOST_DEBUG_PORT = 48765
HOST_SCOPE_PORT = 48085
