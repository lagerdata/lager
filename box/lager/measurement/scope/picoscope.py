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

import logging

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
        self._command("StartAcquisition", trigger_position_percent=50.0)
        return {"status": "running"}

    def stop(self):
        """Stop acquisition."""
        self._command("StopAcquisition")
        return {"status": "stopped"}

    def single(self):
        """Arm for a single acquisition."""
        self._command("SetCaptureMode", capture_mode="single")
        self._command("StartAcquisition", trigger_position_percent=50.0)
        return {"status": "single"}

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
        self._command("SetTimeOffset", time_offset=float(offset))
        return {"offset": float(offset)}

    def get_timebase_offset(self) -> float:
        return float(self._command("GetTimeOffset").get("time_offset"))

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

    # ============ Waveform capture ============
    def capture(self, timeout: float | None = None):
        """One triggered capture, decoded into an ``lscp.CaptureFrame``."""
        return self.client.capture(timeout=timeout)

    def stream_start(self, channels=None, volts_per_div=None, time_per_div=None):
        """Configure and arm streaming, then return the capture parameters.

        The counterpart to ``stream_capture``. Documented in the Python
        reference for a while before it existed; this is the implementation.
        """
        selected = list(channels or [self.channel])
        for channel in selected:
            self.enable_channel(channel)
            if volts_per_div is not None:
                self.set_channel_scale(volts_per_div, channel)
        if time_per_div is not None:
            self.set_timebase_scale(time_per_div)

        self._command("SetCaptureMode", capture_mode="auto")
        self._command("StartAcquisition", trigger_position_percent=50.0)
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

    def stream_capture(self, count: int = 1, timeout: float | None = None):
        """Yield ``count`` captures as decoded frames."""
        for _ in range(max(1, int(count))):
            yield self.capture(timeout=timeout)

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
