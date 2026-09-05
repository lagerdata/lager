# Transport baseline

Measured on STG-2 with a PicoScope 2204A/2205A (`0ce9:1007`), driven from
inside the box container against the pre-rewrite daemon. Reproduce with:

    docker exec lager python3 stream_bench.py --duration 15 --channels N

## Before (legacy JSON wire format)

| | 1 channel | 2 channels |
|---|---|---|
| capture rate | 26.1 /s | 29.3 /s |
| samples delivered | 208.6 kS/s | 176.0 kS/s |
| wire volume | 16.5 MB/s | 13.7 MB/s |
| bytes per capture | 648.1 KB | 478.7 KB |
| **bytes per sample** | **83.0** | **81.7** |
| client decode p50 | 36.90 ms | 33.03 ms |
| client decode p99 | 46.04 ms | 34.84 ms |
| inter-arrival p50 | 38.02 ms | 33.92 ms |
| inter-arrival p99 | 47.52 ms | 35.69 ms |
| command RTT p50 | 1.33 ms | 1.45 ms |
| command RTT p99 | 1.91 ms | 16.01 ms |

Capture-to-client latency is not measurable on this format: the frame carries
no daemon-side timestamp.

## After (LSCP binary wire format, hardware thread, adaptive poll)

Same box, same scope, same harness, immediately after deploying the rewrite.

| | 1 channel | 2 channels |
|---|---|---|
| capture rate | 84.4 /s | 105.7 /s |
| samples delivered | 675.5 kS/s | 634.5 kS/s |
| wire volume | 1.3 MB/s | 1.2 MB/s |
| bytes per capture | 15.7 KB | 11.8 KB |
| **bytes per sample** | **2.0** | **2.0** |
| client decode p50 | 0.01 ms | 0.01 ms |
| client decode p99 | 0.03 ms | 0.03 ms |
| inter-arrival p50 | 11.72 ms | 9.38 ms |
| inter-arrival p99 | 13.06 ms | 10.39 ms |
| capture->client p50 | 0.39 ms | 0.40 ms |
| capture->client p99 | 0.84 ms | 0.76 ms |
| command RTT p50 | 0.70 ms | 0.65 ms |
| command RTT p99 | 1.47 ms | 1.38 ms |

### Change

| | before | after | |
|---|---|---|---|
| capture rate, 1ch | 26.1 /s | 84.4 /s | 3.2x |
| capture rate, 2ch | 29.3 /s | 105.7 /s | 3.6x |
| bytes per sample | 83.0 | 2.0 | 41x smaller |
| wire volume, 1ch | 16.5 MB/s | 1.3 MB/s | 12.7x less, for 3.2x the captures |
| client decode p50 | 36.90 ms | 0.01 ms | ~3700x |
| command RTT p99, 2ch | 16.01 ms | 1.38 ms | 11.6x |
| capture->client | not measurable | 0.39 ms p50 | — |

Three things worth drawing out.

**The decode wall is gone.** 36.90 ms of parsing per capture became 0.01 ms,
because decoding is now a bounds check and a typed view over the received
buffer rather than parsing 8000 JSON objects. The client is no longer the
bottleneck at any rate this hardware can produce.

**Command latency no longer degrades with load.** Enabling a second channel
used to push command RTT p99 from 1.91 ms to 16.01 ms while p50 barely moved,
which is the signature of contention rather than load: commands were queued
behind the streaming loop holding the scope mutex across `ps2000_get_values`.
With all FFI on a dedicated thread, p99 is 1.38 ms at two channels versus
1.47 ms at one -- no penalty at all, so the contention is genuinely gone
rather than merely reduced.

**Capture-to-client latency is now measurable, and it is small.** The old
frame carried no timestamp, so this could not be quantified. At 0.39 ms p50
the transport contributes almost nothing next to the ~9-12 ms the hardware
takes to fill a block.

The remaining inter-arrival time is hardware, not software: at 84-106
captures/sec the daemon is bounded by USB round-trip and block rearm. Encoding
one capture costs 201 ns against an inter-arrival of ~9-12 ms.

## What the numbers say

**The client is decode-bound, not hardware-bound or network-bound.** Decode p50
(36.9 ms) is within 3% of inter-arrival p50 (38.0 ms), so the consumer spends
essentially the entire inter-capture interval parsing the previous capture. A
faster scope would not raise the delivered rate.

