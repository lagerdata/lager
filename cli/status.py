# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

"""
    lager.status

    Job status output functions
"""
try:
    _TERMIOS_IMPORT_FAILED = False
    import termios
    import tty
    import pty
except (ImportError, ModuleNotFoundError) as exc:
    _TERMIOS_IMPORT_FAILED = exc
import platform
import signal
import threading
import sys
import os
import select
from functools import partial
from bson import decode as bson_loads
import click
import trio
import trio_websocket
from trio_websocket import open_websocket_url
from tenacity import retry, stop_after_attempt, wait_fixed, retry_if_exception_type
import wsproto.frame_protocol as wsframeproto
import requests
from .core.matchers import test_matcher_factory
from .core.utils import heartbeat
from .simple_hdlc import HDLC

def stream_response(response):
    """
        Stream a ``requests`` response to the terminal
    """
    with response:
        for chunk in response.iter_content(chunk_size=None):
            click.echo(chunk, nl=False)

async def handle_url_message(matcher, urls):
    """
        Handle a message with data location urls
    """
    downloader = partial(requests.get, stream=True)
    for url in urls:
        response = await trio.to_thread.run_sync(downloader, url)
        response.raise_for_status()
        await trio.to_thread.run_sync(stream_response, response)

async def handle_data_message(matcher, message):
    """
        Handle a data message
    """
    for item in message:
        entry = item['entry']
        if 'payload' in entry:
            payload = entry['payload']
            matcher.feed(payload)

async def handle_message(matcher, message):
    """
        Handle an individual parsed websocket message
    """
    if 'data' in message:
        return await handle_data_message(matcher, message['data'])
    if 'urls' in message:
        return await handle_url_message(matcher, message['urls'])
    return None

class InterMessageTimeout(Exception):
    """
        Raised if no messages received for ``message_timeout`` seconds
    """
    pass

async def read_from_websocket(websocket, matcher, message_timeout, nursery):
    try:
        while True:
            try:
                with trio.fail_after(message_timeout):
                    try:
                        message = await websocket.get_message()
                    except trio_websocket.ConnectionClosed as exc:
                        if exc.reason is None:
                            return
                        if exc.reason.code != wsframeproto.CloseReason.NORMAL_CLOSURE or exc.reason.reason != 'EOF':
                            raise
                        break
            except trio.TooSlowError:
                raise InterMessageTimeout(message_timeout)
            await handle_message(matcher, bson_loads(message))
    finally:
        matcher.done()
        nursery.cancel_scope.cancel()

def reader_function(io, send_channel, trio_token):
    try:
        while True:
            data = io.read()
            if data is not None:
                trio.from_thread.run(send_channel.send, {'type': 'data', 'value': data}, trio_token=trio_token)
    except EOFError:
        trio.from_thread.run(send_channel.send, {'type': 'EOF'}, trio_token=trio_token)
    except KeyboardInterrupt:
        trio.from_thread.run(send_channel.send, {'type': 'Ctrl+C'}, trio_token=trio_token)


async def write_to_websocket(websocket, receive_channel, eof_timeout, nursery):
    while True:
        message = await receive_channel.receive()
        if message['type'] == 'EOF':
            if eof_timeout is not None:
                await trio.sleep(eof_timeout)
                await websocket.aclose()
            return

        if message['type'] == 'Ctrl+C':
            raise KeyboardInterrupt

        if message['type'] == 'data':
            data = message['value']
            try:
                await websocket.send_message(data)
            except trio_websocket.ConnectionClosed as exc:
                if exc.reason is None:
                    return
                if exc.reason.code != wsframeproto.CloseReason.NORMAL_CLOSURE or exc.reason.reason != 'EOF':
                    raise
                return

