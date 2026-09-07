#!/usr/bin/env python3

# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0
"""
PicoScope hardware tests, mirroring the power-supply suites: run on the box
against a real unit and assert on what comes back rather than on mocks.

Run with: lager python test/api/measurement/test_scope_picoscope.py --box <YOUR-BOX>

Prerequisites:
- A scope net configured on the box (default 'scope1') backed by a PicoScope
- Nothing needs to be wired to the probe. The suite is built around internal
  consistency rather than a known input, so it passes on an open probe picking
  up mains hum as readily as on a signal generator: vpp really is vmax - vmin
  whatever the input is, and a level really should read back as it was set.

Override the net with: SCOPE_NET=my-scope lager python ...

What this covers that the unit tests cannot:
- The vendor SDK actually opens, and reports a model and channel count
- Settings survive a round trip through the driver and the hardware, which is
  where probe attenuation and volts-per-div-to-range mapping go wrong
- Measurements are self-consistent, which catches a scaling error that a
  single-value assertion would not
- Captures arrive, are the length they claim, and carry a working time axis
- Controls that report back correctly while changing nothing on the hardware.
  The horizontal position did exactly that, and so did the sample rate: both
  answered plausibly and neither described the capture that came out. Only a
  frame from a real unit tells them apart, which is why these live here
  rather than in the unit tests.
"""
import os
import sys
import time
import traceback

SCOPE_NET = os.environ.get("SCOPE_NET", "scope1")

# Voltages set and read back go through a volts/div-to-range mapping, so an
# exact match is not expected -- the daemon picks the nearest range the SDK
# offers. A trigger level, by contrast, is stored as given and should return
# exactly.
RANGE_TOLERANCE = 0.5      # 50%: the gap between adjacent SDK ranges
EXACT_TOLERANCE = 1e-6

_results = []


def _record(name, passed, detail=""):
    _results.append((name, passed, detail))
    status = "PASS" if passed else "FAIL"
    msg = f"  {status}: {name}"
    if detail:
        msg += f" -- {detail}"
    print(msg)


def _scope():
    from lager import Net, NetType
    return Net.get(SCOPE_NET, type=NetType.Analog)


def _close(actual, expected, tol):
    return abs(actual - expected) <= max(abs(expected) * tol, tol)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------
def test_capabilities():
    """The unit opens and describes itself."""
    scope = _scope()
    caps = scope.capabilities()

    model = caps.get("model")
    _record("model reported", bool(model), f"model={model!r}")

    channels = caps.get("analog_channels")
    ok = isinstance(channels, int) and 1 <= channels <= 8
    _record("channel count is plausible", ok, f"channels={channels!r}")

    serial = caps.get("serial")
    _record("serial reported", bool(serial), f"serial={serial!r}")

    # A scope that reports no voltage ranges cannot have its scale set, so an
    # empty list here explains every later scaling failure.
    ranges = caps.get("voltage_ranges") or []
    _record("voltage ranges reported", len(ranges) > 0, f"{len(ranges)} ranges")

    return all(p for _, p, _ in _results)


def test_channel_enable():
    """Enable and disable are observable."""
    scope = _scope()
    passed = True

    scope.enable_channel("A")
    on = scope.get_channel_display("A")
    _record("channel A enabled", on is True, f"is_enabled={on!r}")
    passed &= on is True

    scope.disable_channel("A")
    off = scope.get_channel_display("A")
    _record("channel A disabled", off is False, f"is_enabled={off!r}")
    passed &= off is False

    # Leave it on; everything below measures on A.
    scope.enable_channel("A")
    return passed


def test_channel_beyond_the_unit_is_refused():
    """A 2-channel unit says so rather than reading a misleading zero."""
    scope = _scope()
    channels = scope.capabilities().get("analog_channels") or 2
    beyond = chr(ord("A") + int(channels))

    try:
        scope.measure_vpp(beyond)
    except Exception as e:
        named = beyond in str(e)
        _record("channel beyond the unit is refused", named,
                f"channel {beyond}: {str(e)[-70:]}")
        return named

    _record("channel beyond the unit is refused", False,
            f"channel {beyond} returned a value on a {channels}-channel unit")
    return False


