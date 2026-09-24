# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

"""
    lager.gateway_tunnel

    Local TCP tunnels to a box's raw debug ports, through its gateway.

    An authenticating gateway can only check credentials on HTTP, so a box
    behind one does not publish its raw-TCP debug ports (GDB, OpenOCD telnet
    and TCL, RTT). Instead the gateway accepts an HTTP ``CONNECT`` on the
    debug-service port (8765) and, once the request is authorized, splices
    the connection to that port in the Lager container. The handshake is
    specified in ``docs/reference/gateway-auth-contract.md`` §10.

    :class:`GatewayTunnel` listens on 127.0.0.1 and opens one ``CONNECT``
    per accepted connection, each with a freshly resolved bearer token, so a
    GDB client that disconnects and reconnects an hour later still gets in.
    :func:`choose_route` decides whether a box needs a tunnel at all; a plain
    Lager box publishes its debug ports and is reached directly, as before.

    Nothing here is GDB-specific: a tunnel is a (box, port) pair, which is
    what a general ``lager box tunnel`` command would build on.
"""
import errno
import socket
import sys
import threading
import time

import click
from requests.structures import CaseInsensitiveDict

from .errors import LagerError
from .gateway_auth import (
    ACCESS_DOCS_URL,
    DISCOVERY_HEADER,
    auth_headers_for_box,
    auth_server_for_box,
    handle_gateway_denial,
    record_box_auth_server,
)

# The gateway accepts CONNECT on the debug-service port.
TUNNEL_SERVICE_PORT = 8765
# Budget for reaching the gateway and reading its answer to one CONNECT.
CONNECT_TIMEOUT = 10.0
# A response head larger than this is not a gateway talking to us.
MAX_HEAD_BYTES = 64 * 1024
# After one side of a spliced connection ends, how long the other side gets
# to finish on its own before both sockets are closed under it.
CLOSE_GRACE_SECONDS = 2.0
# How often the accept loop wakes to see whether it was closed.
_ACCEPT_POLL_SECONDS = 0.5
_CHUNK = 64 * 1024

ROUTE_DIRECT = 'direct'
ROUTE_TUNNEL = 'tunnel'


class TunnelError(LagerError):
    """A CONNECT the gateway answered with something other than 200."""

    def __init__(self, problem, *, status=None, **kwargs):
        super().__init__(problem, **kwargs)
        self.status = status


class TunnelUnsupported(TunnelError):
    """Whatever answered on the debug-service port does not do CONNECT.

    That is a plain Lager box (its own debug service answers 501), or a
    gateway from before debug tunnels existed.
    """


class TunnelTargetDown(TunnelError):
    """The gateway supports tunnels, but nothing listens on that port."""


class TunnelUnreachable(TunnelError):
    """The debug-service port did not accept a TCP connection at all."""


class _Reply:
    """Response-shaped view of a CONNECT answer.

    ``handle_gateway_denial`` reads ``status_code``, ``headers`` and
    ``request.headers`` off a requests response; this gives it the same
    three, so a tunnel denial is reported in exactly the words every other
    command uses.
    """

    def __init__(self, status_code, headers, body, sent_headers):
        self.status_code = status_code
        self.headers = headers
        self.body = body
        self.request = type('_SentRequest', (), {'headers': sent_headers})()

    @property
    def is_denial(self):
        return (self.status_code in (401, 403, 503)
                and DISCOVERY_HEADER in self.headers)


