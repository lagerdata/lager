#!/usr/bin/env python3
# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0
"""What a browser on the other end of a real network receives from the scope.

Takes the path the web UI takes -- a ticket from ``GET /scope/<net>/stream``,
then the relay WebSocket it names, through whatever gateway fronts the box --
and reports what arrives: frames per second, the gaps between them, captures
skipped, bandwidth, and how much delay builds up in queues along the way.

Queueing delay is read from the frames themselves. Each carries the box's
monotonic time at capture; the box's clock and this one differ by an unknown
offset, but the offset is constant, so ``arrival - capture`` minus its
smallest value over the run is the delay each frame spent queued somewhere
beyond the best case. A stream that is keeping up holds that near zero; one
that is buffering grows it without bound.

``--credits N`` acts as the web UI does: subscribes with N credits and returns
them at most 60 times a second, as a display drawing on each animation frame
would. Without it, it subscribes the way a client that predates flow control
does, and is sent every capture.

Run from a laptop, against a box, with a `lager login` session if the box is
gated:

    python3 relay_probe.py --box 100.75.209.99 --net picoscope1 --duration 20
    python3 relay_probe.py --box 100.75.209.99 --net picoscope1 --credits 3
"""

import argparse
import asyncio
import json
import statistics
import struct
import sys
import time

try:
    import requests
    import websockets
except ImportError:
    sys.exit("requires requests and websockets: pip install requests websockets")

LSCP_HEADER = "<IHHQQdIIIBBH"
FLAG_TRIGGERED = 1 << 0
DISPLAY_INTERVAL = 1 / 60


def auth_headers(box):
    """The CLI's bearer token for a gated box, or nothing for a plain one."""
    try:
        from cli.gateway_auth import auth_headers_for_box
    except ImportError:
        return {}
    return auth_headers_for_box(box) or {}


def net_command(box, headers, net, action, **params):
    response = requests.post(
        "http://%s:9000/net/command" % box,
        json={"netname": net, "action": action, "params": params},
        headers=headers,
        timeout=30,
    )
    body = response.json() if response.content else {}
    if not response.ok:
        raise SystemExit("%s %s failed: %s" % (net, action, body.get("error") or response.status_code))
    return body


def percentile(values, fraction):
    if not values:
        return float("nan")
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, int(fraction * len(ordered)))]


async def probe(args, headers):
    ticket = requests.get(
        "http://%s:9000/scope/%s/stream" % (args.box, args.net), headers=headers, timeout=15
    ).json()
    if not ticket.get("success"):
        raise SystemExit("ticket refused: %s" % ticket.get("error"))
    url = "ws://%s:9000%s" % (args.box, ticket["ws_path"])

    arrivals, captures, seqs, sizes, triggered = [], [], [], [], 0
    async with websockets.connect(
        url, additional_headers=headers, max_size=None, compression=None
    ) as ws:
        subscribe = {"command": "Subscribe"}
        if args.credits:
            subscribe["credits"] = args.credits
        if args.max_fps:
            subscribe["max_fps"] = args.max_fps
        await ws.send(json.dumps(subscribe))

        owed = 0
        next_draw = time.monotonic()
        deadline = time.monotonic() + args.duration
        while time.monotonic() < deadline:
            timeout = max(0.0, min(deadline, next_draw) - time.monotonic()) if owed else deadline - time.monotonic()
            try:
                message = await asyncio.wait_for(ws.recv(), timeout=max(timeout, 0.001))
            except asyncio.TimeoutError:
                message = None
            now = time.monotonic()
            if isinstance(message, (bytes, bytearray)):
                header = struct.unpack_from(LSCP_HEADER, message, 0)
                _, _, flags, seq, capture_ns = header[:5]
                arrivals.append(now)
                captures.append(capture_ns / 1e9)
                seqs.append(seq)
                sizes.append(len(message))
                triggered += bool(flags & FLAG_TRIGGERED)
                owed += 1
            if args.credits and owed and now >= next_draw:
                await ws.send(json.dumps({"command": "Credit", "count": owed}))
                owed = 0
                next_draw = now + DISPLAY_INTERVAL

    if len(arrivals) < 2:
        raise SystemExit("received %d frames" % len(arrivals))
    span = arrivals[-1] - arrivals[0]
    gaps = [(b - a) * 1000 for a, b in zip(arrivals, arrivals[1:])]
    skipped = sum(max(0, b - a - 1) for a, b in zip(seqs, seqs[1:]))
    offsets = [a - c for a, c in zip(arrivals, captures)]
    base = min(offsets)
    queued = [(o - base) * 1000 for o in offsets]
    return {
        "mode": ("credits=%d" % args.credits if args.credits else "every capture")
                + (" max_fps=%g" % args.max_fps if args.max_fps else ""),
        "frames": len(arrivals),
        "fps": (len(arrivals) - 1) / span,
        "gap_ms_p50": percentile(gaps, 0.50),
        "gap_ms_p95": percentile(gaps, 0.95),
        "gap_ms_p99": percentile(gaps, 0.99),
        "gap_ms_max": max(gaps),
        "captures_skipped": skipped,
        "kbytes_per_s": sum(sizes) / span / 1024,
        "queued_ms_p50": percentile(queued, 0.50),
        "queued_ms_p95": percentile(queued, 0.95),
        "queued_ms_max": max(queued),
        "queued_ms_last": queued[-1],
        "triggered_pct": 100.0 * triggered / len(arrivals),
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--box", required=True)
    parser.add_argument("--net", required=True, help="the scope net")
    parser.add_argument("--duration", type=float, default=15.0)
    parser.add_argument("--credits", type=int, default=0)
    parser.add_argument("--max-fps", type=float, default=0.0)
    parser.add_argument("--setup", action="store_true",
                        help="enable channel A, 1 V trigger, and run, before measuring")
    parser.add_argument("--channel-net", default=None, help="channel net for --setup")
    parser.add_argument("--timebase", type=float, default=None, help="seconds/div for --setup")
    parser.add_argument("--mode", default=None, help="trigger mode for --setup")
    args = parser.parse_args()

    headers = auth_headers(args.box)
    if args.setup:
        if args.channel_net:
            net_command(args.box, headers, args.channel_net, "enable_net")
        if args.timebase:
            net_command(args.box, headers, args.net, "set_timebase", seconds_per_div=args.timebase)
        trigger = {"level": 1.0}
        if args.mode:
            trigger["mode"] = args.mode
        net_command(args.box, headers, args.net, "trigger_edge", **trigger)
        net_command(args.box, headers, args.net, "start_capture")
        time.sleep(1.0)

    result = asyncio.run(probe(args, headers))
    width = max(len(k) for k in result)
    for key, value in result.items():
        print("%s  %s" % (key.ljust(width), "%.1f" % value if isinstance(value, float) else value))


if __name__ == "__main__":
    main()