def test_vertical_scale_round_trip():
    """Volts/div survives the mapping onto an SDK range."""
    scope = _scope()
    passed = True

    for requested in (0.05, 0.2, 1.0, 2.0):
        scope.set_channel_scale(requested, "A")
        got = scope.get_channel_scale("A")
        ok = _close(got, requested, RANGE_TOLERANCE)
        _record(f"scale {requested} V/div", ok, f"read back {got}")
        passed &= ok

    scope.set_channel_scale(1.0, "A")
    return passed


def test_timebase_round_trip():
    """Seconds/div survives the mapping onto an SDK timebase index."""
    scope = _scope()
    passed = True

    for requested in (0.0001, 0.001, 0.01):
        scope.set_timebase_scale(requested)
        got = scope.get_timebase_scale()
        ok = _close(got, requested, RANGE_TOLERANCE)
        _record(f"timebase {requested} s/div", ok, f"read back {got}")
        passed &= ok

    scope.set_timebase_scale(0.001)
    return passed


def test_coupling_round_trip():
    scope = _scope()
    passed = True

    for requested in ("dc", "ac"):
        scope.set_channel_coupling(requested, "A")
        got = str(scope.get_channel_coupling("A")).lower()
        ok = got == requested
        _record(f"coupling {requested}", ok, f"read back {got!r}")
        passed &= ok

    scope.set_channel_coupling("dc", "A")
    return passed


def test_trigger_level_round_trip_through_the_probe():
    """A level is in probe-tip volts, at whatever attenuation is set.

    This is the assertion that caught the real bug: the level was stored
    divided by the attenuation and returned undivided, so through a 10x probe
    1.0 V read back as 0.1 V. It is checked at both attenuations because the
    1x case passes even when the conversion is missing entirely.
    """
    scope = _scope()
    passed = True
    original = scope.get_channel_probe("A")

    # A wide range, so the level is inside it at either attenuation.
    scope.set_channel_scale(1.0, "A")

    for attenuation in (1.0, 10.0):
        scope.set_channel_probe(attenuation, "A")
        for level in (0.0, 0.25, -0.5):
            scope.set_trigger_level(level)
            got = scope.get_trigger_level()
            ok = _close(got, level, EXACT_TOLERANCE)
            _record(f"trigger level {level} V at {attenuation}x", ok,
                    f"read back {got}")
            passed &= ok

    # Swapping the probe must not move the level the user chose.
    scope.set_channel_probe(1.0, "A")
    scope.set_trigger_level(0.5)
    scope.set_channel_probe(10.0, "A")
    after = scope.get_trigger_level()
    ok = _close(after, 0.5, EXACT_TOLERANCE)
    _record("level survives a probe swap", ok, f"0.5 -> {after}")
    passed &= ok

    scope.set_channel_probe(original, "A")
    scope.set_trigger_level(0.0)
    return passed


def test_trigger_slope_round_trip():
    scope = _scope()
    passed = True

    for requested in ("rising", "falling"):
        scope.set_trigger_slope(requested)
        got = str(scope.get_trigger_slope()).lower()
        ok = got == requested
        _record(f"trigger slope {requested}", ok, f"read back {got!r}")
        passed &= ok

    scope.set_trigger_slope("rising")
    return passed


def test_capture_mode_round_trip():
    scope = _scope()
    passed = True

    for requested in ("auto", "normal", "single"):
        scope.set_capture_mode(requested)
        got = str(scope.get_capture_mode()).lower()
        ok = got == requested
        _record(f"capture mode {requested}", ok, f"read back {got!r}")
        passed &= ok

    scope.set_capture_mode("auto")
    return passed


