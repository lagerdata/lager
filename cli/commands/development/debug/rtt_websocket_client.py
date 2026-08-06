# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

"""
WebSocket client for bi-directional (interactive) RTT sessions.

Connects to the box's ``/rtt`` Socket.IO namespace on :9000 and bridges it to
the local terminal:

* Up-channel (target -> host): raw RTT bytes are written verbatim to stdout.
  stdout stays a clean binary stream on purpose, so the canonical defmt
  pipeline keeps working::

      lager debug <NET> gdbserver --rtt --interactive 2>/dev/null \\
          | defmt-print -e app.elf

* Down-channel (host -> target): stdin is forwarded raw to the target's RTT
  down buffer. The terminal is left in cooked (line-buffered) mode, so line
  editing and local echo are handled by the terminal itself — echo goes to
  the tty, never into the stdout pipe, which keeps the defmt byte stream
  uncorrupted.

All status and error messages go to stderr.
"""
import os
import sys
import threading

import socketio
import click


class RTTWebSocketClient:
    """WebSocket client for bi-directional RTT sessions."""

    def __init__(self, box_url: str, netname: str, channel: int = 0,
                 search_params: dict | None = None):
        """
        Initialize RTT WebSocket client.

        Args:
            box_url: Box WebSocket URL (e.g., http://box:9000)
            netname: Name of the debug net
            channel: RTT channel number (default 0)
            search_params: Optional RTT control-block search overrides
                (search_addr / search_size / chunk_size)
        """
        self.box_url = box_url
        self.netname = netname
        self.channel = channel
        self.search_params = search_params or {}
        self.connected = False
        self.rtt_active = False
        self.stop_event = threading.Event()

        self.sio = socketio.Client(
            logger=False,
            engineio_logger=False,
            reconnection=False
        )

        self.sio.on('connect', self._on_connect, namespace='/rtt')
        self.sio.on('disconnect', self._on_disconnect, namespace='/rtt')
        self.sio.on('rtt_connected', self._on_rtt_connected, namespace='/rtt')
        self.sio.on('rtt_data', self._on_rtt_data, namespace='/rtt')
        self.sio.on('rtt_stopped', self._on_rtt_stopped, namespace='/rtt')
        self.sio.on('error', self._on_error, namespace='/rtt')

    def _on_connect(self):
        self.connected = True

    def _on_disconnect(self):
        self.connected = False
        self.rtt_active = False
        self.stop_event.set()

    def _on_rtt_connected(self, data):
        self.rtt_active = True
        backend = data.get('backend', 'unknown')
        channel = data.get('channel', self.channel)
        click.secho(
            f"RTT attached to {self.netname} (channel {channel}, {backend}) "
            f"[interactive]",
            fg='green', err=True)
        click.secho(
            "Type to send to the target's RTT down-channel. Ctrl+C to disconnect.",
            fg='yellow', err=True)

    def _on_rtt_data(self, data):
        """Write raw up-channel bytes to stdout (binary, defmt-pipeable)."""
        try:
            hex_data = data.get('data', '')
            if hex_data:
                sys.stdout.buffer.write(bytes.fromhex(hex_data))
                sys.stdout.buffer.flush()
        except BrokenPipeError:
            # Downstream consumer (defmt-print) went away — end the session.
            self.stop_event.set()
        except Exception as e:
            click.secho(f"\nError processing RTT data: {e}", fg='red', err=True)

    def _on_rtt_stopped(self, data):
        self.rtt_active = False
        self.stop_event.set()

    def _on_error(self, data):
        message = (data or {}).get('message', 'Unknown error')
        click.secho(f"\nError: {message}", fg='red', err=True)
        self.stop_event.set()

    def _read_stdin_thread(self):
        """Forward stdin to the RTT down-channel, raw and unmodified.

        Blocking reads in a daemon thread: on session teardown the process
        exits and the thread dies with it, so no select/poll loop is needed
        (and blocking os.read keeps this working on Windows, where select on
        a pipe/tty fd is unavailable). EOF (piped stdin exhausted) just stops
        forwarding — the up-channel stream keeps running.
        """
        fd_in = sys.stdin.fileno()
        while not self.stop_event.is_set() and self.rtt_active:
            try:
                data = os.read(fd_in, 4096)
            except OSError:
                break
            if not data:
                break
            try:
                self.sio.emit('rtt_write', {'data': data.hex()}, namespace='/rtt')
            except Exception as e:
                click.secho(f"\nError sending to RTT: {e}", fg='red', err=True)
                break

    def connect_and_run(self) -> int:
        """Connect to the box and run the bi-directional session."""
        try:
            from ....gateway_auth import auth_headers_for_url, ws_handshake_recovery
            headers = auth_headers_for_url(self.box_url)
            for attempt in (0, 1):
                try:
                    self.sio.connect(
                        self.box_url,
                        namespaces=['/rtt'],
                        wait_timeout=10,
                        headers=headers
                    )
                    break
                except Exception as e:
                    # The handshake exception hides the HTTP response; probe
                    # the box over HTTP so a gated box can hand back a token
                    # for one retry and a genuine denial shows the actionable
                    # error (same recovery shape as the UART client).
                    retry_headers, denial = ws_handshake_recovery(self.box_url, headers)
                    if denial is not None:
                        denial.show()
                        return 1
                    if attempt == 0 and retry_headers:
                        headers = retry_headers
                        continue
                    from ....errors import connection_error
                    from urllib.parse import urlparse
                    host = urlparse(self.box_url).hostname or self.box_url
                    connection_error(e, host=host).show()
                    return 1

            start_payload = {
                'netname': self.netname,
                'channel': self.channel,
            }
            for key in ('search_addr', 'search_size', 'chunk_size'):
                if self.search_params.get(key) is not None:
                    start_payload[key] = self.search_params[key]
            self.sio.emit('start_rtt', start_payload, namespace='/rtt')

            # Wait for the RTT session to become active
            timeout = 15
            while not self.rtt_active and not self.stop_event.is_set() and timeout > 0:
                self.sio.sleep(0.1)
                timeout -= 0.1

            if not self.rtt_active:
                if not self.stop_event.is_set():
                    click.secho("Error: RTT session failed to start", fg='red', err=True)
                return 1

            stdin_thread = threading.Thread(target=self._read_stdin_thread, daemon=True)
            stdin_thread.start()

            while not self.stop_event.is_set():
                self.sio.sleep(0.1)

            if self.rtt_active:
                self.sio.emit('stop_rtt', namespace='/rtt')
                self.sio.sleep(0.5)

            return 0

        except KeyboardInterrupt:
            click.secho('\nDisconnected', fg='red', err=True)
            if self.rtt_active:
                try:
                    self.sio.emit('stop_rtt', namespace='/rtt')
                    self.sio.sleep(0.3)
                except Exception:
                    pass
            return 0
        except Exception as e:
            click.secho(f"\nError: {str(e)}", fg='red', err=True)
            return 1
        finally:
            if self.connected:
                try:
                    self.sio.disconnect()
                except Exception:
                    pass


def connect_rtt_interactive(box_url: str, netname: str, channel: int = 0,
                            search_params: dict | None = None) -> int:
    """
    Run a bi-directional RTT session against the box's /rtt namespace.

    Args:
        box_url: Box WebSocket URL (e.g., http://box:9000)
        netname: Name of the debug net
        channel: RTT channel number
        search_params: Optional RTT control-block search overrides

    Returns:
        Exit code (0 for success, non-zero for error)
    """
    client = RTTWebSocketClient(box_url, netname, channel=channel,
                                search_params=search_params)
    return client.connect_and_run()
