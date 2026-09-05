#!/usr/bin/env python3
# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0
"""Throughput and latency harness for the oscilloscope daemon.

Speaks both wire formats so before/after numbers are directly comparable:

  legacy  text frames, ``{"TriggeredData": {"samples": [{channel, voltage,
          sample_index}, ...]}}``. Carries no daemon-side timestamp, so
          capture-to-client latency cannot be measured on this path.
  lscp    binary frames (see protocol::lscp). Carries ``capture_mono_ns``,
          so end-to-end latency is measured rather than inferred.

The format is detected per frame, so a daemon serving either one works
without a flag.

Run it from inside the box container, where the daemon socket lives:

    docker exec lager python3 stream_bench.py --duration 20
"""

import argparse
import asyncio
import json
import statistics
import struct
import sys
import time

try:
    import websockets
except ImportError:
    sys.exit("websockets is required: pip install websockets")


LSCP_MAGIC = 0x5043534C  # "LSCP" little-endian
LSCP_HEADER = "<IHHQQdIIIBBH"
# The header is padded out to 64 bytes; the struct above covers its first 48.
LSCP_HEADER_SIZE = 64
LSCP_CHANNEL = "<BBBBffI"
LSCP_CHANNEL_SIZE = struct.calcsize(LSCP_CHANNEL)


def percentile(values, pct):
    """Nearest-rank percentile. Avoids a numpy dependency in the container."""
    if not values:
        return float("nan")
    ordered = sorted(values)
    index = min(len(ordered) - 1, int(round(pct / 100.0 * len(ordered) + 0.5)) - 1)
    return ordered[max(0, index)]


def human_bytes(n):
    for unit in ("B", "KB", "MB", "GB"):
        if abs(n) < 1024.0:
            return f"{n:.1f} {unit}"
        n /= 1024.0
    return f"{n:.1f} TB"


class Capture:
    """Normalized view of one capture, whichever wire format delivered it."""

    __slots__ = ("wire_bytes", "sample_count", "channel_count", "latency_ms")

    def __init__(self, wire_bytes, sample_count, channel_count, latency_ms):
        self.wire_bytes = wire_bytes
        self.sample_count = sample_count
        self.channel_count = channel_count
        self.latency_ms = latency_ms


def decode_legacy(text):
    payload = json.loads(text)
    capture = payload.get("TriggeredData") or payload.get("triggered_data")
    if capture is None:
        return None
    samples = capture.get("samples", [])
    channels = {
        json.dumps(s.get("channel"), sort_keys=True) for s in samples[:4096]
    }
    return Capture(
        wire_bytes=len(text),
        sample_count=len(samples),
        channel_count=max(1, len(channels)),
        # No daemon timestamp exists in this format.
        latency_ms=None,
    )


def decode_lscp(buf, host_mono_ns):
    if len(buf) < LSCP_HEADER_SIZE:
        return None
    (
        magic,
        _version,
        _flags,
        _seq,
        capture_mono_ns,
        _interval_ns,
        _pre,
        _post,
        samples_per_channel,
        channel_count,
        _resolution_bits,
        _overflow,
    ) = struct.unpack_from(LSCP_HEADER, buf, 0)
    if magic != LSCP_MAGIC:
        return None
    latency_ms = None
    if capture_mono_ns:
        latency_ms = (host_mono_ns - capture_mono_ns) / 1e6
    return Capture(
        wire_bytes=len(buf),
        sample_count=samples_per_channel * channel_count,
        channel_count=channel_count,
        latency_ms=latency_ms,
    )


async def command_rtt(ws, samples):
    """Measure control-plane round trips while the stream is running.

    Uses GetSampleRate because it is a pure read that every daemon build
    answers, so the number reflects handler scheduling rather than hardware.
    """
    rtts = []
    for _ in range(samples):
        start = time.perf_counter()
        await ws.send(json.dumps({"command": "GetSampleRate"}))
        deadline = time.perf_counter() + 2.0
        while time.perf_counter() < deadline:
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=2.0)
            except asyncio.TimeoutError:
                break
            if isinstance(raw, str) and "SampleRate" in raw:
                rtts.append((time.perf_counter() - start) * 1000.0)
                break
        await asyncio.sleep(0.05)
    return rtts