def test_acquisition_control():
    """Run, stop and force-trigger are accepted and captures flow."""
    scope = _scope()
    passed = True

    scope.run()
    # A capture has to actually arrive, which is the difference between the
    # daemon accepting the command and the hardware doing something.
    ready = False
    for _ in range(50):
        if scope.is_ready():
            ready = True
            break
        time.sleep(0.05)
    _record("a capture becomes available after run()", ready)
    passed &= ready

    scope.trigger_force()
    _record("force trigger accepted", True)

    scope.stop()
    _record("stop accepted", True)
    return passed


def test_measurements_are_self_consistent():
    """The measurement set has to agree with itself.

    Every relation here holds for any input, which is what makes this
    runnable on an open probe. A scaling error in the counts-to-volts
    conversion breaks the first two; a timebase error breaks the third.
    """
    scope = _scope()
    scope.set_channel_scale(1.0, "A")
    scope.run()
    time.sleep(0.3)

    values = scope.measure_all("A")
    scope.stop()

    if not values:
        _record("measure_all returned values", False, "empty")
        return False
    _record("measure_all returned values", True, f"{len(values)} quantities")

    passed = True
    vmax, vmin = values.get("vmax"), values.get("vmin")
    vpp, vavg = values.get("vpp"), values.get("vavg")
    vrms = values.get("vrms")

    if None not in (vmax, vmin, vpp):
        ok = _close(vpp, vmax - vmin, 1e-6)
        _record("vpp == vmax - vmin", ok,
                f"vpp={vpp:.6f} vs {vmax - vmin:.6f}")
        passed &= ok

    if None not in (vmax, vmin, vavg):
        ok = vmin <= vavg <= vmax
        _record("vmin <= vavg <= vmax", ok,
                f"{vmin:.4f} <= {vavg:.4f} <= {vmax:.4f}")
        passed &= ok

    if None not in (vrms, vmax, vmin):
        # RMS of any real signal cannot exceed its largest excursion.
        largest = max(abs(vmax), abs(vmin))
        ok = 0 <= vrms <= largest + 1e-6
        _record("0 <= vrms <= max|v|", ok, f"vrms={vrms:.4f} max|v|={largest:.4f}")
        passed &= ok

    period, frequency = values.get("period"), values.get("frequency")
    if period and frequency:
        ok = _close(period, 1.0 / frequency, 1e-6)
        _record("period == 1 / frequency", ok,
                f"period={period:.9f} 1/f={1.0 / frequency:.9f}")
        passed &= ok
    else:
        # Not a failure: an input without two clean cycles on screen has no
        # resolvable period, and the daemon says so rather than guessing.
        _record("period/frequency present", True,
                "absent -- no resolvable period on this input, which is allowed")

    return passed


def test_named_measurements_agree_with_measure_all():
    """The per-quantity accessors read the same scale as the bulk one.

    They take separate captures, so the values differ; what must not differ
    is the order of magnitude, which is what a unit-conversion slip changes.
    """
    scope = _scope()
    scope.set_channel_scale(1.0, "A")
    scope.run()
    time.sleep(0.3)

    bulk = scope.measure_all("A")
    passed = True

    for name, method in (("vpp", scope.measure_vpp),
                         ("vmax", scope.measure_vmax),
                         ("vmin", scope.measure_vmin),
                         ("vrms", scope.measure_vrms)):
        expected = bulk.get(name)
        if expected is None:
            continue
        actual = method("A")
        # Generous: separate captures of a live signal. This is a scale check.
        ok = abs(actual - expected) <= max(abs(expected) * 2.0, 0.05)
        _record(f"measure_{name}() matches measure_all()['{name}']", ok,
                f"{actual:.4f} vs {expected:.4f}")
        passed &= ok

    scope.stop()
    return passed


