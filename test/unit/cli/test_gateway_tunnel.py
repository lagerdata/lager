# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

"""
Debug tunnels through a box's gateway (``cli/gateway_tunnel.py``).

A fake gateway -- a real socket server on 127.0.0.1 -- speaks the CONNECT
handshake from ``docs/reference/gateway-auth-contract.md`` §10 and then
echoes, so every test drives the tunnel over real sockets: bytes really
cross the splice in both directions, and the listener really binds.
"""
import base64
import json
import socket
import threading
import time

import pytest

from cli import gateway_auth, gateway_tunnel
from cli.errors import LagerError
from cli.gateway_tunnel import (
    ROUTE_DIRECT, ROUTE_TUNNEL, GatewayTunnel, TunnelError, TunnelTargetDown,
    choose_route, open_tunnel,
)

BOX = '127.0.0.1'
AUTH_URL = 'http://cp:3001'


@pytest.fixture(autouse=True)
def isolated_store(tmp_path, monkeypatch):
    monkeypatch.setenv('LAGER_GATEWAY_AUTH_FILE', str(tmp_path / 'gateway_auth.json'))
    monkeypatch.delenv('LAGER_GATEWAY_TOKEN', raising=False)


def make_jwt(exp, sub='u1'):
    header = base64.urlsafe_b64encode(b'{"alg":"HS256"}').rstrip(b'=')
    payload = base64.urlsafe_b64encode(
        json.dumps({'exp': exp, 'sub': sub}).encode()).rstrip(b'=')
    return f'{header.decode()}.{payload.decode()}.sig'


def response(status, reason, headers=None, body=b''):
    lines = [f'HTTP/1.1 {status} {reason}']
    for name, value in (headers or {}).items():
        lines.append(f'{name}: {value}')
    if body:
        lines.append(f'Content-Length: {len(body)}')
    return ('\r\n'.join(lines) + '\r\n\r\n').encode() + body


class FakeGateway:
    """A CONNECT-speaking gateway that echoes once a tunnel is open.

    ``answer(request_head, headers)`` returns the bytes to send back; when
    they start with a 200 the connection turns into an echo. Every request
    is recorded as (request line, headers dict).
    """

    def __init__(self, answer=None, greeting=b''):
        self.answer = answer or (lambda line, headers: response(200, 'Connection Established'))
        self.greeting = greeting
        self.requests = []
        self.conns = []
        self.sock = socket.socket()
        self.sock.bind(('127.0.0.1', 0))
        self.sock.listen(16)
        self.port = self.sock.getsockname()[1]
        self._stop = False
        threading.Thread(target=self._serve, daemon=True).start()

    def _serve(self):
        while not self._stop:
            try:
                conn, _ = self.sock.accept()
            except OSError:
                return
            threading.Thread(target=self._handle, args=(conn,), daemon=True).start()

    def _handle(self, conn):
        self.conns.append(conn)
        buf = b''
        while b'\r\n\r\n' not in buf:
            chunk = conn.recv(4096)
            if not chunk:
                conn.close()
                return
            buf += chunk
        head, rest = buf.split(b'\r\n\r\n', 1)
        lines = head.decode().split('\r\n')
        headers = {}
        for line in lines[1:]:
            name, _, value = line.partition(':')
            headers[name.strip()] = value.strip()
        self.requests.append((lines[0], headers))
        reply = self.answer(lines[0], headers)
        if not reply.startswith(b'HTTP/1.1 200'):
            conn.sendall(reply)
            conn.close()
            return
        # Response head and the server's greeting in ONE send, so the client
        # sees payload arrive in the same read as the head.
        conn.sendall(reply + self.greeting)
        try:
            if rest:
                conn.sendall(rest)
            while True:
                data = conn.recv(65536)
                if not data:
                    break
                conn.sendall(data)
        except OSError:
            pass
        finally:
            try:
                conn.shutdown(socket.SHUT_WR)
            except OSError:
                pass
            conn.close()

    def drop_all(self):
        """Close every open tunnel from the gateway side (access revoked)."""
        for conn in list(self.conns):
            try:
                conn.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            conn.close()

    def close(self):
        self._stop = True
        self.sock.close()
        self.drop_all()