def _read_head(sock):
    """Read one HTTP response head. Returns (status, headers, leftover).

    ``leftover`` is whatever arrived after the blank line. On a 200 it is
    already tunnel payload -- a GDB server can greet the moment the
    gateway splices -- so it must be forwarded, not dropped.
    """
    buf = b''
    while b'\r\n\r\n' not in buf:
        if len(buf) > MAX_HEAD_BYTES:
            raise ValueError('response head too large')
        chunk = sock.recv(4096)
        if not chunk:
            raise ValueError('connection closed before a response head')
        buf += chunk
    head, leftover = buf.split(b'\r\n\r\n', 1)
    lines = head.decode('iso-8859-1').split('\r\n')
    parts = lines[0].split(' ', 2)
    if len(parts) < 2 or not parts[0].startswith('HTTP/'):
        raise ValueError(f'not an HTTP response: {lines[0]!r}')
    status = int(parts[1])
    headers = CaseInsensitiveDict()
    for line in lines[1:]:
        name, sep, value = line.partition(':')
        if sep:
            headers[name.strip()] = value.strip()
    return status, headers, leftover


def _read_body(sock, headers, leftover, limit=4096):
    """Best-effort read of a short error body, for the message."""
    try:
        length = min(int(headers.get('Content-Length', '0')), limit)
    except ValueError:
        length = 0
    body = leftover
    try:
        while len(body) < length:
            chunk = sock.recv(length - len(body))
            if not chunk:
                break
            body += chunk
    except OSError:
        pass
    return body[:limit].decode('utf-8', 'replace').strip()


def _send_connect(box_ip, port, auth_headers, *, service_port, timeout):
    """One CONNECT round trip. Returns (socket or None, reply, leftover).

    The socket comes back only on a 200, still open and in blocking mode;
    on anything else it is closed here.
    """
    try:
        sock = socket.create_connection((box_ip, service_port), timeout=timeout)
    except OSError as exc:
        raise TunnelUnreachable(
            f'Box {box_ip} did not answer on port {service_port}.',
            cause=str(exc),
            fixes=[f'Check that the box is online: lager hello --box {box_ip}'],
        )
    lines = [f'CONNECT {box_ip}:{port} HTTP/1.1', f'Host: {box_ip}:{port}']
    lines += [f'{name}: {value}' for name, value in auth_headers.items()]
    request = ('\r\n'.join(lines) + '\r\n\r\n').encode('iso-8859-1')
    try:
        sock.sendall(request)
        status, headers, leftover = _read_head(sock)
    except (OSError, ValueError) as exc:
        sock.close()
        # Something that is not a CONNECT-speaking gateway: a peer that
        # hangs up or answers garbage cannot open a tunnel either way.
        raise TunnelUnsupported(
            f'Box {box_ip} did not answer the tunnel request.',
            cause=str(exc),
        )
    if status == 200:
        sock.settimeout(None)
        return sock, _Reply(status, headers, '', dict(auth_headers)), leftover
    body = _read_body(sock, headers, leftover)
    sock.close()
    return None, _Reply(status, headers, body, dict(auth_headers)), b''


def open_tunnel(box_ip, port, *, service_port=TUNNEL_SERVICE_PORT,
                timeout=CONNECT_TIMEOUT):
    """Open one tunnel to ``port`` on the box. Returns (socket, leftover).

    The bearer token is resolved for this call, never reused from an
    earlier one: tokens are short-lived and ``auth_headers_for_box``
    refreshes them, and a pinned ``LAGER_GATEWAY_TOKEN`` just rides along.

    A gateway denial raises the same actionable error as every other
    command (contract §6.3), including the in-call retry when the box was
    not yet known to be gated but a session for its auth server exists.
    Every other refusal raises a :class:`TunnelError` subclass.
    """
    headers = auth_headers_for_box(box_ip)
    sock, reply, leftover = _send_connect(
        box_ip, port, headers, service_port=service_port, timeout=timeout)
    if sock is not None:
        return sock, leftover

    if reply.is_denial:
        record_box_auth_server(box_ip, reply.headers[DISCOVERY_HEADER])
        if reply.status_code == 401 and 'Authorization' not in headers:
            retry_headers = auth_headers_for_box(box_ip)
            if retry_headers:
                sock, reply, leftover = _send_connect(
                    box_ip, port, retry_headers,
                    service_port=service_port, timeout=timeout)
                if sock is not None:
                    return sock, leftover
        if reply.is_denial:
            handle_gateway_denial(reply, box_ip)
            # handle_gateway_denial raises for every denial status; this is
            # only reached if a future status slips past it.
            raise TunnelError(
                f'Box {box_ip} refused the tunnel (HTTP {reply.status_code}).',
                status=reply.status_code, fixes=[f'Details: {ACCESS_DOCS_URL}'])

    raise _refusal(box_ip, port, reply)