def test_capture_frames():
    """Frames arrive, are the length they claim, and have a usable time axis."""
    scope = _scope()
    info = scope.stream_start(channel="A", volts_per_div=1.0, time_per_div=0.001,
                              capture_mode="auto")
    passed = True

    rate = info.get("sample_rate")
    ok = isinstance(rate, (int, float)) and rate > 0
    _record("stream_start reports a sample rate", ok, f"sample_rate={rate}")
    passed &= ok

    try:
        frames = list(scope.stream_frames(count=3, timeout=5.0))
    except Exception as e:
        _record("three frames arrive", False, str(e)[:80])
        scope.stream_stop()
        return False

    got = len(frames)
    _record("three frames arrive", got == 3, f"{got} frames")
    passed &= got == 3

    for index, frame in enumerate(frames):
        counts = frame.counts("A")
        ok = len(counts) == frame.samples_per_channel
        _record(f"frame {index} sample count matches its header", ok,
                f"{len(counts)} vs {frame.samples_per_channel}")
        passed &= ok

        axis = frame.time_axis()
        ok = len(axis) == frame.samples_per_channel and axis[0] < axis[-1]
        _record(f"frame {index} time axis is monotonic", ok,
                f"{axis[0]:.6g}s to {axis[-1]:.6g}s")
        passed &= ok

        volts = frame.volts("A")
        # Within the range that was asked for, allowing the mapping headroom.
        largest = max(abs(float(volts.max())), abs(float(volts.min())))
        ok = largest < 1000.0
        _record(f"frame {index} volts are physical", ok, f"max|v|={largest:.4f}")
        passed &= ok

    # Sequence numbers must advance, or the client is being handed one frame
    # repeatedly and would show a frozen trace that looks live.
    seqs = [f.seq for f in frames]
    ok = len(set(seqs)) == len(seqs)
    _record("frames have distinct sequence numbers", ok, f"seqs={seqs}")
    passed &= ok

    scope.stream_stop()
    return passed


def test_csv_capture():
    """stream_capture writes the documented columns."""
    import csv
    import tempfile

    scope = _scope()
    scope.stream_start(channel="A", volts_per_div=1.0, time_per_div=0.001,
                       capture_mode="auto")

    path = os.path.join(tempfile.gettempdir(), "scope_hw_test.csv")
    passed = True
    try:
        result = scope.stream_capture(output=path, duration=2.0, samples=200)
        rows_reported = result.get("samples_per_channel", 0)
        ok = rows_reported > 0
        _record("stream_capture collected samples", ok,
                f"{rows_reported} per channel")
        passed &= ok

        with open(path, newline="") as handle:
            rows = list(csv.reader(handle))

        expected = ["capture", "channel", "sample_index", "time_ns", "voltage"]
        ok = rows and rows[0] == expected
        _record("CSV header matches the documented columns", ok,
                f"{rows[0] if rows else 'empty'}")
        passed &= ok

        ok = len(rows) - 1 == result.get("rows")
        _record("CSV row count matches the summary", ok,
                f"{len(rows) - 1} rows vs {result.get('rows')}")
        passed &= ok

        if len(rows) > 2:
            # Every value has to parse; a formatting slip here is invisible
            # until somebody loads the file.
            try:
                for row in rows[1:20]:
                    int(row[2])
                    float(row[3])
                    float(row[4])
                _record("CSV values parse as numbers", True)
            except ValueError as e:
                _record("CSV values parse as numbers", False, str(e))
                passed = False
    finally:
        scope.stream_stop()
        if os.path.exists(path):
            os.remove(path)

    return passed