@pytest.fixture
def gateway():
    gws = []

    def make(**kwargs):
        gw = FakeGateway(**kwargs)
        gws.append(gw)
        return gw
    yield make
    for gw in gws:
        gw.close()


def start_tunnel(gw, remote_port=2331, **kwargs):
    notes = []
    tunnel = GatewayTunnel(BOX, remote_port, local_port=0, service_port=gw.port,
                           notify=notes.append, **kwargs)
    tunnel.bind()
    errors = []

    def run():
        try:
            tunnel.serve_forever()
        except LagerError as err:
            errors.append(err)
    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    return tunnel, thread, notes, errors


def recv_exactly(sock, n, timeout=5):
    sock.settimeout(timeout)
    buf = b''
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            break
        buf += chunk
    return buf


def wait_for(predicate, timeout=5):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return predicate()


# ---------------------------------------------------------------------------
# The splice
# ---------------------------------------------------------------------------

def test_bytes_cross_both_ways(gateway):
    gw = gateway()
    tunnel, _, notes, _ = start_tunnel(gw)
    try:
        with socket.create_connection(('127.0.0.1', tunnel.local_port)) as c:
            c.sendall(b'$qSupported#37')
            assert recv_exactly(c, 14) == b'$qSupported#37'
        line, _ = gw.requests[0]
        assert line == f'CONNECT {BOX}:2331 HTTP/1.1'
    finally:
        tunnel.close()


def test_a_large_payload_arrives_intact(gateway):
    gw = gateway()
    tunnel, _, _, _ = start_tunnel(gw)
    payload = bytes(range(256)) * 16384        # 4 MiB
    try:
        with socket.create_connection(('127.0.0.1', tunnel.local_port)) as c:
            sender = threading.Thread(target=c.sendall, args=(payload,))
            sender.start()
            got = recv_exactly(c, len(payload), timeout=20)
            sender.join()
        assert got == payload
    finally:
        tunnel.close()


def test_bytes_after_the_response_head_are_forwarded(gateway):
    # A GDB server can greet the instant the gateway splices; those bytes
    # arrive in the same read as the 200 and must not be dropped.
    gw = gateway(greeting=b'+$OK#9a')
    tunnel, _, _, _ = start_tunnel(gw)
    try:
        with socket.create_connection(('127.0.0.1', tunnel.local_port)) as c:
            assert recv_exactly(c, 7) == b'+$OK#9a'
    finally:
        tunnel.close()


def test_several_connections_at_once(gateway):
    gw = gateway()
    tunnel, _, _, _ = start_tunnel(gw)
    try:
        clients = [socket.create_connection(('127.0.0.1', tunnel.local_port))
                   for _ in range(4)]
        for i, c in enumerate(clients):
            c.sendall(f'client-{i}'.encode())
        for i, c in enumerate(clients):
            assert recv_exactly(c, 8) == f'client-{i}'.encode()
        for c in clients:
            c.close()
        assert len(gw.requests) == 4
    finally:
        tunnel.close()


def test_every_connection_resolves_a_fresh_token(gateway, monkeypatch):
    gw = gateway()
    tokens = iter(['t1', 't2', 't3'])
    monkeypatch.setattr(gateway_tunnel, 'auth_headers_for_box',
                        lambda box: {'Authorization': f'Bearer {next(tokens)}'})
    tunnel, _, _, _ = start_tunnel(gw)
    try:
        for _ in range(2):
            with socket.create_connection(('127.0.0.1', tunnel.local_port)) as c:
                c.sendall(b'x')
                recv_exactly(c, 1)
        assert [h['Authorization'] for _, h in gw.requests] == ['Bearer t1', 'Bearer t2']
    finally:
        tunnel.close()


def test_a_pinned_token_rides_on_the_connect(gateway, monkeypatch):
    monkeypatch.setenv('LAGER_GATEWAY_TOKEN', 'ci-token')
    gw = gateway()
    sock, _ = open_tunnel(BOX, 2331, service_port=gw.port)
    sock.close()
    assert gw.requests[0][1]['Authorization'] == 'Bearer ci-token'