def _refusal(box_ip, port, reply):
    """The error for a non-200 CONNECT answer that is not a denial."""
    detail = reply.body or None
    if reply.status_code == 403:
        return TunnelError(
            f'The gateway of box {box_ip} does not tunnel port {port}.',
            status=403,
            cause=detail or 'Gateways tunnel only the debug ports: GDB '
                            '2331-2342, OpenOCD 4444-4447 and 6666-6669, '
                            'RTT 9090-9097.',
        )
    if reply.status_code == 502:
        return TunnelTargetDown(
            f'Nothing is listening on port {port} on box {box_ip}.',
            status=502,
            cause=detail or 'The debug server there is not running.',
        )
    return TunnelUnsupported(
        f'Box {box_ip} did not open a tunnel (HTTP {reply.status_code}).',
        status=reply.status_code,
        cause=detail,
    )


def probe_tunnel(box_ip, port, *, service_port=TUNNEL_SERVICE_PORT,
                 settle_seconds=5.0, timeout=CONNECT_TIMEOUT):
    """True if the box's gateway opens a tunnel to ``port``.

    False when nothing on the debug-service port speaks CONNECT. A 502 is
    retried for up to ``settle_seconds``, because a debug server that the
    box has only just started can take a moment to listen. Denials and
    other refusals raise.
    """
    deadline = time.monotonic() + settle_seconds
    while True:
        try:
            sock, _ = open_tunnel(box_ip, port, service_port=service_port,
                                  timeout=timeout)
        except TunnelUnsupported:
            return False
        except TunnelTargetDown as err:
            if time.monotonic() >= deadline:
                raise TunnelTargetDown(
                    f'The gateway of box {box_ip} could not reach the debug '
                    f'server on port {port}.',
                    status=err.status,
                    cause=err.cause or 'The server did not start listening, '
                                       'or it stopped.',
                )
            time.sleep(0.5)
            continue
        sock.close()
        return True


def port_reachable(box_ip, port, timeout=2.0):
    """True if a plain TCP connect to the box's port succeeds."""
    try:
        with socket.create_connection((box_ip, port), timeout=timeout):
            return True
    except OSError:
        return False


def choose_route(box_ip, port, *, service_port=TUNNEL_SERVICE_PORT,
                 settle_seconds=5.0, direct_timeout=2.0):
    """How a client on this machine should reach ``port`` on the box.

    Returns :data:`ROUTE_TUNNEL` or :data:`ROUTE_DIRECT`, or raises a
    :class:`~cli.errors.LagerError` the caller should show.

    1. Ask for a tunnel. Only a gateway that supports tunnels answers 200.
       A plain Lager box answers the CONNECT with its own debug service's
       501, so its debug server is never touched by this check.
    2. No tunnel, and the box has never answered with a gateway denial:
       a plain Lager box, reached directly exactly as before.
    3. No tunnel, but the box is known to be gated: its gateway predates
       tunnels. If the port is reachable anyway (the box publishes it, or
       the recorded mapping is stale and the address now belongs to a plain
       box), connect directly; otherwise say the gateway needs an update.

    Step 3's direct connect is the only one that reaches the debug server
    itself, and it only runs on a gated box whose gateway cannot tunnel.
    """
    known_gated = bool(auth_server_for_box(box_ip))
    try:
        if probe_tunnel(box_ip, port, service_port=service_port,
                        settle_seconds=settle_seconds):
            return ROUTE_TUNNEL
    except TunnelUnreachable:
        # The command that called this has just used that same port, so
        # this is a passing fault. A plain box keeps its old behaviour
        # rather than failing on it; a gated box has no other way in.
        if known_gated:
            raise
        return ROUTE_DIRECT
    # Read again: an old enforcing gateway answers the probe's first
    # CONNECT with a denial, which records the box as gated just now.
    if not auth_server_for_box(box_ip):
        return ROUTE_DIRECT
    if port_reachable(box_ip, port, timeout=direct_timeout):
        return ROUTE_DIRECT
    raise LagerError(
        f'Port {port} on box {box_ip} is reachable only through its gateway, '
        'and that gateway does not support debug tunnels yet.',
        cause='The debug server runs on the box. The gateway in front of it '
              'must be updated before a local debugger can reach it.',
        fixes=['Ask the administrator of the box to update its gateway.',
               f'Details: {ACCESS_DOCS_URL}'],
    )