def test_sample_rate_is_the_rate_in_use():
    """get_sample_rate() reports the timebase in effect, not the unit's ceiling.

    These are far apart -- a 2204A tops out at 100 MS/s and runs at about
    780 kS/s at 1 ms/div -- and reporting the ceiling was wrong in a way
    nothing local could catch: it is a plausible number, and it only shows up
    as a fault in whatever divides the depth by it to work out how long the
    capture window is. The horizontal position does exactly that, and was off
    by the ratio between the two. The frame carries the real interval, so it
    is the thing to check against.
    """
    scope = _scope()
    scope.set_timebase_scale(0.001)
    scope.run()
    time.sleep(0.3)
    frame = scope.capture(timeout=10.0)
    scope.stop()

    reported = scope.get_sample_rate()
    actual = frame.sample_rate_hz
    ok = _close(reported, actual, 0.05)
    _record("get_sample_rate() matches the frame's own interval", ok,
            f"reported {reported:.6g} S/s, frame {actual:.6g} S/s")

    # The ceiling is still reported, just under its own name.
    ceiling = (scope.capabilities() or {}).get("max_sample_rate_hz")
    if ceiling:
        under = reported <= ceiling * 1.001
        _record("the rate in use does not exceed the unit's maximum", under,
                f"{reported:.6g} <= {ceiling:.6g}")
        ok &= under

    return ok


def test_horizontal_position_moves_the_window():
    """A time offset moves the capture window relative to the trigger.

    The evidence is the frame's own pre/post-trigger split, not the value
    read back: the position was previously stored and returned faithfully
    while the hardware kept capturing the same centred window, so a readback
    test passed against a control that moved nothing.

    Positive looks forward, at signal later than the trigger, which means
    less of the window sits before it. Travel is one window each way, since
    a block is all the scope holds.
    """
    scope = _scope()
    scope.set_timebase_scale(0.001)
    scope.set_capture_mode("auto")

    rate, depth = scope.get_sample_rate(), scope.get_memory_depth()
    if not rate or not depth:
        _record("a capture window could be measured", False,
                f"rate={rate!r} depth={depth!r}")
        return False
    span = depth / rate

    def pre_fraction(offset):
        scope.trace_settings.set_time_offset(offset)
        scope.run()
        time.sleep(0.3)
        frame = scope.capture(timeout=10.0)
        total = frame.pre_trigger_samples + frame.post_trigger_samples
        return (frame.pre_trigger_samples / total) if total else 0.0

    passed = True
    centred = pre_fraction(0.0)
    ok = abs(centred - 0.5) < 0.05
    _record("a centred window is half pre-trigger", ok, f"{centred:.3f}")
    passed &= ok

    forward = pre_fraction(span / 4.0)
    ok = forward < centred - 0.1
    _record("looking forward shrinks the pre-trigger share", ok,
            f"{forward:.3f} vs centred {centred:.3f}")
    passed &= ok

    backward = pre_fraction(-span / 4.0)
    ok = backward > centred + 0.1
    _record("looking back grows the pre-trigger share", ok,
            f"{backward:.3f} vs centred {centred:.3f}")
    passed &= ok

    edge = pre_fraction(span)
    ok = edge < 0.02
    _record("travel stops at the edge of the block", ok, f"{edge:.3f}")
    passed &= ok

    scope.trace_settings.set_time_offset(0.0)
    readback = scope.trace_settings.get_time_offset()
    ok = _close(readback, 0.0, EXACT_TOLERANCE)
    _record("the offset returns to centre", ok, f"read back {readback}")
    passed &= ok

    scope.stop()
    return passed


