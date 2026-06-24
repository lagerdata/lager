# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

"""
UART HTTP endpoint handlers.

This module contains all the HTTP endpoint logic for UART operations.
"""
from __future__ import annotations

import json
import time
from typing import Generator

import serial

from flask import Response, stream_with_context, Request, jsonify

from .dispatcher import _resolve_net_and_driver, UARTBackendError


def handle_uart_stream(request: Request) -> Response:
    """
    Handle /uart/net/stream endpoint.

    Request body:
    {
        "netname": "uart_net",
        "overrides": {...},
        "interactive": false
    }

    Returns:
        Flask Response with streaming UART data or error
    """
    try:
        data = request.json
        netname = data.get('netname')
        overrides = data.get('overrides', {})
        interactive = data.get('interactive', False)

        if not netname:
            return jsonify({'error': 'netname is required'}), 400

        # Delegate to streaming function
        return stream_uart_net(netname, overrides, interactive)

    except Exception as e:
        return jsonify({'error': f'Internal error: {str(e)}'}), 500


def stream_uart_net(netname: str, overrides: dict, interactive: bool) -> Response:
    """
    Stream UART data via HTTP using chunked transfer encoding.

    Args:
        netname: Name of the UART net
        overrides: Parameter overrides (baudrate, parity, etc.)
        interactive: Whether to enable interactive (bidirectional) mode

    Returns:
        Flask Response with streaming UART data
    """
    if interactive:
        # Interactive mode requires bidirectional communication
        # For now, return an error - this needs WebSocket or separate write endpoint
        return Response(
            json.dumps({'error': 'Interactive mode not yet supported via HTTP endpoint'}),
            status=400,
            mimetype='application/json'
        )

    try:
        # Resolve the net and create driver
        driver = _resolve_net_and_driver(netname, overrides)

        # Connect to the serial port
        driver._connect()

        # Create a generator that yields UART data
        def generate() -> Generator[bytes, None, None]:
            """Generator that yields UART data chunks."""
            connection_active = True
            try:
                # Send connection message
                msg = f"\033[32mConnected to {driver.device_path} at {driver.baudrate} baud [read-only]\033[0m\r\n"
                yield msg.encode()
                msg = "\033[33mPress Ctrl+C to disconnect\033[0m\r\n\n"
                yield msg.encode()

                # Stream data from serial port
                # Limit chunk size to prevent memory issues with high-speed devices
                MAX_CHUNK_SIZE = 4096

                while connection_active:
                    try:
                        # Read available data (non-blocking with timeout). The
                        # serial read is wrapped separately so a tty renumber /
                        # transient I/O error triggers a bounded reopen instead
                        # of being mistaken for a client disconnect.
                        try:
                            waiting = driver.serial_conn.in_waiting
                            if waiting > 0:
                                read_size = min(waiting, MAX_CHUNK_SIZE)
                                data = driver.serial_conn.read(read_size)
                            else:
                                # Read with timeout to avoid tight loop
                                # (gevent-friendly, prevents CPU spinning)
                                data = driver.serial_conn.read(1)
                        except (serial.SerialException, OSError):
                            if driver._reopen_with_backoff():
                                yield b"\r\n\033[33mReconnected\033[0m\r\n"
                                continue
                            connection_active = False
                            break
                        if data:
                            # Apply output post-processing if enabled
                            if driver.opost:
                                data = data.replace(b'\n', b'\r\n')
                            yield data
                    except (BrokenPipeError, ConnectionResetError):
                        # Client disconnected abruptly
                        connection_active = False
                        break
                    except Exception as e:
                        # Log other exceptions but don't crash
                        import logging
                        logging.getLogger(__name__).error(f"Error in UART stream: {e}")
                        connection_active = False
                        break

            except GeneratorExit:
                # Client disconnected gracefully
                connection_active = False
            except (BrokenPipeError, ConnectionResetError, OSError):
                # Client disconnected abruptly
                connection_active = False
            except Exception as e:
                # Catch any other exception to prevent server crash
                import logging
                logging.getLogger(__name__).error(f"Unexpected error in UART generator: {e}")
                connection_active = False
            finally:
                # Cleanup serial connection
                try:
                    driver._cleanup()
                except Exception as e:
                    import logging
                    logging.getLogger(__name__).error(f"Error during cleanup: {e}")
                # Only yield disconnect message if we're not in GeneratorExit
                if connection_active:
                    try:
                        yield b"\r\n\033[31mDisconnected\033[0m\r\n"
                    except (GeneratorExit, BrokenPipeError, ConnectionResetError, OSError):
                        pass

        # Return streaming response
        return Response(
            stream_with_context(generate()),
            mimetype='application/octet-stream',
            headers={
                'X-Accel-Buffering': 'no',  # Disable nginx buffering
                'Cache-Control': 'no-cache',
            }
        )

    except UARTBackendError as e:
        return Response(
            json.dumps({'error': str(e)}),
            status=400,
            mimetype='application/json'
        )
    except FileNotFoundError as e:
        return Response(
            json.dumps({'error': f'UART device not found: {str(e)}'}),
            status=404,
            mimetype='application/json'
        )
    except Exception as e:
        return Response(
            json.dumps({'error': f'Internal error: {str(e)}'}),
            status=500,
            mimetype='application/json'
        )


def stream_uart_interactive(netname: str, overrides: dict) -> Response:
    """
    Stream UART data with bidirectional (interactive) support.

    This would require WebSocket support or a separate write endpoint.
    For now, this is a placeholder for future implementation.

    Args:
        netname: Name of the UART net
        overrides: Parameter overrides

    Returns:
        Flask Response (currently error response)
    """
    return Response(
        json.dumps({
            'error': 'Interactive mode requires WebSocket support',
            'suggestion': 'Use non-interactive mode or implement WebSocket endpoint'
        }),
        status=501,
        mimetype='application/json'
    )
