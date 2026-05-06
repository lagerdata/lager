# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

"""
    lager.debug.tunnel

    Python debugger (pdb) tunnel functions.

    This enables remote debugging of Python scripts running on the box.
    Note: GDB tunneling has been removed - GDB debugging uses direct HTTP.
"""
import functools
import sys
import logging
import click
import trio
import trio_websocket
from ....core.utils import heartbeat
import errno

logger = logging.getLogger(__name__)


async def send_to_websocket(websocket, client_stream, nursery):
    """Read data from client stream and send to websocket."""
    try:
        async with client_stream:
            async for msg in client_stream:
                await websocket.send_message(msg)
    except trio.BrokenResourceError:
        pass
    finally:
        nursery.cancel_scope.cancel()


async def send_to_client(websocket, client_stream, nursery):
    """Read data from websocket and send to client stream."""
    while True:
        try:
            msg = await websocket.get_message()
            await client_stream.send_all(msg)
        except trio_websocket.ConnectionClosed:
            nursery.cancel_scope.cancel()


async def connection_handler(connection_params, client_stream, debug=True):
    """Handle a single connection from a client (Python debugger)."""
    (uri, kwargs) = connection_params
    sockname = client_stream.socket.getsockname()
    if debug:
        click.echo(f'Serving client: {sockname}')
    try:
        async with trio_websocket.open_websocket_url(uri, disconnect_timeout=1, **kwargs) as websocket:
            async with trio.open_nursery() as nursery:
                nursery.start_soon(send_to_websocket, websocket, client_stream, nursery)
                nursery.start_soon(send_to_client, websocket, client_stream, nursery)
                nursery.start_soon(heartbeat, websocket, 30, 30)
    except trio_websocket.ConnectionRejected as exc:
        click.secho('Connection to Lager Box rejected. Is the box '\
                    'powered on and have a solid internet connection?', fg='red', err=True)
        if debug:
            logger.exception('Exception in connection_handler', exc_info=exc)
    except trio.ClosedResourceError:
        click.secho('Tunnel connection closed', fg='yellow', err=True)
    except Exception as exc:  # pylint: disable=broad-except
        logger.exception('Exception in connection_handler', exc_info=exc)
    finally:
        if debug:
            click.echo(f'client disconnected: {sockname}')


async def serve_tunnel(host, port, connection_params, name, debug=True, rtt=None, rtt_port=None, print_output=None, *, task_status=trio.TASK_STATUS_IGNORED):
    """
    Start server that tunnels traffic to Python debugger (pdb) on box.

    This is used by `lager python` commands to enable remote debugging.
    """
    async with trio.open_nursery() as nursery:
        handler = functools.partial(connection_handler, connection_params, debug=debug)
        serve_listeners = functools.partial(trio.serve_tcp, handler, port, host=host)

        try:
            server = await nursery.start(serve_listeners)
        except OSError as exc:
            if port == 5555 and exc.errno == errno.EADDRINUSE:
                return
            raise
        task_status.started(server)

        if name and debug:
            click.echo(f'Serving {name} on {host}:{port}. Press Ctrl+C to quit.')
        try:
            await trio.sleep_forever()
        except KeyboardInterrupt:
            nursery.cancel_scope.cancel()
