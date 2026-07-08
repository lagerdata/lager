#!/usr/bin/env python3

# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0
"""
Verify that the LabJack I2C_SPEED_THROTTLE actually changes bus speed,
without needing an oscilloscope.

Times repeated fixed-size reads at several configured frequencies. Bus
time per transaction is (bits / frequency), so slower clocks make each
read measurably slower. Per-transaction overhead (LJM register writes,
USB/network round trips) is cancelled by subtracting the max-speed
baseline, giving an implied bus frequency to compare against the request.

Run with:
  lager python test_i2c_labjack_timing.py --box <boxname> \
      --env I2C_NET=<netname> [--env I2C_ADDR=0x76]

Prerequisites:
- An I2C net configured in /etc/lager/saved_nets.json with instrument
  "labjack_t7"
- At least one responding device on the bus (address auto-detected via
  scan if I2C_ADDR is not set)

Pass criteria (loose -- T-series I2C is software-timed and varies with load):
- Median read duration strictly increases as requested frequency decreases
- Implied bus frequency within a factor of ~2 of each request
"""
import os
import statistics
import sys
import time

I2C_NET = os.environ.get("I2C_NET", "i2c2")
I2C_ADDR = os.environ.get("I2C_ADDR", "")
NUM_BYTES = int(os.environ.get("NUM_BYTES", "56"))  # LabJack max per transaction
REPS = int(os.environ.get("REPS", "20"))

# Requested frequency -> acceptable implied-frequency range.
# 450 kHz is the throttle=0 baseline that overhead is measured against.
#
# Default floor is 10 kHz: many slave chips have an SMBus-style bus
# timeout (~25-35 ms) and will wedge the bus (holding SDA low, error
# 2720 I2C_BUS_BUSY) if a multi-byte transaction runs slower than that.
# Set INCLUDE_1KHZ=1 to test 1 kHz anyway on slaves that tolerate it.
BASELINE_FREQ = 450_000
TEST_FREQS = [
    (100_000, (50_000, 200_000)),
    (10_000, (5_000, 20_000)),
]
if os.environ.get("INCLUDE_1KHZ"):
    TEST_FREQS.append((1_000, (500, 2_000)))

# Approximate clocks per transaction: address byte plus each data byte
# is 8 bits + ACK.
BITS_PER_READ = (NUM_BYTES + 1) * 9


def median_read_seconds(i2c, addr):
    durations = []
    for _ in range(REPS):
        start = time.perf_counter()
        data = i2c.read(addr, NUM_BYTES)
        durations.append(time.perf_counter() - start)
        assert len(data) == NUM_BYTES, f"short read: {len(data)}/{NUM_BYTES}"
    return statistics.median(durations)


def main():
    from lager import Net, NetType

    print(f"Net: {I2C_NET}, read size: {NUM_BYTES} bytes, reps: {REPS}")
    i2c = Net.get(I2C_NET, NetType.I2C)

    if I2C_ADDR:
        addr = int(I2C_ADDR, 0)
    else:
        i2c.config(frequency_hz=BASELINE_FREQ)
        found = i2c.scan()
        if not found:
            print("FAIL: no I2C devices found on bus; set I2C_ADDR or check wiring")
            return 1
        addr = found[0]
    print(f"Target device: 0x{addr:02x}")

    # Baseline at max speed (throttle=0)
    i2c.config(frequency_hz=BASELINE_FREQ)
    baseline = median_read_seconds(i2c, addr)
    print(f"\n{BASELINE_FREQ:>7} Hz requested: median {baseline*1000:8.2f} ms  (baseline)")

    ok = True
    prev = baseline
    for freq, (lo, hi) in TEST_FREQS:
        i2c.config(frequency_hz=freq)
        dur = median_read_seconds(i2c, addr)

        extra = dur - baseline
        # extra bus time per bit = 1/f - 1/baseline  =>  solve for f
        if extra > 0:
            implied = 1.0 / (extra / BITS_PER_READ + 1.0 / BASELINE_FREQ)
        else:
            implied = float("inf")

        slower = dur > prev
        in_range = lo <= implied <= hi
        status = "PASS" if (slower and in_range) else "FAIL"
        if status == "FAIL":
            ok = False
        print(f"{freq:>7} Hz requested: median {dur*1000:8.2f} ms  "
              f"implied ~{implied:,.0f} Hz  "
              f"(expect {lo:,}-{hi:,}) {status}")
        prev = dur

    # Restore standard speed
    i2c.config(frequency_hz=100_000)

    print()
    if ok:
        print("PASS: throttle scales bus speed as expected")
        return 0
    print("FAIL: bus speed did not track requested frequency")
    return 1


if __name__ == "__main__":
    sys.exit(main())
