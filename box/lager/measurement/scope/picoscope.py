# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

"""
PicoScope oscilloscope driver.

Presents the same method surface as ``rigol_mso5000.RigolMso5000`` so that the
CLI, Python API, and MCP can drive either scope through one set of calls. The
difference is underneath: the Rigol speaks SCPI over VISA, while a PicoScope is
a USB device with a vendor SDK, driven here through the oscilloscope daemon's
WebSocket control plane (see ``daemon_client.py``).

Two consequences of that difference are worth knowing:

* **Measurements are computed, not queried.** No PicoTech API has an
  equivalent of ``:MEAS:VPP?``, so the daemon captures a block and computes
  the value from the samples. One capture yields the whole measurement set,
  so ``measure_all`` costs the same as ``measure_vpp``.

* **Channels are letters.** PicoScope channels are A-D where the Rigol's are
  1-4. Every method here accepts either and normalizes, so callers written
  against the Rigol keep working.

This replaces the ``PassThroughMapper`` that scope nets used to resolve to,
which exposed no scope operations at all.
"""
from __future__ import annotations

import csv
import logging
import time

from . import daemon_client

logger = logging.getLogger(__name__)

# Volts-per-division to full-scale conversion. A PicoScope has no notion of a
# division -- the SDK takes a voltage range -- so the daemon maps a requested
# volts/div onto the nearest range it supports. Eight divisions is the
# convention the Rigol and the web UI both use, and keeping it here means a
# `scale 0.5` means the same thing on either instrument.
DIVISIONS_VERTICAL = 8
DIVISIONS_HORIZONTAL = 10

# Wire tokens, which are not uniform across the protocol's enums: Coupling
# serializes as written (`DC`) while CaptureMode and TriggerSlope are
# lowercased by serde. Sending the wrong case is not silently tolerated -- the
# daemon answers "unknown variant" -- so these tables hold the exact tokens
# rather than deriving them from the input.
_COUPLINGS = {"dc": "DC", "ac": "AC", "gnd": "GND", "ground": "GND"}

# Trigger in the middle of the block: half the capture before it, half after,
# which is the unshifted window.
_CENTRED_TRIGGER_PERCENT = 50.0

_SLOPES = {
    "rising": "rising", "positive": "rising", "pos": "rising", "rise": "rising",
    "falling": "falling", "negative": "falling", "neg": "falling", "fall": "falling",
    "either": "either", "both": "either", "any": "either",
    "neither": "neither", "none": "neither",
}

_CAPTURE_MODES = {
    "auto": "auto", "normal": "normal", "norm": "normal", "single": "single",
}

# Rigol measurement item names -> daemon measurement names, so
# get_measure_item() accepts what a Rigol caller already passes.
_MEASURE_ITEMS = {
    "vpp": "vpp", "vmax": "vmax", "vmin": "vmin", "vrms": "vrms",
    "vavg": "vavg", "vtop": "vmax", "vbase": "vmin",
    "period": "period", "freq": "frequency", "frequency": "frequency",
    "prise": "rise_time", "rise": "rise_time", "risetime": "rise_time",
    "pfall": "fall_time", "fall": "fall_time", "falltime": "fall_time",
    "pduty": "duty_cycle_pos", "nduty": "duty_cycle_neg",
    "pwidth": "pulse_width_pos", "nwidth": "pulse_width_neg",
    "overshoot": "overshoot",
}


class UnsupportedScopeFeature(RuntimeError):
    """The attached unit cannot do this.

    Raised instead of silently doing nothing, so a caller asking a 2-channel
    2204A about channel C gets told rather than reading a misleading zero.
    """