def _is_addr_in_use(exc):
    return exc.errno in (errno.EADDRINUSE, getattr(errno, 'WSAEADDRINUSE', None))


class GatewayTunnel:
    """A 127.0.0.1 listener that tunnels each connection to one box port.

    ::

        tunnel = GatewayTunnel(box_ip, 2331)
        tunnel.bind()            # LagerError if the local port is taken
        tunnel.serve_forever()   # until close(), Ctrl-C, or a fatal refusal

    or ``tunnel.start()`` to serve from a background thread.

    Every accepted connection gets its own CONNECT, opened with a freshly
    resolved token, so several clients can be connected at once and a
    client can come back after its token has been refreshed. A refusal that
    will not go away by itself -- a denial, an unsupported gateway, a port
    it will not tunnel -- ends the tunnel and is raised from
    ``serve_forever``. A 502 (the debug server is not listening right now)
    is reported for that connection only.
    """

    def __init__(self, box_ip, remote_port, *, local_port=None,
                 service_port=TUNNEL_SERVICE_PORT, box_label=None,
                 notify=None):
        self.box_ip = box_ip
        self.remote_port = remote_port
        self.local_port = remote_port if local_port is None else local_port
        self.service_port = service_port
        self.box_label = box_label or box_ip
        self._notify = notify or (lambda msg: click.secho(msg, fg='yellow', err=True))
        self._listener = None
        self._thread = None
        self._closed = threading.Event()
        self._lock = threading.Lock()
        self._active = set()
        self.fatal = None

    # -- lifecycle ---------------------------------------------------------

    def bind(self):
        """Claim 127.0.0.1:<local_port>. Never any other interface.

        Binding all interfaces would put the box's debug port -- halt,
        read memory, reflash -- on this machine's network, without the
        gateway's check in front of it.
        """
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        if sys.platform != 'win32':
            # Lets a port whose last tunnel is in TIME_WAIT be reused. On
            # Windows the same option would let two listeners share a port.
            listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            listener.bind(('127.0.0.1', self.local_port))
        except OSError as exc:
            listener.close()
            if _is_addr_in_use(exc):
                raise LagerError(
                    f'Local port {self.local_port} is already in use.',
                    cause='Another program, or another tunnel, already listens '
                          f'on 127.0.0.1:{self.local_port}.',
                    fixes=['Pick a free local port with --local-port <PORT>.'],
                )
            raise
        listener.listen(8)
        listener.settimeout(_ACCEPT_POLL_SECONDS)
        self._listener = listener
        self.local_port = listener.getsockname()[1]
        return self.local_port

    def serve_forever(self):
        """Accept and tunnel connections until closed.

        Raises the refusal that ended the tunnel, if one did.
        """
        if self._listener is None:
            self.bind()
        try:
            while not self._closed.is_set():
                try:
                    conn, _ = self._listener.accept()
                except socket.timeout:
                    continue
                except OSError:
                    break           # the listener was closed under us
                conn.settimeout(None)
                threading.Thread(target=self._handle, args=(conn,),
                                 daemon=True).start()
        finally:
            self.close()
        if self.fatal is not None:
            raise self.fatal

    def start(self):
        """Serve from a daemon thread. A fatal refusal is reported via
        ``notify`` and left on ``self.fatal``."""
        if self._listener is None:
            self.bind()

        def run():
            try:
                self.serve_forever()
            except LagerError as err:
                self._notify(err.format_message())

        self._thread = threading.Thread(target=run, daemon=True)
        self._thread.start()
        return self._thread

    def close(self):
        """Stop listening and drop every open tunnel. Idempotent."""
        self._closed.set()
        with self._lock:
            listener, self._listener = self._listener, None
            active = list(self._active)
            self._active.clear()
        if listener is not None:
            listener.close()
        for sock in active:
            _hard_close(sock)

    @property
    def closed(self):
        return self._closed.is_set()

    # -- per connection ----------------------------------------------------

    def _track(self, sock):
        with self._lock:
            if self._closed.is_set():
                return False
            self._active.add(sock)
            return True

    def _untrack(self, *socks):
        with self._lock:
            for sock in socks:
                self._active.discard(sock)

    def _handle(self, local):
        if not self._track(local):
            _hard_close(local)
            return
        try:
            upstream, leftover = open_tunnel(
                self.box_ip, self.remote_port, service_port=self.service_port)
        except (TunnelTargetDown, TunnelUnreachable) as err:
            # Can pass: the server restarts, the network recovers. Drop
            # this connection and keep listening for the next one.
            self._untrack(local)
            _hard_close(local)
            self._notify(f'{err.problem} Your debugger was disconnected.')
            return
        except LagerError as err:
            self._untrack(local)
            _hard_close(local)
            if not self._closed.is_set():
                self.fatal = err
                self.close()
            return
        except Exception as exc:  # noqa: BLE001 -- one connection, not the tunnel
            self._untrack(local)
            _hard_close(local)
            self._notify(f'Tunnel to {self.box_label}:{self.remote_port} '
                         f'failed: {exc}')
            return
        if not self._track(upstream):
            self._untrack(local)
            _hard_close(local)
            _hard_close(upstream)
            return
        try:
            remote_first = _splice(local, upstream, leftover)
        finally:
            self._untrack(local, upstream)
            _hard_close(local)
            _hard_close(upstream)
        if remote_first and not self._closed.is_set():
            self._notify(
                f'Box {self.box_label} closed the tunnel to port '
                f'{self.remote_port}. The debug server stopped, or your '
                'access to the box was revoked.')


