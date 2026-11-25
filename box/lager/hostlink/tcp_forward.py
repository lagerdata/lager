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
Nothing is authenticated at this layer; the host services listen on loopback
only, which is the same exposure these ports have inside a Linux container.
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


class _Handler(socketserver.BaseRequestHandler):
    def handle(self) -> None:
        server = self.server
        host = server.resolve_host()
        try:
            upstream = socket.create_connection((host, server.to_port), CONNECT_TIMEOUT_S)
        except OSError as exc:
            logger.warning("cannot reach %s:%s: %s", host, server.to_port, exc)
            return
        upstream.settimeout(None)
        with upstream:
            back = threading.Thread(target=_pump, args=(upstream, self.request), daemon=True)
            back.start()
            _pump(self.request, upstream)
            back.join()


class Forwarder(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True

    def __init__(self, listen, to_port, resolve_host):
        super().__init__(listen, _Handler)
        self.to_port = to_port
        self.resolve_host = resolve_host


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--listen", type=int, required=True)
    parser.add_argument("--to", type=int, required=True, dest="to_port")
    parser.add_argument("--endpoint-file", default=None)
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s")
    server = Forwarder((args.host, args.listen), args.to_port,
                       lambda: target_host(args.endpoint_file))
    logger.info("Forwarding %s:%s to host port %s", args.host, args.listen, args.to_port)
    with server:
        server.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
