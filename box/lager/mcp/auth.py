# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

"""Optional bearer-token check in front of the box MCP server.

Off by default. The token file's existence is the switch (see
``lager.box_config.mcp_token``): with no file every request passes through
untouched, which is the server's behavior before this module existed. With a
file, a request passes only when it carries ``Authorization: Bearer <token>``.

This is a pure ASGI middleware on purpose. Starlette's ``BaseHTTPMiddleware``
runs the downstream app in a separate task and re-streams its response, which
interferes with the long-lived streaming responses the MCP streamable-HTTP
transport depends on. A pure ASGI wrapper either answers the request itself or
hands the untouched ``scope``/``receive``/``send`` to the app; it is never in
the response path of a request it lets through.

The file is looked at on every request, so ``enable``, ``rotate`` and
``disable`` take effect at once, with no restart. The read is cached on the
file's identity (inode, mtime, size), so the steady-state cost is one stat.

The token is a box-local static secret. It is not a gateway credential, and
it travels in cleartext HTTP like everything else on this port.
"""

from __future__ import annotations

import hmac
import logging
import os

from ..box_config import mcp_token

logger = logging.getLogger(__name__)

_BEARER = b"bearer "

_UNAUTHORIZED_BODY = (
    b"Unauthorized. This box requires a bearer token for MCP. Send the header "
    b"'Authorization: Bearer <token>'.\n"
)
_UNAVAILABLE_BODY = (
    b"The MCP token file on this box is unreadable or empty, so every request "
    b"is refused. On the operator machine, run 'lager box-config mcp-token "
    b"status' for the state, then 'rotate' to repair it or 'disable' to remove it.\n"
)


class BearerTokenMiddleware:
    """Require ``Authorization: Bearer <token>`` when a token file exists.

    All state lives on the instance. A module-level cache would be shared by
    every app in the process, and one app's token file would then answer for
    another's.
    """

    def __init__(self, app, token_path):
        self.app = app
        self.token_path = os.fspath(token_path)
        self._cache_key = None
        self._cache_value = (mcp_token.DISABLED, None)
        self._logged_state = None

    # --- token file ---

    def _load(self):
        """``(state, token)`` for this request, re-reading only on change."""
        try:
            st = os.stat(self.token_path)
        except FileNotFoundError:
            self._cache_key = None
            return mcp_token.DISABLED, None
        except OSError:
            self._cache_key = None
            return mcp_token.UNREADABLE, None
        # The inode is in the key because a rotate is an os.replace: a new
        # file of the same size, possibly inside the same mtime tick.
        key = (st.st_ino, st.st_mtime_ns, st.st_size)
        if key != self._cache_key:
            self._cache_value = mcp_token.load(self.token_path)
            self._cache_key = key
        return self._cache_value

    def _note(self, state):
        """One log line per state CHANGE. Never the header, never the token."""
        if state == self._logged_state:
            return
        self._logged_state = state
        if state == mcp_token.ENABLED:
            logger.info("MCP bearer token is enabled (%s)", self.token_path)
        elif state == mcp_token.DISABLED:
            logger.info("MCP bearer token is disabled: no token file, requests are not authenticated")
        else:
            logger.error(
                "MCP token file %s is %s: refusing every request until it is repaired",
                self.token_path, state,
            )

    # --- request ---

    @staticmethod
    def _authorized(scope, token):
        for name, value in scope.get("headers") or ():
            if name.lower() != b"authorization":
                continue
            if value[:len(_BEARER)].lower() != _BEARER:
                continue
            # bytes in, bytes out: compare_digest accepts any byte values, so
            # a header that is not ASCII is a wrong token, not a server error.
            if hmac.compare_digest(value[len(_BEARER):].strip(), token):
                return True
        return False

    async def _deny(self, scope, send, status, body, *, challenge):
        if scope["type"] == "websocket":
            # Closing before accept makes the server answer the handshake
            # with HTTP 403. 1008 is "policy violation".
            await send({"type": "websocket.close", "code": 1008})
            return
        headers = [
            (b"content-type", b"text/plain; charset=utf-8"),
            (b"content-length", str(len(body)).encode("ascii")),
        ]
        if challenge:
            headers.append((b"www-authenticate", b"Bearer"))
        await send({"type": "http.response.start", "status": status, "headers": headers})
        await send({"type": "http.response.body", "body": body})

    async def __call__(self, scope, receive, send):
        # `lifespan` (and anything else that is not a request) is not ours.
        if scope["type"] not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return

        state, token = self._load()
        self._note(state)

        if state == mcp_token.DISABLED:
            await self.app(scope, receive, send)
            return
        if state != mcp_token.ENABLED:
            # 503, not 401: a 401 would send the operator looking for a wrong
            # token when the fault is the file on the box.
            await self._deny(scope, send, 503, _UNAVAILABLE_BODY, challenge=False)
            return
        if self._authorized(scope, token):
            await self.app(scope, receive, send)
            return
        await self._deny(scope, send, 401, _UNAUTHORIZED_BODY, challenge=True)