def trace_voltage_at(times, volts, at):
    """The trace's voltage at time ``at``, linearly interpolated.

    ``None`` when ``at`` falls outside the captured window: a cursor parked
    off the end of the record has no signal under it, and interpolating from
    the nearest end would answer with a voltage from a different moment.

    The samples land on a grid the user did not choose, so a cursor almost
    never sits exactly on one. Interpolating between the two it falls between
    is what a scope does, and it matters most where it is easiest to notice --
    on a fast edge, where the nearest sample can be most of the amplitude
    away.
    """
    if times is None or volts is None:
        return None
    count = min(len(times), len(volts))
    if count == 0:
        return None
    if at < times[0] or at > times[count - 1]:
        return None

    # Bisect rather than scan: a block is thousands of samples and the web UI
    # asks for this on every frame it draws.
    low, high = 0, count - 1
    while high - low > 1:
        middle = (low + high) // 2
        if times[middle] <= at:
            low = middle
        else:
            high = middle

    span = times[high] - times[low]
    if span <= 0:
        return float(volts[low])
    fraction = (at - times[low]) / span
    return float(volts[low]) + fraction * (float(volts[high]) - float(volts[low]))


def cursor_readings(time_pair=None, volts_pair=None, times=None, volts=None):
    """Everything a pair of time cursors and a pair of voltage cursors say.

    A free function, and the only place the arithmetic lives, so the CLI's
    answer and the panel's are the same answer. Absent quantities are left
    out rather than zeroed, the same way a capture with no resolvable period
    reports no period: a cursor off the end of the record has no voltage
    under it, and two cursors at the same instant have no frequency.
    """
    readings = {}

    if time_pair:
        t1, t2 = float(time_pair[0]), float(time_pair[1])
        readings["t1"] = t1
        readings["t2"] = t2
        readings["delta_t"] = t2 - t1
        if readings["delta_t"]:
            # What the pair is usually for: put them a cycle apart and read
            # the frequency off directly.
            readings["frequency"] = 1.0 / readings["delta_t"]

        if times is not None and volts is not None and len(times):
            readings["window_start"] = float(times[0])
            readings["window_end"] = float(times[len(times) - 1])
            trace_v1 = trace_voltage_at(times, volts, t1)
            trace_v2 = trace_voltage_at(times, volts, t2)
            if trace_v1 is not None:
                readings["trace_v1"] = trace_v1
            if trace_v2 is not None:
                readings["trace_v2"] = trace_v2
            if trace_v1 is not None and trace_v2 is not None:
                readings["trace_delta_v"] = trace_v2 - trace_v1

    if volts_pair:
        v1, v2 = float(volts_pair[0]), float(volts_pair[1])
        readings["v1"] = v1
        readings["v2"] = v2
        readings["delta_v"] = v2 - v1

    return readings


def normalize_channel(channel) -> dict:
    """Render a channel as the daemon's ``ChannelId`` JSON.

    Accepts ``"A"``, ``"a"``, ``1``, ``"1"``, or ``"CHAN1"`` -- the spellings
    that arrive from the web UI, the CLI, saved-net pin fields, and callers
    written against the Rigol respectively.
    """
    if channel is None:
        return {"Alphabetic": "A"}

    if isinstance(channel, dict):
        # Already in wire form (from the UI or a replayed command).
        if "Alphabetic" in channel or "Numeric" in channel:
            return channel
        raise ValueError("Unrecognized channel object: %r" % (channel,))

    text = str(channel).strip().upper()
    for prefix in ("CHANNEL", "CHAN", "CH"):
        if text.startswith(prefix):
            text = text[len(prefix):]
            break

    if text.isdigit():
        index = int(text)
        if not 1 <= index <= 26:
            raise ValueError("Channel number out of range: %r" % (channel,))
        # 1 -> A, matching how the UI labels the first channel.
        return {"Alphabetic": chr(ord("A") + index - 1)}

    if len(text) == 1 and text.isalpha():
        return {"Alphabetic": text}

    raise ValueError("Unrecognized channel: %r" % (channel,))


def channel_label(channel) -> str:
    """Human-readable label for a channel, for messages and errors."""
    wire = normalize_channel(channel)
    if "Alphabetic" in wire:
        return wire["Alphabetic"]
    return str(wire["Numeric"])


