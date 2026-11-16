# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

"""In-container relay to the host agent.

Runs inside the Box container on the port the hardware service normally
holds. Every existing caller -- the ``Device`` proxy, the nets handlers,
diagnostics, the script executor -- keeps posting to ``localhost:8080``
exactly as on Linux, and this process forwards each request to the native
agent on the host, adding the token and the protocol version.

That is the whole reason it exists as a process rather than as a change to
those callers: a Linux Box runs none of this, and none of its code had to be
edited to make room for it.

What the relay adds beyond forwarding:

* a handshake before the first forwarded request, repeated after any failure
  to reach the agent, so a restarted or upgraded agent is renegotiated;
* a re-read of the endpoint record whenever the file changes, so a
  re-provisioned agent is picked up without restarting the container;
* one clear, structured error when the agent cannot be reached, in place of
  a bare connection failure.

Driver errors are not touched. Status and body from the agent are returned
as they arrived, so the text a user sees matches a Linux Box.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Dict, Optional, Tuple

import urllib3

from . import auth, config, protocol

logger = logging.getLogger(__name__)

DEFAULT_LISTEN_HOST = "0.0.0.0"  # published by the container, as on Linux
DEFAULT_LISTEN_PORT = 8080

# Reaching the agent is a loopback-class hop; if it does not connect quickly
# it is not going to.
CONNECT_TIMEOUT_S = 3.0
HANDSHAKE_TIMEOUT_S = 5.0
# No read timeout on forwarded calls. The caller owns the deadline (a GPIO
# wait may legitimately block for minutes), and the agent bounds every driver
# call itself.
FORWARD_TIMEOUT = urllib3.Timeout(connect=CONNECT_TIMEOUT_S, read=None)

# Hop-by-hop headers are meaningful for one connection only (RFC 9110 7.6.1).
_HOP_BY_HOP = frozenset({
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
    "te", "trailer", "transfer-encoding", "upgrade", "host", "content-length",
})

_BOX_VERSION_PATH = "/etc/lager/version"


def _box_version() -> str:
    try:
        with open(_BOX_VERSION_PATH, "r", encoding="utf-8") as fh:
            return fh.read().strip().split("|", 1)[0]
    except OSError:
        return ""


class LinkError(Exception):
    """The agent could not be used. Carries the response to send instead."""

    def __init__(self, status: int, body: Dict):
        super().__init__(body.get("error", ""))
        self.status = status
        self.body = body


class Link:
    """Connection state to the agent, shared by all request threads."""

    def __init__(self, endpoint_file: Optional[str] = None,
                 pool: Optional[urllib3.PoolManager] = None):
        self._endpoint_file = endpoint_file or config.endpoint_path()
        self._pool = pool or urllib3.PoolManager(num_pools=4, maxsize=32, retries=False)
        self._lock = threading.Lock()
        self._endpoint: Optional[config.Endpoint] = None
        self._endpoint_mtime: Optional[float] = None
        self._negotiated: Optional[int] = None
        self._agent_info: Dict = {}
        self._last_error: Optional[Dict] = None

    # -- endpoint record ---------------------------------------------------

    def _load_endpoint_locked(self) -> config.Endpoint:
        """Current endpoint, re-read when the file changed. Lock held."""
        try:
            mtime = os.path.getmtime(self._endpoint_file)
        except OSError:
            self._endpoint = None
            self._endpoint_mtime = None
            self._negotiated = None
            raise LinkError(503, protocol.error_body(
                protocol.ERR_NOT_CONFIGURED,
                "The Lager host agent is not set up on this machine. "
                "Run 'lager install' again to repair the installation.",
            ))
        if self._endpoint is None or mtime != self._endpoint_mtime:
            try:
                self._endpoint = config.load_endpoint(self._endpoint_file)
            except config.EndpointError as exc:
                self._endpoint = None
                self._negotiated = None
                raise LinkError(503, protocol.error_body(
                    protocol.ERR_NOT_CONFIGURED,
                    "The Lager host agent configuration cannot be read. "
                    "Run 'lager install' again to repair the installation.",
                    details=str(exc),
                ))
            self._endpoint_mtime = mtime
            # A new record may mean a new agent: negotiate again.
            self._negotiated = None
        return self._endpoint

    # -- handshake ---------------------------------------------------------

    def _unreachable(self, exc: Exception) -> LinkError:
        return LinkError(503, protocol.error_body(
            protocol.ERR_AGENT_UNREACHABLE,
            "The Lager host agent is not responding. Hardware is unavailable "
            "until it is running again.",
            retryable=True,
            details=f"{type(exc).__name__}: {exc}",
        ))

    def _handshake_locked(self, endpoint: config.Endpoint) -> None:
        try:
            resp = self._pool.request(
                "POST", endpoint.base_url() + protocol.HANDSHAKE_PATH,
                body=json.dumps(protocol.handshake_request(_box_version())).encode("utf-8"),
                headers={
                    "Content-Type": "application/json",
                    auth.AUTH_HEADER: auth.bearer_header(endpoint.token),
                },
                timeout=urllib3.Timeout(connect=CONNECT_TIMEOUT_S, read=HANDSHAKE_TIMEOUT_S),
            )
        except urllib3.exceptions.HTTPError as exc:
            raise self._unreachable(exc) from exc

        try:
            body = json.loads(resp.data or b"{}")
        except ValueError:
            body = {}
        if resp.status == 200 and isinstance(body, dict):
            try:
                self._negotiated = protocol.negotiate(
                    body.get("protocol"), body.get("protocol"))
            except (ValueError, protocol.ProtocolMismatch) as exc:
                raise self._mismatch(str(exc)) from exc
            self._agent_info = body
            logger.info("Host agent handshake ok: protocol %s, agent %s on %s",
                        self._negotiated, body.get("agent_version"), body.get("platform"))
            return
        if resp.status == 401:
            raise LinkError(503, protocol.error_body(
                protocol.ERR_UNAUTHORIZED,
                "The Lager host agent rejected this Box's credentials. "
                "Run 'lager install' again to repair the installation.",
            ))
        if isinstance(body, dict) and body.get("code") == protocol.ERR_PROTOCOL_MISMATCH:
            raise self._mismatch(body.get("error", ""))
        raise LinkError(503, protocol.error_body(
            protocol.ERR_AGENT_UNREACHABLE,
            "The Lager host agent answered the handshake unexpectedly "
            f"(HTTP {resp.status}).",
            retryable=True,
        ))

    @staticmethod
    def _mismatch(details: str) -> LinkError:
        return LinkError(503, protocol.error_body(
            protocol.ERR_PROTOCOL_MISMATCH,
            "The Lager Box and the Lager host agent are different versions "
            "and cannot work together. Run 'lager update' to bring them back "
            "in step.",
            details=details or None,
        ))

    def ready_endpoint(self) -> Tuple[config.Endpoint, int]:
        """Endpoint and negotiated protocol, handshaking first if needed."""
        with self._lock:
            try:
                endpoint = self._load_endpoint_locked()
                if self._negotiated is None:
                    self._handshake_locked(endpoint)
            except LinkError as exc:
                self._last_error = exc.body
                raise
            self._last_error = None
            return endpoint, self._negotiated

    def mark_unreachable(self) -> None:
        """Forget the negotiation: the next request handshakes again."""
        with self._lock:
            self._negotiated = None

    # -- forwarding --------------------------------------------------------

    def _send(self, endpoint: config.Endpoint, negotiated: int, method: str,
              path: str, headers: Dict[str, str], body: Optional[bytes]):
        out_headers = {k: v for k, v in headers.items() if k.lower() not in _HOP_BY_HOP}
        # The caller's own Authorization header, if any, is not the agent's
        # business and must not shadow the link token.
        out_headers = {k: v for k, v in out_headers.items()
                       if k.lower() != auth.AUTH_HEADER.lower()}
        out_headers[auth.AUTH_HEADER] = auth.bearer_header(endpoint.token)
        out_headers[protocol.PROTOCOL_HEADER] = str(negotiated)
        try:
            return self._pool.request(
                method, endpoint.base_url() + path,
                body=body, headers=out_headers,
                timeout=FORWARD_TIMEOUT, redirect=False,
            )
        except urllib3.exceptions.HTTPError as exc:
            self.mark_unreachable()
            error = self._unreachable(exc)
            with self._lock:
                self._last_error = error.body
            raise error from exc

    def forward(self, method: str, path: str, headers: Dict[str, str],
                body: Optional[bytes]) -> Tuple[int, Dict[str, str], bytes]:
        """Send one request to the agent and return its response verbatim."""
        endpoint, negotiated = self.ready_endpoint()
        resp = self._send(endpoint, negotiated, method, path, headers, body)
        if resp.status in (401, 426):
            # The agent changed under us: a new token, or a new version. The
            # request was refused before any driver ran, so it is safe to
            # renegotiate and send it once more. If the link really is
            # broken, the handshake raises the clear error instead.
            self.mark_unreachable()
            endpoint, negotiated = self.ready_endpoint()
            resp = self._send(endpoint, negotiated, method, path, headers, body)
        resp_headers = {k: v for k, v in resp.headers.items()
                        if k.lower() not in _HOP_BY_HOP}
        return resp.status, resp_headers, resp.data

    def status(self) -> Dict:
        """Snapshot for diagnostics. Never includes the token."""
        with self._lock:
            endpoint = self._endpoint
            return {
                "endpoint_file": self._endpoint_file,
                "agent_url": endpoint.base_url() if endpoint else None,
                "connected": self._negotiated is not None,
                "protocol": self._negotiated,
                "agent": dict(self._agent_info) if self._negotiated is not None else {},
                "last_error": self._last_error,
            }


def make_handler(link: Link):
    """Request handler class bound to ``link``."""

    class RelayHandler(BaseHTTPRequestHandler):
        # Keep-alive matters: the Device proxy pools its connections, and a
        # connection per call would add a handshake to every instrument read.
        protocol_version = "HTTP/1.1"
        server_version = "lager-hostlink-relay"

        def log_message(self, fmt, *args):
            logger.debug("%s - %s", self.address_string(), fmt % args)

        def _send(self, status: int, headers: Dict[str, str], body: bytes) -> None:
            self.send_response(status)
            for key, value in headers.items():
                self.send_header(key, value)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)

        def _send_json(self, status: int, payload: Dict) -> None:
            self._send(status, {"Content-Type": "application/json"},
                       json.dumps(payload).encode("utf-8"))

        def _handle(self) -> None:
            try:
                length = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                self._send_json(400, protocol.error_body(
                    protocol.ERR_BAD_REQUEST, "Invalid Content-Length header"))
                return
            body = self.rfile.read(length) if length > 0 else None

            if self.path.startswith(protocol.RELAY_PREFIX):
                if self.path.split("?", 1)[0] == protocol.RELAY_PREFIX + "status":
                    try:
                        link.ready_endpoint()
                    except LinkError:
                        pass  # reported through status()
                    self._send_json(200, link.status())
                else:
                    self._send_json(404, protocol.error_body(
                        protocol.ERR_BAD_REQUEST, "Unknown relay endpoint"))
                return

            try:
                status, headers, data = link.forward(
                    self.command, self.path, dict(self.headers.items()), body)
            except LinkError as exc:
                self._send_json(exc.status, exc.body)
                return
            self._send(status, headers, data)

        do_GET = do_POST = do_PUT = do_DELETE = do_PATCH = do_HEAD = _handle

    return RelayHandler


def make_server(link: Link, host: str = DEFAULT_LISTEN_HOST,
                port: int = DEFAULT_LISTEN_PORT) -> ThreadingHTTPServer:
    server = ThreadingHTTPServer((host, port), make_handler(link))
    server.daemon_threads = True
    return server


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="Relay the Box's hardware service port to the Lager host agent.")
    parser.add_argument("--host", default=DEFAULT_LISTEN_HOST)
    parser.add_argument("--port", type=int, default=DEFAULT_LISTEN_PORT)
    parser.add_argument("--endpoint-file", default=None,
                        help="endpoint record (default: /etc/lager/host_agent.json)")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s")
    link = Link(args.endpoint_file)
    server = make_server(link, args.host, args.port)
    logger.info("Hostlink relay listening on %s:%s", args.host, args.port)
    try:
        link.ready_endpoint()
    except LinkError as exc:
        # Not fatal: the agent may simply start after the container. Every
        # request retries the handshake until it succeeds.
        logger.warning("Host agent not available yet: %s", exc)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