**83 bytes per sample.** Each sample ships as a JSON object tagging every
reading with a channel enum and an index:
`{"channel":{"Alphabetic":"A"},"voltage":0.123456,"sample_index":4095}`. The
underlying datum is a single `int16` ADC count, so the wire carries roughly 40x
more bytes than the information in it. At 208 kS/s of real signal this produces
16.5 MB/s of traffic.

**Command RTT p99 degrades 8x under two-channel load** (1.91 ms to 16.01 ms)
while p50 barely moves. That is the signature of lock contention rather than
load: control commands block behind the streaming loop holding the scope mutex
across FFI calls, so the tail suffers while the median does not.

These three observations are what the transport rewrite targets, in order:
packed binary framing removes the decode wall, and the dedicated hardware
thread removes the tail-latency contention.

## Codec microbenchmarks

`cargo bench -p daemon`, on an arm64 dev machine. Both encoders run over
identical sample data, so the comparison is measured rather than asserted.

| capture | LSCP encode | LSCP decode | legacy JSON encode |
|---|---|---|---|
| 8000 x 1ch | 201 ns | 2.78 µs | 443 µs |
| 8000 x 2ch | 342 ns | 5.49 µs | 908 µs |
| 32000 x 4ch | 3.14 µs | 43.6 µs | — |
| 128000 x 2ch | 6.13 µs | — | — |

Encoding the real STG-2 capture went from 443 µs to 201 ns, about 2200x. The
gap is structural rather than an optimization: the old path formatted 8000
JSON objects with a channel enum and a float per sample, while the new one
memcpys a contiguous `i16` block behind a 64-byte header.

At 201 ns per capture, encoding is no longer a factor in the capture rate at
any rate the hardware can produce.

## Wire size

| capture | legacy | LSCP | ratio |
|---|---|---|---|
| 8000 x 1ch | 648.1 KB | 15.7 KB | 41x |
| bytes per sample | 83.0 | 2.0 | 41x |

The floor is two bytes per sample, since that is the size of the ADC count
itself. The remaining 80 bytes were JSON syntax, a repeated channel tag, a
sample index recoverable from position, and a float expansion of an integer.

## The :9000 relay

The daemon binds loopback only, so every external client reaches captures
through the box HTTP server's relay (`GET /scope/<net>/ws`). The question that
decided whether that is acceptable as the *only* data plane: does a Python
relay in the middle sustain the capture rate? Measured on STG-2, two channels,
25 s, same harness pointed at each endpoint.

| | direct :8085 | via :9000 relay |
|---|---|---|
| throughput | 1.1 MB/s | 1.1 MB/s |
| bytes per sample | 2.0 | 2.0 |
| inter-arrival p50 | 10.25 ms | 10.16 ms |
| inter-arrival p99 | 11.56 ms | 11.69 ms |
| inter-arrival max | 11.88 ms | 12.39 ms |
| capture->client p50 | 0.88 ms | 1.60 ms |
| capture->client p99 | 1.07 ms | 1.88 ms |
| command RTT p50 | 1.49 ms | 2.59 ms |
| command RTT p99 | 1.74 ms | 3.06 ms |

**The relay is free in throughput terms and costs ~0.7 ms in latency.**
Inter-arrival is unchanged within noise, including the max, so no capture is
delayed enough to be dropped or coalesced. That is the property that matters:
the relay forwards frames verbatim, never decoding LSCP, so its per-frame cost
is independent of capture size and the hardware remains the only bottleneck.

Two findings were required to get there, both worth keeping in mind for any
future WebSocket work on the box:

**permessage-deflate must be off.** simple_websocket answers every handshake
with `PerMessageDeflate`, and both browsers and the `websockets` library offer
it, so it gets negotiated by default. Compressing raw ADC counts wins almost
nothing and cost 3.52 ms median capture-to-client latency with spikes to
44.8 ms, against 1.60 ms and a 12.4 ms max with it disabled.

**Nagle must be off.** With compression already fixed, command RTT still
showed a 42.44 ms p99 against a 2.59 ms median -- the unmistakable Nagle plus
delayed-ACK interaction on the small (tens of bytes) command frames. Captures
never saw it because they fill segments. `TCP_NODELAY` on both relay sockets
brought p99 to 3.06 ms.

Both are one-line fixes, but neither is visible in a functional test: the
relay worked correctly the whole time and only the tail latency was wrong.