def test_a_plain_box_request_carries_no_token(gateway):
    gw = gateway()
    sock, _ = open_tunnel(BOX, 2331, service_port=gw.port)
    sock.close()
    assert 'Authorization' not in gw.requests[0][1]


# ---------------------------------------------------------------------------
# Binding
# ---------------------------------------------------------------------------

def test_listens_on_loopback_only(gateway):
    gw = gateway()
    tunnel = GatewayTunnel(BOX, 2331, local_port=0, service_port=gw.port)
    tunnel.bind()
    try:
        assert tunnel._listener.getsockname()[0] == '127.0.0.1'
    finally:
        tunnel.close()


def test_default_local_port_is_the_box_port():
    assert GatewayTunnel(BOX, 2334).local_port == 2334


def test_a_local_port_in_use_names_the_flag():
    taken = socket.socket()
    taken.bind(('127.0.0.1', 0))
    taken.listen(1)
    port = taken.getsockname()[1]
    try:
        with pytest.raises(LagerError) as info:
            GatewayTunnel(BOX, 2331, local_port=port).bind()
        assert f'Local port {port} is already in use' in info.value.problem
        assert any('--local-port' in fix for fix in info.value.fixes)
    finally:
        taken.close()


# ---------------------------------------------------------------------------
# Refusals
# ---------------------------------------------------------------------------

def denial(status, reason):
    return lambda line, headers: response(
        status, reason, {'X-Gateway-Auth-Url': AUTH_URL, 'Content-Type': 'application/json'},
        b'{}')


def test_401_asks_for_sign_in(gateway):
    gw = gateway(answer=denial(401, 'Unauthorized'))
    with pytest.raises(LagerError) as info:
        open_tunnel(BOX, 2331, service_port=gw.port)
    assert 'requires sign-in' in info.value.problem
    assert f'lager login {AUTH_URL}' in info.value.fixes
    # The discovery is recorded, like every other command's.
    assert gateway_auth.auth_server_for_box(BOX) == AUTH_URL


def test_first_contact_401_retries_once_with_a_stored_session(gateway):
    gateway_auth.save_login(AUTH_URL, make_jwt(time.time() + 900), {'r': '1'})

    def answer(line, headers):
        if 'Authorization' in headers:
            return response(200, 'Connection Established')
        return denial(401, 'Unauthorized')(line, headers)
    gw = gateway(answer=answer)
    sock, _ = open_tunnel(BOX, 2331, service_port=gw.port)
    sock.close()
    assert len(gw.requests) == 2
    assert 'Authorization' in gw.requests[1][1]


def test_403_with_discovery_header_is_no_access(gateway):
    gw = gateway(answer=denial(403, 'Forbidden'))
    with pytest.raises(LagerError) as info:
        open_tunnel(BOX, 2331, service_port=gw.port)
    assert 'not authorized to use box' in info.value.problem


def test_503_is_auth_server_down(gateway):
    gw = gateway(answer=denial(503, 'Service Unavailable'))
    with pytest.raises(LagerError) as info:
        open_tunnel(BOX, 2331, service_port=gw.port)
    assert 'auth server is unreachable' in info.value.problem


def test_403_without_discovery_header_is_a_port_it_will_not_tunnel(gateway):
    gw = gateway(answer=lambda line, headers: response(
        403, 'Forbidden', {'Content-Type': 'text/plain'}, b'port 22 is not tunnelable'))
    with pytest.raises(TunnelError) as info:
        open_tunnel(BOX, 22, service_port=gw.port)
    assert 'does not tunnel port 22' in info.value.problem
    assert info.value.cause == 'port 22 is not tunnelable'
    # Not a denial: nothing is learned about the box's auth server.
    assert gateway_auth.auth_server_for_box(BOX) is None


def test_502_says_nothing_is_listening(gateway):
    gw = gateway(answer=lambda line, headers: response(
        502, 'Bad Gateway', {'Content-Type': 'text/plain'}, b'nothing listening'))
    with pytest.raises(TunnelTargetDown) as info:
        open_tunnel(BOX, 2331, service_port=gw.port)
    assert 'Nothing is listening on port 2331' in info.value.problem


