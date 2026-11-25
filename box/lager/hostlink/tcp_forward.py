# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

"""Forward one container port to a service on the host, byte for byte.

The hardware service speaks HTTP, so it gets the relay, which adds the link
token. The debug service and the oscilloscope daemon carry streams (RTT,
WebSocket captures) and are forwarded as plain TCP instead: a listener here
on the port the service holds on a Linux Box, connected to the same service
running natively on the host.

    python3 -m lager.hostlink.tcp_forward --listen 8765 --to 48765

The host name comes from the endpoint record, so this follows the agent.

With ``--add-token`` (the debug service's forward) each connection's first
line is taken as an HTTP request line and the link token is added after it as
an ``Authorization`` header, which the host's debug service requires. The
debug service speaks HTTP/1.0, one request per connection, so the first
request is the only one. A connection whose first line does not arrive, or
whose token cannot be read, is closed without reaching the host. Other
forwards (the oscilloscope daemon's WebSocket) are carried unchanged.
"""
from __future__ import annotations

import argparse
import logging
import socket
import socketserver
import threading

from . import config

logger = logging.getLogger(__name__)

CONNECT_TIMEOUT_S = 5.0
_CHUNK = 65536
# An HTTP request line longer than this is not one the debug service sends.
MAX_REQUEST_LINE = 8192
REQUEST_LINE_TIMEOUT_S = 10.0


def _pump(src: socket.socket, dst: socket.socket) -> None:
    try:
        while True:
            data = src.recv(_CHUNK)
            if not data:
                break
            dst.sendall(data)
    except OSError:
        pass
    finally:
        # Half-close, so the other direction can still drain.
        try:
            dst.shutdown(socket.SHUT_WR)
        except OSError:
            pass


def target_host(endpoint_file=None) -> str:
    """The host's name as seen from here; the default when no record exists."""
    try:
        return config.load_endpoint(endpoint_file).connect_host
    except config.EndpointError:
        return config.DEFAULT_CONNECT_HOST


def read_request_line(sock: socket.socket) -> bytes | None:
    """Bytes read up to and including the first CRLF, plus whatever arrived
    with it; None if the connection ends, stalls, or sends too much first."""
    sock.settimeout(REQUEST_LINE_TIMEOUT_S)
    data = b""
    try:
        while b"\r\n" not in data:
            chunk = sock.recv(_CHUNK)
            if not chunk:
                return None
            data += chunk
            if len(data) > MAX_REQUEST_LINE and b"\r\n" not in data[:MAX_REQUEST_LINE]:
                return None
    except OSError:
        return None
    finally:
        sock.settimeout(None)
    return data


def with_token(data: bytes, token: str) -> bytes:
    """``data`` with an Authorization header for ``token`` after its first line."""
    line, rest = data.split(b"\r\n", 1)
    return line + b"\r\n" + f"Authorization: Bearer {token}\r\n".encode() + rest


def endpoint_token(endpoint_file=None) -> str | None:
    try:
        return config.load_endpoint(endpoint_file).token
    except config.EndpointError:
        return None


class _Handler(socketserver.BaseRequestHandler):
    def handle(self) -> None:
        server = self.server
        first = b""
        if server.token_source is not None:
            data = read_request_line(self.request)
            token = server.token_source() if data is not None else None
            if data is None or not token:
                logger.warning("closing a connection: %s",
                               "no request line" if data is None else "no link token")
                return
            first = with_token(data, token)
        host = server.resolve_host()
        try:
            upstream = socket.create_connection((host, server.to_port), CONNECT_TIMEOUT_S)
        except OSError as exc:
            logger.warning("cannot reach %s:%s: %s", host, server.to_port, exc)
            return
        upstream.settimeout(None)
        with upstream:
            if first:
                try:
                    upstream.sendall(first)
                except OSError:
                    return
            back = threading.Thread(target=_pump, args=(upstream, self.request), daemon=True)
            back.start()
            _pump(self.request, upstream)
            back.join()


class Forwarder(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True

    def __init__(self, listen, to_port, resolve_host, token_source=None):
        super().__init__(listen, _Handler)
        self.to_port = to_port
        self.resolve_host = resolve_host
        self.token_source = token_source


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--listen", type=int, required=True)
    parser.add_argument("--to", type=int, required=True, dest="to_port")
    parser.add_argument("--endpoint-file", default=None)
    parser.add_argument("--add-token", action="store_true",
                        help="add the link token to each connection's HTTP request")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s")
    server = Forwarder((args.host, args.listen), args.to_port,
                       lambda: target_host(args.endpoint_file),
                       (lambda: endpoint_token(args.endpoint_file)) if args.add_token else None)
    logger.info("Forwarding %s:%s to host port %s", args.host, args.listen, args.to_port)
    with server:
        server.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