def _lookup(table, value, kind):
    key = str(value).strip().lower()
    if key not in table:
        raise ValueError("Unknown %s %r; expected one of %s" % (
            kind, value, ", ".join(sorted(set(table)))))
    return table[key]


class PicoScope:
    """Scope operations for a PicoScope net, over the daemon control plane.

    One instance per net. The connection to the daemon is lazy and persistent:
    lazy so constructing a driver never touches hardware, persistent because
    reconnecting per command would triple the cost of the cheapest ones.
    """

    def __init__(self, address=None, pin=None, channel=None, netname=None, **kwargs):
        self.address = address
        self.netname = netname
        # A scope net's pin is the channel it is wired to; it becomes the
        # default for every per-channel call, matching the Rigol driver.
        self.channel = pin or channel or 1
        self._client = None
        self._capabilities = None
        # Cursor positions, held here rather than in the daemon: they are
        # nothing the hardware knows about, and putting them on the box
        # instead of in the browser is what lets the CLI place a cursor and
        # the web UI draw it. Seconds relative to the trigger, and volts at
        # the probe tip, matching every other number in this driver.
        self._cursors = {"time": None, "volts": None}

    # -- plumbing --------------------------------------------------------
    @property
    def client(self) -> daemon_client.ScopeDaemonClient:
        if self._client is None:
            self._client = daemon_client.ScopeDaemonClient()
        return self._client

    def close(self) -> None:
        if self._client is not None:
            self._client.close()
            self._client = None

    def _command(self, name, **params):
        return self.client.command(name, **params)

    def _channel(self, channel=None) -> dict:
        return normalize_channel(channel if channel is not None else self.channel)

    # -- capabilities ----------------------------------------------------
    def capabilities(self, refresh: bool = False) -> dict:
        """What the attached unit supports, as detected when it was opened.

        Cached because it cannot change without the daemon reopening the
        device, and the UI asks for it on every page load.
        """
        if self._capabilities is None or refresh:
            response = self._command("GetCapabilities")
            self._capabilities = response.get("capabilities") or {}
        return self._capabilities

    def _require_channel(self, channel=None) -> dict:
        """Normalize a channel and reject one this unit does not have."""
        wire = self._channel(channel)
        try:
            capabilities = self.capabilities()
        except daemon_client.ScopeDaemonError:
            # Without capabilities we cannot check, and refusing the command
            # on that basis would be worse than letting the daemon answer.
            return wire

        count = capabilities.get("analog_channels")
        if not count or "Alphabetic" not in wire:
            return wire

        available = [chr(ord("A") + i) for i in range(int(count))]
        if wire["Alphabetic"] not in available:
            raise UnsupportedScopeFeature(
                "%s has %d channel(s) (%s); channel %s does not exist" % (
                    capabilities.get("model") or "this scope", int(count),
                    ", ".join(available), wire["Alphabetic"]))
        return wire

    # ============ Acquisition Control ============
    def run(self):
        """Start continuous acquisition."""
        self._command("SetCaptureMode", capture_mode="auto")
        self._start_acquisition()
        return {"status": "running"}

    def stop(self):
        """Stop acquisition."""
        self._command("StopAcquisition")
        return {"status": "stopped"}

    def single(self):
        """Arm for a single acquisition."""
        self._command("SetCaptureMode", capture_mode="single")
        self._start_acquisition()
        return {"status": "single"}

    def _start_acquisition(self):
        """Arm a capture, keeping whatever horizontal position is set.

        StartAcquisition carries the pre/post-trigger split, and the daemon
        stores whatever it is handed for every capture after it. A fixed 50%
        here would therefore recentre a shifted window on the next Run --
        the position would appear to work, then undo itself.
        """
        offset = self.get_timebase_offset()
        percent = (_CENTRED_TRIGGER_PERCENT if not offset
                   else self._trigger_position_percent(offset))
        self._command("StartAcquisition", trigger_position_percent=percent)

    def trigger_force(self):
        """Trigger now rather than waiting for the configured condition."""
        self._command("ForceTrigger")
        return {"status": "triggered"}

    def is_ready(self) -> bool:
        """Whether a capture is available to read."""
        return bool(self._command("IsReady").get("is_ready"))

    def set_capture_mode(self, mode):
        token = _lookup(_CAPTURE_MODES, mode, "capture mode")
        self._command("SetCaptureMode", capture_mode=token)
        return {"capture_mode": token}

    def get_capture_mode(self):
        return self._command("GetCaptureMode").get("capture_mode")

    def autoscale(self):
        """Not available on PicoScope.

        The SDKs expose no autoset, and guessing one from a capture would give
        a different result than the Rigol's, so this reports the gap instead
        of pretending. Set a range explicitly with ``set_channel_scale``.
        """
        raise UnsupportedScopeFeature(
            "PicoScope has no autoscale; set volts/div and time/div explicitly")

    # ============ Channel Configuration ============
    def enable_channel(self, channel=None):
        self._command("EnableChannel", channel=self._require_channel(channel))
        return {"channel": channel_label(channel or self.channel), "enabled": True}

    def disable_channel(self, channel=None):
        self._command("DisableChannel", channel=self._require_channel(channel))
        return {"channel": channel_label(channel or self.channel), "enabled": False}

    # Rigol aliases.
    enable = enable_channel
    disable = disable_channel

    def get_channel_display(self, channel=None) -> bool:
        response = self._command("IsChannelEnabled",
                                 channel=self._require_channel(channel))
        return bool(response.get("is_enabled"))

    is_channel_enabled = get_channel_display

    def set_channel_scale(self, scale, channel=None):
        """Set vertical scale in volts per division."""
        self._command("SetVoltsPerDiv", channel=self._require_channel(channel),
                      volts_per_div=float(scale))
        return {"scale": float(scale)}

    def get_channel_scale(self, channel=None) -> float:
        response = self._command("GetVoltsPerDiv",
                                 channel=self._require_channel(channel))
        return float(response.get("volts_per_div"))

    def set_channel_offset(self, offset, channel=None):
        self._command("SetVoltsOffset", channel=self._require_channel(channel),
                      volts_offset=float(offset))
        return {"offset": float(offset)}

    def get_channel_offset(self, channel=None) -> float:
        response = self._command("GetVoltsOffset",
                                 channel=self._require_channel(channel))
        return float(response.get("volts_offset"))

    def set_channel_coupling(self, coupling, channel=None):
        token = _lookup(_COUPLINGS, coupling, "coupling")
        self._command("SetCoupling", channel=self._require_channel(channel),
                      coupling=token)
        return {"coupling": token}

    def get_channel_coupling(self, channel=None) -> str:
        response = self._command("GetCoupling",
                                 channel=self._require_channel(channel))
        return str(response.get("coupling"))

    def set_channel_probe(self, ratio, channel=None):
        """Set probe attenuation ratio (1 for 1x, 10 for 10x)."""
        self._command("SetAttenuation", channel=self._require_channel(channel),
                      attenuation=float(ratio))
        return {"probe": float(ratio)}

    def get_channel_probe(self, channel=None) -> float:
        response = self._command("GetAttenuation",
                                 channel=self._require_channel(channel))
        return float(response.get("attenuation"))

    # ============ Timebase ============
    def set_timebase_scale(self, scale):
        """Set horizontal scale in seconds per division."""
        self._command("SetTimePerDiv", time_per_div=float(scale))
        return {"scale": float(scale)}

    def get_timebase_scale(self) -> float:
        return float(self._command("GetTimePerDiv").get("time_per_div"))

    def set_timebase_offset(self, offset):
        """Shift the capture window later (positive) or earlier (negative).

        This is the horizontal position: it moves the window in time relative
        to the trigger, to bring signal that falls off the left or right edge
        of the screen into view.

        The daemon's own SetTimeOffset only stores the number -- nothing reads
        it back out -- so on its own it moves nothing. What actually moves the
        window is where the trigger sits inside the block, so both are sent:
        the offset so it can be read back, the split so it takes effect.
        Travel is one window each way, since a block is all the scope holds.
        """
        seconds = float(offset)
        percent = self._trigger_position_percent(seconds)
        self._command("SetTimeOffset", time_offset=seconds)
        # Re-arms: a window somewhere else has to be captured to be seen, and
        # the daemon keeps this split for every capture that follows.
        self._command("StartAcquisition", trigger_position_percent=percent)
        return {"offset": seconds, "trigger_position_percent": percent}

    def get_timebase_offset(self) -> float:
        # Defaulted rather than trusted: every arm reads this to keep the
        # window where it was put, so a daemon that answered without the
        # field would otherwise take Run down with it. No offset reported is
        # fairly read as no offset.
        offset = self._command("GetTimeOffset").get("time_offset")
        return float(offset) if offset is not None else 0.0

    def _trigger_position_percent(self, offset_seconds) -> float:
        """Where the trigger sits in the block, as a percentage from its start.

        The window spans one block, so an offset is a fraction of it: 50% puts
        the trigger in the middle, and looking `offset` further forward moves
        it that fraction to the left. At 0% the block is entirely after the
        trigger and at 100% entirely before, which is as far as it goes --
        beyond that needs a trigger delay the daemon does not expose.
        """
        span = self._capture_span_seconds()
        if not span:
            return _CENTRED_TRIGGER_PERCENT
        moved = _CENTRED_TRIGGER_PERCENT - (float(offset_seconds) / span) * 100.0
        return max(0.0, min(100.0, moved))

    def _capture_span_seconds(self):
        """How much time one capture covers, or None if it cannot be worked out."""
        rate = self.get_sample_rate()
        depth = self.get_memory_depth()
        if rate <= 0 or depth <= 0:
            return None
        return depth / rate

    def get_sample_rate(self) -> float:
        return float(self._command("GetSampleRate").get("sample_rate"))

    def get_memory_depth(self) -> int:
        return int(self._command("GetMemoryDepth").get("memory_depth"))

    def get_bandwidth(self) -> float:
        return float(self._command("GetBandwidth").get("bandwidth"))

    def get_channel_count(self) -> int:
        return int(self._command("GetChannelCount").get("channel_count"))

    # ============ Trigger ============
    def set_trigger_level(self, level, source=None):
        if source is not None:
            self.set_trigger_source(source)
        self._command("SetTriggerLevel", trigger_level=float(level))
        return {"level": float(level)}

    def get_trigger_level(self, source=None) -> float:
        return float(self._command("GetTriggerLevel").get("trigger_level"))

    def set_trigger_source(self, source):
        self._command("SetTriggerSource",
                      trigger_source=self._require_channel(source))
        return {"source": channel_label(source)}

    def get_trigger_source(self):
        response = self._command("GetTriggerSource")
        return channel_label(response.get("trigger_source"))

    def set_trigger_slope(self, slope):
        token = _lookup(_SLOPES, slope, "trigger slope")
        self._command("SetTriggerSlope", trigger_slope=token)
        return {"slope": token}

    def get_trigger_slope(self) -> str:
        return str(self._command("GetTriggerSlope").get("trigger_slope"))

    def set_trigger_coupling(self, coupling):
        """Not available on PicoScope.

        A Rigol filters the signal on its way to the trigger comparator --
        `:TRIGger:COUPling` takes DC, AC, LF-reject or HF-reject -- so a noisy
        or drifting edge can be triggered on without touching what is
        displayed. `ps2000_set_trigger` has no equivalent parameter: the
        comparator sees the channel as it is.

        Raised rather than ignored because the two nearby ways of being
        helpful are both wrong. Silently doing nothing leaves a scope that
        will not trigger and a setting that claims to have been applied; and
        falling through to the channel's input coupling, which is what this
        used to do, moves the trace on screen instead.
        """
        raise UnsupportedScopeFeature(
            "PicoScope has no trigger coupling filter; for the channel's "
            "input coupling use set_channel_coupling (`lager scope <net> "
            "coupling ac`)")

    def get_trigger_coupling(self):
        """Not available on PicoScope. See ``set_trigger_coupling``."""
        raise UnsupportedScopeFeature(
            "PicoScope has no trigger coupling filter; for the channel's "
            "input coupling use get_channel_coupling")

    # Rigol edge-trigger aliases: a PicoScope has only edge triggers on the
    # 2000 series, so edge and generic trigger are the same setting.
    set_trigger_edge_level = set_trigger_level
    get_trigger_edge_level = get_trigger_level
    set_trigger_edge_source = set_trigger_source
    get_trigger_edge_source = get_trigger_source
    set_trigger_edge_slope = set_trigger_slope
    get_trigger_edge_slope = get_trigger_slope

    # ============ Measurements ============
    def measure(self, item, channel=None):
        """Measure one quantity, by daemon or Rigol name."""
        name = _MEASURE_ITEMS.get(str(item).strip().lower(), str(item).strip().lower())
        response = self._command("Measure", channel=self._require_channel(channel),
                                 measurement=name)
        value = response.get("value")
        if value is None:
            raise UnsupportedScopeFeature(
                "%s is not present in this capture (a period needs at least two "
                "full cycles on screen)" % name)
        return float(value)

    def measure_all(self, channel=None) -> dict:
        """Every measurement from a single capture.

        Cheaper than several ``measure`` calls, which each take their own
        capture.
        """
        response = self._command("Measure", channel=self._require_channel(channel))
        return response.get("measurements") or {}

    def measure_frequency(self, channel=None) -> float:
        return self.measure("frequency", channel)

    def measure_period(self, channel=None) -> float:
        return self.measure("period", channel)

    def measure_vpp(self, channel=None) -> float:
        return self.measure("vpp", channel)

    def measure_vmax(self, channel=None) -> float:
        return self.measure("vmax", channel)

    def measure_vmin(self, channel=None) -> float:
        return self.measure("vmin", channel)

    def measure_vrms(self, channel=None) -> float:
        return self.measure("vrms", channel)

    def measure_vavg(self, channel=None) -> float:
        return self.measure("vavg", channel)

    def measure_duty_cycle(self, channel=None) -> float:
        return self.measure("duty_cycle_pos", channel)

    def measure_rise_time(self, channel=None) -> float:
        return self.measure("rise_time", channel)

    def measure_fall_time(self, channel=None) -> float:
        return self.measure("fall_time", channel)

    def get_measure_item(self, item, channel=None) -> float:
        """Rigol-compatible measurement accessor."""
        return self.measure(item, channel)

    # ============ Cursors ============
    #
    # Driven by typing, in either CLI, with nothing to drag. A Rigol's cursors
    # are markers on the instrument's own screen, read back over SCPI; a
    # PicoScope has no screen, so these are markers over the captured samples
    # instead. They live on the box so that both CLIs and the web UI are
    # looking at one set: place a cursor with `lager scope`, and the plot in
    # the browser draws it.

    def set_cursors(self, time=None, volts=None, channel=None) -> dict:
        """Place the time pair, the voltage pair, or both.

        Each pair is independent and only the ones given are touched, so
        moving the time cursors does not clear voltage cursors set earlier.
        """
        if time is not None:
            if len(time) != 2:
                raise ValueError(
                    "time cursors come in a pair, got %r" % (time,))
            self._cursors["time"] = (float(time[0]), float(time[1]))
        if volts is not None:
            if len(volts) != 2:
                raise ValueError(
                    "voltage cursors come in a pair, got %r" % (volts,))
            self._cursors["volts"] = (float(volts[0]), float(volts[1]))
        if channel is not None:
            self._cursors["channel"] = channel_label(channel)
        return self.get_cursors()

    def get_cursors(self) -> dict:
        """Where the cursors are, without taking a capture to read them.

        The channel is always reported, defaulting to this net's own, because
        the voltage readings depend on it: whoever draws these has to put them
        on the same trace they were measured against.
        """
        return {
            "time": list(self._cursors["time"]) if self._cursors["time"] else None,
            "volts": list(self._cursors["volts"]) if self._cursors["volts"] else None,
            "channel": self._cursors.get("channel") or channel_label(self.channel),
        }

    def clear_cursors(self) -> dict:
        self._cursors = {"time": None, "volts": None}
        return self.get_cursors()

    def measure_cursors(self, channel=None, timeout: float | None = None) -> dict:
        """Read the cursors against a capture.

        Time cursors need the samples, since the useful part is the voltage
        of the trace under each one; voltage cursors are arithmetic and need
        no capture. So one is taken only when there is a time pair to read,
        which keeps `cursor volts` free.
        """
        cursors = self.get_cursors()
        if not cursors["time"] and not cursors["volts"]:
            return {"cursors": cursors, "readings": {}}

        times = trace = None
        if cursors["time"]:
            label = channel_label(channel) if channel else cursors["channel"]
            cursors["channel"] = label
            frame = self._capture_for_cursors(timeout)
            # A disabled channel is absent from the capture rather than
            # present and empty, and asking the frame for it reports the
            # channel as though it did not exist on the scope. Said the way
            # the daemon says it for measurements, since the cause is the
            # same and so is the fix.
            if frame.channel_index(label) is None:
                raise UnsupportedScopeFeature(
                    "channel %s is not enabled, so there is nothing for the "
                    "cursors to read" % label)
            times = frame.time_axis()
            trace = frame.volts(label)

        return {
            "cursors": cursors,
            "readings": cursor_readings(cursors["time"], cursors["volts"],
                                        times, trace),
        }

    def _capture_for_cursors(self, timeout=None):
        """A capture to read cursors against, arming if nothing is coming.

        Read from a running acquisition where there is one, so a cursor
        reading matches the trace on screen and does not interrupt a stream.
        Cold, there is nothing to wait for, so it arms and asks again rather
        than blocking until the timeout and reporting a dead scope.
        """
        wait = timeout if timeout is not None else 2.0
        try:
            return self.capture(timeout=wait)
        except daemon_client.ScopeDaemonError:
            self._start_acquisition()
            return self.capture(timeout=wait)

    # ============ Waveform capture ============
    def capture(self, timeout: float | None = None):
        """One triggered capture, decoded into an ``lscp.CaptureFrame``."""
        return self.client.capture(timeout=timeout)

    def stream_start(self, channel=None, volts_per_div=None, time_per_div=None,
                     trigger_level=None, trigger_slope=None, capture_mode=None,
                     coupling=None, channels=None):
        """Configure and arm streaming, then return the capture parameters.

        The counterpart to ``stream_capture``. Every setting is optional and
        only the ones given are applied, so arming with a different trigger
        level does not quietly reset the coupling somebody set earlier.

        ``channel`` takes a single channel, matching the documented API and
        the ``lager scope stream start`` flags. ``channels`` takes several,
        for a multi-channel capture; passing both enables the union.
        """
        selected = []
        for value in ([channel] if channel is not None else []) + list(channels or []):
            if value not in selected:
                selected.append(value)
        if not selected:
            selected = [self.channel]

        for target in selected:
            self.enable_channel(target)
            if volts_per_div is not None:
                self.set_channel_scale(volts_per_div, target)
            if coupling is not None:
                self.set_channel_coupling(coupling, target)

        if time_per_div is not None:
            self.set_timebase_scale(time_per_div)
        if trigger_slope is not None:
            self.set_trigger_slope(trigger_slope)
        if trigger_level is not None:
            # The level is in volts on whichever channel triggers, so the
            # source is pointed at the first selected channel first.
            self.set_trigger_source(selected[0])
            self.set_trigger_level(trigger_level)

        self._command("SetCaptureMode",
                      capture_mode=_lookup(_CAPTURE_MODES, capture_mode or "auto",
                                           "capture mode"))
        self._start_acquisition()
        return {
            "channels": [channel_label(c) for c in selected],
            "sample_rate": self.get_sample_rate(),
            "memory_depth": self.get_memory_depth(),
        }

    def subscribe(self):
        """Receive captures pushed on this connection as they are acquired.

        For a consumer that wants every capture. ``stream_capture`` does not
        need it -- that asks for one capture at a time -- and a control-only
        caller should not subscribe, since the pushed frames then share the
        socket with its command replies.
        """
        self._command("Subscribe")
        return {"subscribed": True}

    def unsubscribe(self):
        self._command("Unsubscribe")
        return {"subscribed": False}

    def stream_frames(self, count: int = 1, timeout: float | None = None):
        """Yield ``count`` captures as decoded ``lscp.CaptureFrame`` objects.

        The zero-copy path: a frame's ``counts()`` is a view over the
        received buffer, so nothing is converted until the caller asks. Use
        this over ``stream_capture`` when the samples are going into numpy
        rather than onto disk.
        """
        for _ in range(max(1, int(count))):
            yield self.capture(timeout=timeout)

    def stream_capture(self, output=None, duration: float = 1.0, samples=None,
                       timeout: float | None = None) -> dict:
        """Capture for ``duration`` seconds, optionally writing a CSV.

        Returns a summary rather than the samples themselves, because the
        point of a duration-bounded capture is usually the file. For the
        samples in memory, iterate ``stream_frames`` instead.

        ``samples`` caps the rows per channel, so a long duration on a fast
        timebase cannot fill the disk unnoticed. It is a cap, not a target:
        the capture still stops at ``duration``.
        """
        if duration is not None and duration <= 0:
            raise ValueError("duration must be positive, got %r" % (duration,))
        if samples is not None and samples <= 0:
            raise ValueError("samples must be positive, got %r" % (samples,))

        deadline = time.monotonic() + float(duration)
        rows = []
        captures = 0
        per_channel = 0

        while time.monotonic() < deadline:
            if samples is not None and per_channel >= samples:
                break
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            try:
                frame = self.capture(timeout=timeout if timeout is not None
                                     else remaining)
            except daemon_client.ScopeDaemonError:
                # A capture that does not arrive before the deadline ends the
                # run with whatever was collected, rather than failing and
                # discarding it.
                break

            interval_ns = frame.sample_interval_ns
            take = frame.samples_per_channel
            if samples is not None:
                take = min(take, samples - per_channel)

            if output is not None:
                for index, descriptor in enumerate(frame.channels):
                    volts = frame.volts(index)
                    for i in range(take):
                        rows.append((captures, descriptor.channel, i,
                                     i * interval_ns, float(volts[i])))

            per_channel += take
            captures += 1

        if output is not None:
            with open(output, "w", newline="") as handle:
                writer = csv.writer(handle)
                writer.writerow(["capture", "channel", "sample_index",
                                 "time_ns", "voltage"])
                writer.writerows(rows)

        return {
            "captures": captures,
            "samples_per_channel": per_channel,
            "rows": len(rows),
            "output": output,
        }

    def stream_stop(self):
        return self.stop()


def create_device(net_info=None, **kwargs):
    """hardware_service factory entry point."""
    info = net_info or {}
    return PicoScope(
        address=info.get("address"),
        pin=info.get("pin") or info.get("channel"),
        netname=info.get("name"),
    )