def test_a_plain_box_answering_501_is_unsupported(gateway):
    gw = gateway(answer=lambda line, headers: response(
        501, 'Unsupported method', {'Content-Type': 'text/html'}, b'<html/>'))
    with pytest.raises(gateway_tunnel.TunnelUnsupported):
        open_tunnel(BOX, 2331, service_port=gw.port)


def test_a_denial_ends_the_tunnel_with_that_error(gateway):
    gw = gateway(answer=denial(403, 'Forbidden'))
    tunnel, thread, _, errors = start_tunnel(gw)
    c = socket.create_connection(('127.0.0.1', tunnel.local_port))
    try:
        thread.join(5)
        assert not thread.is_alive()
        assert len(errors) == 1
        assert 'not authorized to use box' in errors[0].problem
        assert tunnel.closed
    finally:
        c.close()


def test_a_502_drops_that_connection_and_keeps_listening(gateway):
    gw = gateway(answer=lambda line, headers: response(
        502, 'Bad Gateway', {'Content-Type': 'text/plain'}, b''))
    tunnel, thread, notes, errors = start_tunnel(gw)
    try:
        with socket.create_connection(('127.0.0.1', tunnel.local_port)) as c:
            c.settimeout(5)
            assert c.recv(1) == b''
        assert wait_for(lambda: notes)
        assert 'Nothing is listening on port 2331' in notes[0]
        assert thread.is_alive() and not errors
    finally:
        tunnel.close()


def test_a_gateway_side_close_is_reported_once(gateway):
    gw = gateway()
    tunnel, thread, notes, _ = start_tunnel(gw, box_label='STG-1')
    try:
        c = socket.create_connection(('127.0.0.1', tunnel.local_port))
        c.sendall(b'x')
        assert recv_exactly(c, 1) == b'x'
        gw.drop_all()
        c.settimeout(5)
        assert c.recv(1) == b''
        c.close()
        assert wait_for(lambda: notes)
        time.sleep(0.3)
        assert len(notes) == 1
        assert 'Box STG-1 closed the tunnel to port 2331' in notes[0]
        assert 'revoked' in notes[0]
        # The listener stays up: a revoked user's next connect is refused
        # by the gateway with a clear 403, not by a dead local port.
        assert thread.is_alive()
    finally:
        tunnel.close()


def test_a_local_hangup_is_not_reported(gateway):
    gw = gateway()
    tunnel, _, notes, _ = start_tunnel(gw)
    try:
        with socket.create_connection(('127.0.0.1', tunnel.local_port)) as c:
            c.sendall(b'x')
            recv_exactly(c, 1)
        time.sleep(0.5)
        assert notes == []
    finally:
        tunnel.close()


def test_a_client_can_reconnect(gateway):
    gw = gateway()
    tunnel, _, _, _ = start_tunnel(gw)
    try:
        for payload in (b'first', b'again'):
            with socket.create_connection(('127.0.0.1', tunnel.local_port)) as c:
                c.sendall(payload)
                assert recv_exactly(c, 5) == payload
    finally:
        tunnel.close()


# ---------------------------------------------------------------------------
# choose_route
# ---------------------------------------------------------------------------

def test_route_tunnel_when_the_gateway_opens_one(gateway):
    gw = gateway()
    assert choose_route(BOX, 2331, service_port=gw.port) == ROUTE_TUNNEL


def test_route_tunnel_for_a_known_gated_box(gateway):
    gateway_auth.record_box_auth_server(BOX, AUTH_URL)
    gateway_auth.save_login(AUTH_URL, make_jwt(time.time() + 900), {'r': '1'})
    gw = gateway()
    assert choose_route(BOX, 2331, service_port=gw.port) == ROUTE_TUNNEL
    assert 'Authorization' in gw.requests[0][1]


def test_route_direct_on_a_plain_box_without_touching_its_debug_port(gateway, monkeypatch):
    # A plain box's own debug service answers CONNECT with 501. The GDB
    # port itself is never probed: connecting to it can halt the target.
    gw = gateway(answer=lambda line, headers: response(501, 'Unsupported method'))
    touched = []
    monkeypatch.setattr(gateway_tunnel, 'port_reachable',
                        lambda *a, **k: touched.append(a) or True)
    assert choose_route(BOX, 2331, service_port=gw.port) == ROUTE_DIRECT
    assert touched == []