def test_a_capture_is_in_probe_tip_volts():
    """Samples are referred to the probe tip, like volts/div and the level.

    The invariant is easy to state backwards: the probe ratio alone must NOT
    change what a count is worth. A 10x probe puts a tenth of the signal at
    the BNC and the driver picks a range a tenth as wide to match, so the
    same volts/div draws the same trace either way. What must move the scale
    is volts/div, because that is the screen changing.

    Asserted as ratios so there is no full-scale constant to get wrong, and
    so it holds on any PicoScope rather than this one.
    """
    scope = _scope()
    original = scope.get_channel_probe("A")
    scope.set_timebase_scale(0.001)

    def volts_per_count(probe, volts_per_div):
        scope.set_channel_probe(probe, "A")
        scope.set_channel_scale(volts_per_div, "A")
        scope.run()
        time.sleep(0.3)
        frame = scope.capture(timeout=10.0)
        index = frame.channel_index("A")
        return frame.channels[index].scale_v_per_count

    passed = True
    at_1x = volts_per_count(1.0, 1.0)
    at_10x = volts_per_count(10.0, 1.0)
    drift = abs(at_10x / at_1x - 1.0) if at_1x else 1.0
    ok = drift < 0.01
    _record("the same volts/div is the same trace through any probe", ok,
            f"{at_1x:.6g} vs {at_10x:.6g} V/count, {drift * 100:.3f}% apart")
    passed &= ok

    coarse = volts_per_count(10.0, 1.0)
    fine = volts_per_count(10.0, 0.1)
    zoom = coarse / fine if fine else 0.0
    ok = abs(zoom - 10.0) < 1.0
    _record("a tenth of the volts/div is a tenth of the scale", ok,
            f"ratio {zoom:.4f}")
    passed &= ok

    scope.stop()
    scope.set_channel_probe(original, "A")
    scope.set_channel_scale(1.0, "A")
    return passed


def test_offset_matches_what_the_hardware_can_do():
    """An offset is applied, or refused with a reason -- never quietly dropped.

    A 2000-series unit has no analog offset. The value used to be stored and
    put in the frame anyway, where it was added back on the way to volts: the
    trace stayed where it was and the measurements moved instead, so Vmax,
    Vmin and Vavg all described a signal that was not on screen. Refusing is
    the honest answer. Units that do have the hardware must still accept it,
    so which behaviour is correct comes from the capabilities.
    """
    scope = _scope()
    supported = bool((scope.capabilities() or {}).get("analog_offset"))

    if supported:
        scope.trace_settings.set_volt_offset(0.5)
        got = scope.trace_settings.get_volt_offset()
        ok = _close(got, 0.5, RANGE_TOLERANCE)
        _record("an offset the hardware has is applied", ok, f"read back {got}")
        scope.trace_settings.set_volt_offset(0.0)
        return ok

    try:
        scope.trace_settings.set_volt_offset(0.5)
    except Exception as e:
        message = str(e)
        explains = "offset" in message.lower()
        _record("an offset the hardware lacks is refused", True, message[-70:])
        _record("the refusal explains itself", explains, message[:70])
        return explains

    _record("an offset the hardware lacks is refused", False,
            "0.5 V accepted on a unit reporting analog_offset=false")
    return False


def test_unsupported_features_report_the_gap():
    """A missing feature raises rather than returning a wrong number."""
    scope = _scope()
    passed = True

    for label, call in (
        ("autoscale", lambda: scope.autoscale()),
        ("cursor.set_a", lambda: scope.cursor.set_a(1.0)),
        ("measurement.variance", lambda: scope.measurement.variance()),
        ("measurement.delay", lambda: scope.measurement.delay_rising_rising_edge()),
    ):
        try:
            value = call()
        except Exception as e:
            _record(f"{label} reports the gap", True, str(e)[-60:])
            continue
        _record(f"{label} reports the gap", False, f"returned {value!r}")
        passed = False

    return passed


