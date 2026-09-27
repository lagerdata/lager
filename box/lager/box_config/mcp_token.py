# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

"""The box MCP server's optional bearer token: one file, four operations.

The token is off by default. The file's EXISTENCE is the switch: with no file
the MCP server on port 8100 answers every request, as it always has. With a
file, it answers only a request that carries ``Authorization: Bearer <token>``.

Two readers share this module so they cannot disagree. The MCP server's
middleware (``lager.mcp.auth``) calls :func:`load` to decide each request, and
the box-config shim calls :func:`state` for ``mcp-token status``. Both run in
the container as the same user, so ``status`` reports what the server sees.

The value is a secret, and it is kept out of everything that is not:

- It is never a key in ``box_config.json``, so ``box-config show``, ``export``
  and ``copy`` cannot carry it.
- The shim's audit record names the verb and nothing else.
- :func:`state` has no return path for the value.

Every function takes the path as an argument. Nothing here is module-level
state, so a test that points at its own temporary file is isolated by
construction.

Stdlib only: the shim's unit tests load this package without the box's
runtime dependencies.
"""

from __future__ import annotations

import os
import secrets
import stat

DISABLED = "disabled"      # no file: the server answers everyone
ENABLED = "enabled"        # a readable, non-empty file: the server asks for it
UNREADABLE = "unreadable"  # a file this user cannot read, or not a regular file
EMPTY = "empty"            # a file with nothing in it

# The two states in which the server refuses every request. A token file the
# server cannot use must never mean "open": an operator who enabled the token
# and then restored /etc/lager from a backup with the wrong owner would
# otherwise be running an open server while believing it was closed.
FAIL_CLOSED_STATES = (UNREADABLE, EMPTY)

TOKEN_BYTES = 32  # secrets.token_urlsafe(32) is 43 URL-safe characters
FILE_MODE = 0o600


class TokenError(Exception):
    """A refusal with a machine-readable ``code`` beside the sentence."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def load(path: str) -> tuple[str, bytes | None]:
    """Return ``(state, token)``. The token is ``None`` unless state is ENABLED.

    Only a missing file means DISABLED. Every other failure to produce a
    usable token is a fail-closed state, never DISABLED.
    """
    try:
        st = os.stat(path)
    except FileNotFoundError:
        return DISABLED, None
    except OSError:
        return UNREADABLE, None
    if not stat.S_ISREG(st.st_mode):
        return UNREADABLE, None
    try:
        with open(path, "rb") as f:
            token = f.read().strip()
    except FileNotFoundError:
        # Removed between the stat and the open: a concurrent `disable`.
        return DISABLED, None
    except OSError:
        return UNREADABLE, None
    if not token:
        return EMPTY, None
    return ENABLED, token


def state(path: str) -> str:
    """The state alone. There is deliberately no way to get the value here."""
    return load(path)[0]


def _new_token() -> str:
    return secrets.token_urlsafe(TOKEN_BYTES)


def _write_new_file(path: str, token: str) -> None:
    """Create ``path`` with the token in it, or raise FileExistsError.

    O_EXCL makes "create" and "refuse when one is already there" one atomic
    step, and the mode is on the file before a byte of the secret is.
    fchmod because the mode argument to open() is filtered by the umask.
    """
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, FILE_MODE)
    try:
        os.fchmod(fd, FILE_MODE)
        with os.fdopen(fd, "w", encoding="ascii") as f:
            f.write(token + "\n")
            f.flush()
            os.fsync(f.fileno())
    except BaseException:
        # A half-written file would be EMPTY, and EMPTY locks every client
        # out. Leave nothing behind rather than that.
        try:
            os.unlink(path)
        except OSError:
            pass
        raise


def enable(path: str) -> str:
    """Create the token file and return the new value. Refuses when one exists.

    It refuses rather than replaces because the value cannot be shown again:
    a second ``enable`` that silently minted a new token would break every
    client holding the first one. ``rotate`` is the verb that means that.
    """
    token = _new_token()
    try:
        _write_new_file(path, token)
    except FileExistsError:
        raise TokenError(
            "already-enabled",
            "An MCP token already exists on this box, and its value cannot be "
            "shown again. Use rotate to replace it.",
        ) from None
    return token


def rotate(path: str) -> str:
    """Replace the token file atomically and return the new value.

    Refuses when no file exists: turning authentication ON is `enable`'s job,
    and a `rotate` that did it as a side effect would surprise an operator
    who ran it against the wrong box. It does accept an UNREADABLE or EMPTY
    file, which makes it the repair for both.
    """
    if state(path) == DISABLED:
        raise TokenError(
            "not-enabled",
            "No MCP token exists on this box. Use enable to create one.",
        )
    token = _new_token()
    tmp = f"{path}.{secrets.token_hex(8)}.tmp"
    _write_new_file(tmp, token)
    try:
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return token


def disable(path: str) -> str:
    """Remove the token file. Returns the state it was in. Idempotent."""
    previous = state(path)
    try:
        os.unlink(path)
    except FileNotFoundError:
        return DISABLED
    return previous
