# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

"""
WebSocket-failure diagnostic helper for the battery/supply TUIs.

The TUIs talk to box_http_server on port 9000 over a SocketIO WebSocket.
When that connection fails, the user used to see only:

    WebSocket connection failed: Failed to connect to WebSocket server

which is impossible to act on — they don't know if the box is dead, the
container is stopped, an older box-image doesn't have the WS namespace,
or what. This helper probes the box's `/health` HTTP endpoint with a
short timeout and returns a one-line actionable message based on what it
finds. Always returns a string (never raises) so it's safe to call from
the TUI's error path.
"""

from __future__ import annotations

import requests


def make_ws_failure_message(box_ip: str, original_error: str | Exception = '') -> str:
    """Return an actionable one-liner describing why the WebSocket connection
    to `box_ip` (port 9000) failed. Probes `/health` to differentiate
    box-unreachable from box-up-but-WS-missing.

    Always returns a string — never raises. The original WS error message
    is included verbatim in the output so debug info isn't lost.
    """
    base = f'WebSocket connection to {box_ip}:9000 failed'
    detail = f' ({original_error})' if original_error else ''

    try:
        # /health is a cheap, always-present endpoint on box_http_server
        # (port 9000). 2s timeout matches the worst case of a healthy box
        # under temporary load.
        r = requests.get(f'http://{box_ip}:9000/health', timeout=2.0)
        if r.status_code == 200:
            return (
                f'{base}{detail}.\n'
                f'Action: box services are up but the supply/battery session did '
                f'not start. Most often the instrument is offline, busy, or slow '
                f'to respond — check it is powered on and appears in '
                f'`lager instruments --box <name>`, and check the box-side monitor '
                f'log (`lager ssh <name>`, then `sudo docker logs lager | grep MONITOR`). '
                f'On very old boxes (pre-0.20) the WS namespace itself may be '
                f'missing — `lager update --box <name>` fixes that.'
            )
        return (
            f'{base}{detail}.\n'
            f'Action: box responded HTTP {r.status_code} on /health — services may be '
            f'partially up. Try `lager ssh <name>` and `sudo docker restart lager`, '
            f'then retry the TUI.'
        )
    except requests.exceptions.ConnectTimeout:
        return (
            f'{base}{detail}.\n'
            f'Action: timed out reaching {box_ip}:9000. Check network/Tailscale, then '
            f'`lager box hello --box <name>` to confirm box-side services are running.'
        )
    except requests.exceptions.ConnectionError:
        return (
            f'{base}{detail}.\n'
            f'Action: cannot reach {box_ip}:9000 — the lager container may not be '
            f'running. Try `lager ssh <name>` and `sudo docker start lager`, then '
            f'retry the TUI.'
        )
    except Exception as e:  # any other network error
        return (
            f'{base}{detail}.\n'
            f'Action: HTTP probe to {box_ip}:9000 errored ({e}). '
            f'Try `lager box hello --box <name>` for a fuller diagnostic.'
        )