def test_route_direct_for_a_gated_box_whose_port_is_reachable(gateway, monkeypatch):
    gateway_auth.record_box_auth_server(BOX, AUTH_URL)
    gw = gateway(answer=lambda line, headers: response(501, 'Unsupported method'))
    monkeypatch.setattr(gateway_tunnel, 'port_reachable', lambda *a, **k: True)
    assert choose_route(BOX, 2331, service_port=gw.port) == ROUTE_DIRECT


def test_an_old_gateway_on_a_gated_box_says_it_needs_updating(gateway, monkeypatch):
    gateway_auth.record_box_auth_server(BOX, AUTH_URL)
    gw = gateway(answer=lambda line, headers: response(501, 'Unsupported method'))
    monkeypatch.setattr(gateway_tunnel, 'port_reachable', lambda *a, **k: False)
    with pytest.raises(LagerError) as info:
        choose_route(BOX, 2331, service_port=gw.port)
    assert 'does not support debug tunnels yet' in info.value.problem


def test_route_waits_out_a_server_that_is_still_starting(gateway, monkeypatch):
    monkeypatch.setattr(gateway_tunnel.time, 'sleep', lambda s: None)
    answers = iter([502, 502, 200])

    def answer(line, headers):
        status = next(answers)
        if status == 200:
            return response(200, 'Connection Established')
        return response(502, 'Bad Gateway', {'Content-Type': 'text/plain'})
    gw = gateway(answer=answer)
    assert choose_route(BOX, 2331, service_port=gw.port) == ROUTE_TUNNEL


def test_route_gives_up_on_a_server_that_never_listens(gateway):
    gw = gateway(answer=lambda line, headers: response(
        502, 'Bad Gateway', {'Content-Type': 'text/plain'}))
    with pytest.raises(TunnelTargetDown) as info:
        choose_route(BOX, 2331, service_port=gw.port, settle_seconds=0)
    assert 'could not reach the debug server on port 2331' in info.value.problem


def _closed_port():
    s = socket.socket()
    s.bind(('127.0.0.1', 0))
    port = s.getsockname()[1]
    s.close()
    return port


def test_an_unreachable_service_port_on_a_plain_box_keeps_the_direct_route():
    assert choose_route(BOX, 2331, service_port=_closed_port()) == ROUTE_DIRECT


def test_an_unreachable_service_port_on_a_gated_box_is_reported():
    gateway_auth.record_box_auth_server(BOX, AUTH_URL)
    with pytest.raises(gateway_tunnel.TunnelUnreachable):
        choose_route(BOX, 2331, service_port=_closed_port())


def test_an_old_enforcing_gateway_is_recognised_on_first_contact(gateway, monkeypatch):
    # It denies the bare CONNECT (401 + discovery header), then forwards the
    # authenticated retry to the box's own debug service, which answers 501.
    gateway_auth.save_login(AUTH_URL, make_jwt(time.time() + 900), {'r': '1'})

    def answer(line, headers):
        if 'Authorization' in headers:
            return response(501, 'Unsupported method')
        return denial(401, 'Unauthorized')(line, headers)
    gw = gateway(answer=answer)
    monkeypatch.setattr(gateway_tunnel, 'port_reachable', lambda *a, **k: False)
    with pytest.raises(LagerError) as info:
        choose_route(BOX, 2331, service_port=gw.port)
    assert 'does not support debug tunnels yet' in info.value.problem


def test_an_unreachable_service_port_drops_one_connection_only():
    notes = []
    tunnel = GatewayTunnel(BOX, 2331, local_port=0, service_port=_closed_port(),
                           notify=notes.append)
    tunnel.bind()
    thread = threading.Thread(target=tunnel.serve_forever, daemon=True)
    thread.start()
    try:
        with socket.create_connection(('127.0.0.1', tunnel.local_port)) as c:
            c.settimeout(5)
            assert c.recv(1) == b''
        assert wait_for(lambda: notes)
        assert 'did not answer on port' in notes[0]
        assert thread.is_alive()
    finally:
        tunnel.close()