def test_the_net_api_groups_reach_the_hardware():
    """The Rigol-shaped groups work against a PicoScope.

    The whole point of the mapper: a script written against a Rigol should
    run unchanged. These are the calls such a script makes.
    """
    scope = _scope()
    passed = True

    scope.start_capture()
    _record("start_capture()", True)
    time.sleep(0.2)

    vpp = scope.measurement.voltage_peak_to_peak()
    ok = isinstance(vpp, float) and vpp >= 0
    _record("measurement.voltage_peak_to_peak()", ok, f"{vpp}")
    passed &= ok

    scope.trace_settings.set_volts_per_div(0.5)
    got = scope.trace_settings.get_volts_per_div()
    ok = _close(got, 0.5, RANGE_TOLERANCE)
    _record("trace_settings volts/div round trip", ok, f"read back {got}")
    passed &= ok

    scope.trigger_settings.set_mode_normal()
    mode = scope.trigger_settings.get_mode()
    ok = str(mode).lower() == "normal"
    _record("trigger_settings.set_mode_normal()", ok, f"mode={mode!r}")
    passed &= ok

    scope.trigger_settings.edge.set_slope_falling()
    slope = scope.trigger_settings.edge.get_slope()
    ok = str(slope).lower() == "falling"
    _record("trigger_settings.edge slope round trip", ok, f"slope={slope!r}")
    passed &= ok

    status = scope.trigger_settings.get_status()
    ok = status in ("READY", "WAIT")
    _record("trigger_settings.get_status()", ok, f"status={status!r}")
    passed &= ok

    scope.stop_capture()
    _record("stop_capture()", True)

    scope.trigger_settings.set_mode_auto()
    scope.trace_settings.set_volts_per_div(1.0)
    return passed


def main():
    print("=" * 60)
    print(f"PicoScope hardware tests -- net {SCOPE_NET!r}")
    print("=" * 60)

    tests = [
        ("Capabilities",                  test_capabilities),
        ("Channel Enable/Disable",         test_channel_enable),
        ("Channel Beyond The Unit",        test_channel_beyond_the_unit_is_refused),
        ("Vertical Scale Round Trip",      test_vertical_scale_round_trip),
        ("Timebase Round Trip",            test_timebase_round_trip),
        ("Coupling Round Trip",            test_coupling_round_trip),
        ("Trigger Level Through Probe",    test_trigger_level_round_trip_through_the_probe),
        ("Trigger Slope Round Trip",       test_trigger_slope_round_trip),
        ("Capture Mode Round Trip",        test_capture_mode_round_trip),
        ("Acquisition Control",            test_acquisition_control),
        ("Sample Rate In Use",             test_sample_rate_is_the_rate_in_use),
        ("Horizontal Position",            test_horizontal_position_moves_the_window),
        ("Probe-Tip Volts",                test_a_capture_is_in_probe_tip_volts),
        ("Offset Matches The Hardware",    test_offset_matches_what_the_hardware_can_do),
        ("Measurement Self-Consistency",   test_measurements_are_self_consistent),
        ("Named vs Bulk Measurements",     test_named_measurements_agree_with_measure_all),
        ("Capture Frames",                 test_capture_frames),
        ("CSV Capture",                    test_csv_capture),
        ("Unsupported Features",           test_unsupported_features_report_the_gap),
        ("Net API Groups",                 test_the_net_api_groups_reach_the_hardware),
    ]

    test_results = []
    try:
        for name, test_fn in tests:
            print(f"\n{name}")
            try:
                test_results.append((name, bool(test_fn())))
            except Exception as e:
                print(f"\nUNEXPECTED ERROR in {name}: {e}")
                traceback.print_exc()
                test_results.append((name, False))
    finally:
        try:
            _scope().stop()
        except Exception:
            pass

    print("\n" + "=" * 60)
    print("TEST SUMMARY")
    print("=" * 60)

    for name, p in test_results:
        print(f"  [{'PASS' if p else 'FAIL'}] {name}")

    passed_count = sum(1 for _, p in test_results if p)
    print(f"\nTotal: {passed_count}/{len(test_results)} test groups passed")

    sub_passed = sum(1 for _, p, _ in _results if p)
    sub_total = len(_results)
    print(f"Sub-tests: {sub_passed}/{sub_total} passed", end="")
    if sub_total - sub_passed:
        print(f" ({sub_total - sub_passed} failed)")
        print("\nFailed sub-tests:")
        for name, p, detail in _results:
            if not p:
                print(f"  FAIL: {name} -- {detail}")
    else:
        print()

    return 0 if passed_count == len(test_results) else 1


if __name__ == "__main__":
    sys.exit(main())
