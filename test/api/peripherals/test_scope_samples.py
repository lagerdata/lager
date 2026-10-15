# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

# test_scope_samples.py
# Run with: lager python test_scope_samples.py --box MY-BOX
# Runs on scope3 (Rigol MSO5204 CH1) unless --env SCOPE_NET=<net> names another
# scope net, of either make. A Rigol needs a live view paired with
# `lager nets set-capture` for stream_frames.

from lager import Net, NetType
import math
import os
import sys
import time

_results = []
def _record(name, passed, detail=""):
    _results.append((name, passed, detail))
    status = "PASS" if passed else "FAIL"
    msg = f"  {status}: {name}"
    if detail:
        msg += f" -- {detail}"
    print(msg)

def _has_samples(frame):
    if not frame.channels or frame.samples_per_channel <= 0:
        return False
    return any(not math.isnan(v) for v in frame.volts(0))

def main():
    print("=== Oscilloscope Samples Test ===\n")

    net_name = os.environ.get("SCOPE_NET", "scope3")
    scope = None

    try:
        scope = Net.get(net_name, type=NetType.Analog)
        _record("get_net", True, f"retrieved {net_name}")

        scope.enable()
        scope.trace_settings.set_volts_per_div(1.0)
        scope.trace_settings.set_time_per_div(0.001)
        scope.trigger_settings.set_mode_auto()
        scope.start_capture()
        _record("configure", True, "volts=1.0, time=0.001, auto")
        time.sleep(0.5)

        try:
            frame = scope.capture(timeout=10)
            _record("capture", _has_samples(frame),
                    f"{frame.samples_per_channel} samples on {[c.channel for c in frame.channels]}"
                    f" at {frame.sample_rate_hz:.0f} S/s")
        except Exception as e:
            _record("capture", False, str(e))

        try:
            frames = list(scope.stream_frames(count=3, timeout=10))
            seqs = [f.seq for f in frames]
            fresh = len(frames) == 3 and all(b > a for a, b in zip(seqs, seqs[1:]))
            _record("stream_frames", fresh and all(_has_samples(f) for f in frames),
                    f"seq {seqs}")
        except Exception as e:
            _record("stream_frames", False, str(e))

    except Exception as e:
        _record("setup", False, str(e))

    finally:
        if scope is not None:
            try:
                scope.close_capture_connection()
            except Exception:
                pass
            try:
                scope.stop_capture()
            except Exception:
                pass
            try:
                scope.disable()
            except Exception:
                pass

    passed = sum(1 for _, p, _ in _results if p)
    failed = sum(1 for _, p, _ in _results if not p)
    print(f"\n=== Summary: {passed} passed, {failed} failed out of {len(_results)} tests ===")
    return 0 if failed == 0 else 1

if __name__ == "__main__":
    sys.exit(main())