async def run(args):
    url = f"ws://{args.host}:{args.port}{args.path}"
    print(f"connecting to {url}")

    async with websockets.connect(url, max_size=None, ping_interval=None) as ws:
        setup = [
            {"command": "EnableChannel", "channel": {"Alphabetic": "A"}},
            {"command": "SetVoltsPerDiv", "channel": {"Alphabetic": "A"},
             "volts_per_div": args.volts_per_div},
            {"command": "SetTimePerDiv", "time_per_div": args.time_per_div},
            {"command": "SetCaptureMode", "capture_mode": "auto"},
            {"command": "SetTriggerLevel", "trigger_level": 0.0},
        ]
        if args.channels >= 2:
            setup.insert(1, {"command": "EnableChannel",
                             "channel": {"Alphabetic": "B"}})
        for command in setup:
            await ws.send(json.dumps(command))
            await asyncio.sleep(0.05)

        await ws.send(json.dumps({"command": "StartAcquisition",
                                  "trigger_position_percent": 50.0}))

        captures = []
        arrivals = []
        decode_ms = []
        started = time.perf_counter()
        deadline = started + args.duration
        last_arrival = None

        while time.perf_counter() < deadline:
            remaining = deadline - time.perf_counter()
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=max(0.1, remaining))
            except asyncio.TimeoutError:
                break

            arrived_at = time.perf_counter()
            host_mono_ns = time.monotonic_ns()

            decode_start = time.perf_counter()
            if isinstance(raw, bytes):
                capture = decode_lscp(raw, host_mono_ns)
                if capture is not None:
                    args.detected_binary = True
            else:
                capture = decode_legacy(raw)
            decode_elapsed = (time.perf_counter() - decode_start) * 1000.0

            if capture is None:
                continue

            captures.append(capture)
            decode_ms.append(decode_elapsed)
            if last_arrival is not None:
                arrivals.append((arrived_at - last_arrival) * 1000.0)
            last_arrival = arrived_at

        elapsed = time.perf_counter() - started
        await ws.send(json.dumps({"command": "StopAcquisition"}))

        rtts = await command_rtt(ws, args.rtt_samples) if args.rtt_samples else []

    report(args, captures, arrivals, decode_ms, rtts, elapsed)


def report(args, captures, arrivals, decode_ms, rtts, elapsed):
    print()
    print("=" * 62)
    print(f"  daemon stream baseline  ({elapsed:.1f}s)")
    print("=" * 62)

    if not captures:
        print("  no captures received")
        print("  the scope may not be triggering, or the daemon is not streaming")
        return

    total_bytes = sum(c.wire_bytes for c in captures)
    total_samples = sum(c.sample_count for c in captures)
    wire_format = "lscp (binary)" if args.detected_binary else "legacy (json)"

    print(f"  wire format          {wire_format}")
    print(f"  captures             {len(captures)}")
    print(f"  capture rate         {len(captures) / elapsed:.1f} /s")
    print(f"  samples delivered    {total_samples:,} "
          f"({total_samples / elapsed / 1000:.1f} kS/s)")
    print(f"  channels per capture {captures[0].channel_count}")
    print()
    print(f"  wire volume          {human_bytes(total_bytes)} "
          f"({human_bytes(total_bytes / elapsed)}/s)")
    print(f"  bytes per capture    {human_bytes(total_bytes / len(captures))}")
    if total_samples:
        print(f"  bytes per sample     {total_bytes / total_samples:.1f}")
    print()
    print(f"  client decode  p50   {percentile(decode_ms, 50):.2f} ms")
    print(f"  client decode  p99   {percentile(decode_ms, 99):.2f} ms")

    if arrivals:
        print()
        print(f"  inter-arrival  p50   {percentile(arrivals, 50):.2f} ms")
        print(f"  inter-arrival  p99   {percentile(arrivals, 99):.2f} ms")
        print(f"  inter-arrival  max   {max(arrivals):.2f} ms")

    latencies = [c.latency_ms for c in captures if c.latency_ms is not None]
    print()
    if latencies:
        print(f"  capture->client p50  {percentile(latencies, 50):.2f} ms")
        print(f"  capture->client p99  {percentile(latencies, 99):.2f} ms")
    else:
        print("  capture->client      unavailable on the legacy wire format")
        print("                       (no daemon-side timestamp in the frame)")

    if rtts:
        print()
        print(f"  command RTT    p50   {percentile(rtts, 50):.2f} ms")
        print(f"  command RTT    p99   {percentile(rtts, 99):.2f} ms")

    print("=" * 62)

    if arrivals and statistics.median(arrivals) > 0:
        print()
        print("  note: with a fixed poll interval the capture rate is bounded")
        print("        by the poll, not by the hardware.")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="localhost")
    parser.add_argument("--port", type=int, default=8085)
    # Path is what selects direct-to-daemon vs. through the :9000 relay, which
    # is the comparison that decides whether the relay is viable as the only
    # reachable data plane. Pass the ticket's ws_path to measure the relay.
    parser.add_argument("--path", default="/")
    parser.add_argument("--duration", type=float, default=15.0)
    parser.add_argument("--channels", type=int, default=1)
    parser.add_argument("--volts-per-div", type=float, default=1.0)
    parser.add_argument("--time-per-div", type=float, default=0.001)
    parser.add_argument("--rtt-samples", type=int, default=20)
    args = parser.parse_args()
    args.detected_binary = False

    asyncio.run(run(args))


if __name__ == "__main__":
    main()