class TTYIO:
    """
        Class for doing I/O with a TTY managed by curses
    """
    def __init__(self, line_ending=b'', opost=False, is_fixture=False, serial_channel=0, raw=True):
        self.stdin_fileno = sys.stdin.fileno()
        self.stdout_fileno = sys.stdout.fileno()
        if raw:
            self.old_settings = termios.tcgetattr(self.stdin_fileno)
            tty.setraw(self.stdin_fileno)
        else:
            self.old_settings = None
        self.line_ending = line_ending
        self._opost = opost
        self._is_fixture = is_fixture
        self._serial_channel = serial_channel
        self.hdlc = HDLC(False)

    def shutdown(self):
        """
            Restore previous TTY settings
        """
        self.restore()

    def restore(self):
        if self.old_settings:
            termios.tcsetattr(self.stdin_fileno, termios.TCSADRAIN, self.old_settings)

    def output(self, data, fg=None, flush=False):
        if self._opost:
            self.restore()
            if fg:
                click.secho(data, nl=False, fg=fg)
            else:
                click.echo(data, nl=False)
            if flush:
                sys.stdout.flush()

            tty.setraw(self.stdin_fileno)
        else:
            os.write(self.stdout_fileno, data)

    def read(self):
        char = os.read(self.stdin_fileno, 1)
        if char == b'':
            raise EOFError
        if char == b'\x03':
            raise KeyboardInterrupt

        if self._is_fixture:

            UART_TX = 0x6A
            output = bytearray()
            output.append(UART_TX)
            output.append(self._serial_channel)
            output.append(1)
            output.extend(char)
            frame = self.hdlc.sendFrame(output)

            os.write(self.stdout_fileno, char)
            if char == b'\r':
                os.write(self.stdout_fileno, self.line_ending)
            sys.stdout.flush()

            return frame

        if char == b'\r':
            return self.line_ending or char

        return char

class PTTYIO:
    """
        Class for doing I/O with a TTY managed by curses
    """
    def __init__(self, line_ending=b'', opost=False, is_fixture=False):
        self.stdin_fileno = sys.stdin.fileno()
        self.stdout_fileno = sys.stdout.fileno()
        self.line_ending = line_ending
        self.create_ptty()
        self.stdin_poll = select.poll()
        self.stdin_poll.register(self.stdin_fileno, select.POLLIN)
        self.ttypoll = select.poll()
        self.ttypoll.register(self.primary, select.POLLIN)

    def create_ptty(self):
        self.primary, self.secondary = pty.openpty()
        self.s_name = os.ttyname(self.secondary)
        click.echo(f"Opening a new tty device at {self.s_name}", nl=False)

    def shutdown(self):
        os.close(self.primary)
        os.close(self.secondary)

    def output(self, data, fg=None, flush=False):
        os.write(self.stdout_fileno, data)

    def read(self):
        if self.stdin_poll.poll(0):
            key_char = os.read(self.stdin_fileno, 1)
            if key_char == b'\x03':
                raise KeyboardInterrupt

        if self.ttypoll.poll(0):
            char = os.read(self.primary, 1)
            if char == b'':
                raise EOFError
            return char

class StandardIO:
    """
        Class for doing I/O with standard UNIX io streams
    """
    def shutdown(self):
        pass

    def output(self, data, fg=None, flush=False):
        if fg:
            click.secho(data, nl=False, fg=fg)
        else:
            click.echo(data, nl=False)
        if flush:
            sys.stdout.flush()

    def read(self):
        rlist, _wlist, xlist = select.select([sys.stdin], [], [sys.stdin])
        if rlist:
            chunk = sys.stdin.buffer.read()
            if chunk == b'':
                raise EOFError
            return chunk
        if xlist:
            raise EOFError
        return None

def sigint_handler(send_channel, signal, frame):
    send_channel.send({'type': 'Ctrl+C'})

async def handle_sigint(send_channel):
    with trio.open_signal_receiver(signal.SIGINT) as signal_aiter:
        async for signum in signal_aiter:
            await send_channel.send({'type': 'data', 'value': b'\x03'})

@retry(reraise=True, sleep=trio.sleep, stop=stop_after_attempt(4), wait=wait_fixed(2), retry=retry_if_exception_type(trio_websocket.ConnectionRejected))
async def display_job_output(connection_params, test_runner, interactive, line_ending, message_timeout, overall_timeout, eof_timeout, success_regex=None, failure_regex=None, opost=False, serial_channel=0, ptty=False, catch_sigint=False):
    """
        Display job output from websocket
    """
    (uri, kwargs) = connection_params
    match_class = test_matcher_factory(test_runner)
    is_fixture = test_runner == 'fixture'

    if ptty:
        io_source = PTTYIO(line_ending, opost, is_fixture)
    elif interactive:
        io_source = TTYIO(line_ending, opost, is_fixture, serial_channel, raw=interactive=='raw')
    else:
        io_source = StandardIO()

    try:
        matcher = match_class(io_source, success_regex, failure_regex)
        send_channel, receive_channel = trio.open_memory_channel(0)
        allow_reader = True
        if platform.system() == 'Windows' and not interactive:
            allow_reader = False
        with trio.fail_after(overall_timeout):
            async with open_websocket_url(uri, disconnect_timeout=1, **kwargs) as websocket:
                if allow_reader:
                    token = trio.lowlevel.current_trio_token()
                    thread = threading.Thread(target=reader_function, args=(io_source, send_channel, token), daemon=True)
                    thread.start()
                async with trio.open_nursery() as nursery:
                    nursery.start_soon(heartbeat, websocket, 30, 30)
                    nursery.start_soon(read_from_websocket, websocket, matcher, message_timeout, nursery)
                    nursery.start_soon(write_to_websocket, websocket, receive_channel, eof_timeout, nursery)
                    if catch_sigint:
                        nursery.start_soon(handle_sigint, send_channel)

        return matcher
    finally:
        if io_source:
            io_source.shutdown()

