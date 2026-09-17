# Scope daemon: manual clients

Operator-driven checks against a running `oscilloscope-daemon`. No workflow runs these.

The daemon serves WebSocket commands on **8085**. Ports 8082 to 8084 are WebTransport over
QUIC, which `websocat` cannot speak. None of the four is published by `start_box.sh` or opened
in the box firewall, so connect from the host that runs the daemon.

## WebSocket commands with websocat

```bash
brew install websocat
websocat ws://localhost:8085
```

Then type one JSON object per line. A command the daemon cannot parse is logged and gets no
answer, so a silent prompt means the JSON was wrong.

```
{"command": "GetSampleRate"}
{"command": "SetTriggerLevel", "trigger_level": 2.0}
{"command": "GetTriggerLevel"}

{"command": "SetVoltsPerDiv", "channel": {"Alphabetic": "A"}, "volts_per_div": 2.0}
{"command": "GetVoltsPerDiv", "channel": {"Alphabetic": "A"}}

{"command": "EnableChannel", "channel": {"Alphabetic": "A"}}
{"command": "DisableChannel", "channel": {"Alphabetic": "A"}}
{"command": "IsChannelEnabled", "channel": {"Alphabetic": "A"}}

{"command": "EnableChannel", "channel": {"Alphabetic": "B"}}
{"command": "DisableChannel", "channel": {"Alphabetic": "B"}}
{"command": "IsChannelEnabled", "channel": {"Alphabetic": "B"}}

{"command": "SetTimePerDiv", "time_per_div": 0.005}
{"command": "GetTimePerDiv"}
```

`SetTimeOffset`, `SetVoltsOffset` and their getters answer "Unsupported command" here. Only
the WebTransport path handles them.

## WebTransport client: `web_oscilloscope_wt.html`

A browser client for the WebTransport endpoints: 8082 for commands, 8083 for data. Open it in
Chrome, and pass `?host=`, `?commandsPort=` or `?browserPort=` to point it elsewhere.

It pins the certificate by hash, and the hash in the file matches a developer certificate that
is not in the repository (`.gitignore` excludes `certs/`). A new certificate needs its SHA-256
written into the file, next to the comment that says so.

The shipped WebSocket UI is `box/lager/docker/web_oscilloscope.html`, which the box serves and
`lager scope stream web` opens. This page is the WebTransport counterpart, and it ships
nowhere.

<!-- Copyright 2024-2026 Lager Data -->
<!-- SPDX-License-Identifier: Apache-2.0 -->