def _hard_close(sock):
    try:
        sock.shutdown(socket.SHUT_RDWR)
    except OSError:
        pass
    try:
        sock.close()
    except OSError:
        pass


def _splice(local, upstream, leftover=b''):
    """Copy bytes both ways until both directions end.

    Returns True if the box side ended first -- the gateway closed the
    tunnel, or the server behind it went away -- as opposed to the local
    client hanging up.

    Each direction ends with a half-close so the peer sees a clean EOF.
    Once one direction has ended, the other gets ``CLOSE_GRACE_SECONDS`` to
    finish on its own before the caller closes both sockets.
    """
    first = []
    first_lock = threading.Lock()
    one_done = threading.Event()

    def pump(src, dst, name):
        try:
            while True:
                data = src.recv(_CHUNK)
                if not data:
                    break
                dst.sendall(data)
        except OSError:
            pass
        finally:
            with first_lock:
                if not first:
                    first.append(name)
            try:
                dst.shutdown(socket.SHUT_WR)
            except OSError:
                pass
            one_done.set()

    if leftover:
        try:
            local.sendall(leftover)
        except OSError:
            return False
    down = threading.Thread(target=pump, args=(upstream, local, 'remote'), daemon=True)
    up = threading.Thread(target=pump, args=(local, upstream, 'local'), daemon=True)
    down.start()
    up.start()
    one_done.wait()
    down.join(CLOSE_GRACE_SECONDS)
    up.join(CLOSE_GRACE_SECONDS)
    return first[0] == 'remote'