def run_job_output(connection_params, test_runner, interactive, line_ending, message_timeout,
        overall_timeout, eof_timeout, debug=False, success_regex=None, failure_regex=None,
        opost=False, serial_channel=0, ptty=False, catch_sigint=False, disconnect=None):
    """
        Run async task to get job output from websocket
    """
    if interactive and _TERMIOS_IMPORT_FAILED:
        click.echo(_TERMIOS_IMPORT_FAILED, err=True)
        click.echo('Interactive terminal not currently supported by Windows; please try running in Docker.', err=True)
        click.get_current_context().exit(1)

    if not line_ending:
        line_ending = b''
    elif line_ending == 'LF':
        line_ending = b'\n'
    elif line_ending == 'CRLF':
        line_ending = b'\r\n'
    elif line_ending == 'CR':
        line_ending = b'\r'
    else:
        raise ValueError('Invalid line ending')

    try:
        matcher = trio.run(display_job_output, connection_params, test_runner, interactive, line_ending, message_timeout, overall_timeout, eof_timeout, success_regex, failure_regex, opost, serial_channel, ptty, catch_sigint)
        click.get_current_context().exit(matcher.exit_code)
    except trio.TooSlowError:
        suffix = '' if overall_timeout == 1 else 's'
        message = f'Job status timed out after {overall_timeout} second{suffix}'
        click.secho(message, fg='red', err=True)
        click.get_current_context().exit(1)
    except InterMessageTimeout:
        suffix = '' if message_timeout == 1 else 's'
        message = f'Timed out after no messages received for {message_timeout} second{suffix}'
        click.secho(message, fg='red', err=True)
        click.get_current_context().exit(1)
    except requests.exceptions.HTTPError as exc:
        response = getattr(exc, 'response')
        if response is not None and response.status_code == 404:
            click.secho('Test run content not found', fg='red', err=True)
        else:
            click.secho('Error retrieving test run content', fg='red', err=True)
        if debug:
            raise
    except trio_websocket.ConnectionRejected as exc:
        if exc.status_code == 404:
            click.secho('Job not found', fg='red', err=True)
        elif exc.status_code >= 500:
            click.secho('Internal server error. If this persists, report at https://github.com/lagerdata/lager/issues',
                        fg='red', err=True)
        else:
            click.secho('Could not connect to box websocket', fg='red', err=True)
        if debug:
            raise
        click.get_current_context().exit(1)
    except trio_websocket.HandshakeError as exc:
        click.secho('Could not connect to box websocket', fg='red', err=True)
        if debug:
            raise
        click.get_current_context().exit(1)
    except trio_websocket.ConnectionClosed as exc:
        if exc.reason.code != wsframeproto.CloseReason.NORMAL_CLOSURE:
            click.secho('Box websocket closed abnormally', fg='red', err=True)
            if debug:
                raise
            click.get_current_context().exit(1)
        elif exc.reason.reason != 'EOF':
            click.secho('Box websocket closed unexpectedly', fg='red', err=True)
            if debug:
                raise
            click.get_current_context().exit(1)
    except ConnectionRefusedError:
        click.secho('Box websocket connection refused', fg='red', err=True)
        if debug:
            raise
        click.get_current_context().exit(1)
    except BaseExceptionGroup as exc:
        for sub_exc in exc.exceptions:
            if isinstance(sub_exc, BaseExceptionGroup):
                for sub_sub_exc in sub_exc.exceptions:
                    if isinstance(sub_sub_exc, KeyboardInterrupt):
                        return
            elif isinstance(sub_exc, KeyboardInterrupt):
                return
        raise

    finally:
        if disconnect:
            session = click.get_current_context().obj.session
            session.stop_debugger(disconnect)
