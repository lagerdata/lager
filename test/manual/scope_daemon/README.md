# Scope daemon: driving it by hand

Operator-driven checks against a running `oscilloscope-daemon`. No workflow runs these.

The daemon listens on `127.0.0.1:8085` and on the Unix socket `/tmp/lager-scope.sock`, both
inside the `lager` container. Neither is published: clients off the box reach the daemon
through the box HTTP server's relay on port 9000 (`GET /scope/<net>/stream`, then the
WebSocket it names). To talk to the daemon directly, run the client inside the container.

## Commands

Commands and replies are JSON text frames, and every command gets a reply, including one
the daemon cannot parse. The box's own client sends one and prints the reply:

```bash
lager ssh --box <box>
docker exec lager python3 -c "
from lager.measurement.scope import daemon_client as d
print(d.command('GetCapabilities'))
print(d.command('SetTimePerDiv', time_per_div=0.001))
print(d.command('Measure', channel={'Alphabetic': 'A'}))
"
```

The JSON on the wire is the command name under `command`, with its parameters beside it:

```
{"command": "EnableChannel", "channel": {"Alphabetic": "A"}}
{"command": "SetVoltsPerDiv", "channel": {"Alphabetic": "A"}, "volts_per_div": 1.0}
{"command": "SetTriggerLevel", "trigger_level": 1.0}
{"command": "SetCaptureMode", "capture_mode": "normal"}
{"command": "StartAcquisition", "trigger_position_percent": 50.0}
{"command": "StopAcquisition"}
```

## Captures

Captures are binary LSCP frames (see `box/oscilloscope-daemon/protocol/src/lscp.rs`), sent
only to a connection that has sent `{"command": "Subscribe"}`.
`box/oscilloscope-daemon/tests/bench/stream_bench.py` subscribes, and reports the capture
rate and capture-to-client latency:

```bash
docker exec lager python3 /app/stream_bench.py --duration 20
```

Copy the script into the container first (`docker cp`); the image does not ship it.

<!-- Copyright 2024-2026 Lager Data -->
<!-- SPDX-License-Identifier: Apache-2.0 -->
