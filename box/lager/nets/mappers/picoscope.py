# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

"""Net API for a PicoScope, mirroring the Rigol MSO5000 mapper.

A scope net used to resolve to ``PassThroughMapper``, which forwarded every
attribute to a ``Device`` named ``picoscope_2000``. No such backend module
exists, so ``Net.get('scope1').run()`` raised "Hardware module not found" --
the Python API for a PicoScope did not work at all. This routes to the
``scope_hs`` backend the CLI already drives and puts the same grouped surface
over it that the Rigol has, so a script written against one scope reads the
same against the other::

    scope = Net.get('scope1', type=NetType.Analog)
    scope.start_capture()
    print(scope.measurement.voltage_peak_to_peak())
    scope.trace_settings.set_volts_per_div(0.5)

The net's own channel is the default for every per-channel call, which is
what makes ``scope.measurement.voltage_max()`` mean "on this net" the way it
does for a Rigol.

Where a PicoScope genuinely cannot do something the Rigol can -- cursors,
protocol triggers, the logic analyzer -- the method raises
``UnsupportedScopeFeature`` naming the gap. That is deliberate: returning
zero or silently doing nothing turns a missing feature into wrong data.
"""
from __future__ import annotations

from ...measurement.scope.picoscope import UnsupportedScopeFeature


def _unsupported(what, instead=None):
    """Build a raiser for a feature a PicoScope does not have."""
    message = "a PicoScope has no %s" % what
    if instead:
        message += "; %s" % instead

    def raiser(self, *args, **kwargs):
        raise UnsupportedScopeFeature(message)

    return raiser


class _PicoScopeSubMapper:
    """Shared plumbing: hold the net and device, forward the rest."""

    def __init__(self, parent, net, device):
        self.parent = parent
        self.net = net
        self.device = device

    @property
    def _channel(self):
        """The channel this net is wired to, or A if the net has none."""
        return getattr(self.net, "channel", None)

    def __getattr__(self, attr):
        return getattr(self.device, attr)


class TraceSettings_PicoScopeFunctionMapper(_PicoScopeSubMapper):
    """Vertical and horizontal scaling, scoped to this net's channel."""

    def set_volts_per_div(self, volts):
        return self.device.set_channel_scale(volts, self._channel)

    def get_volts_per_div(self) -> float:
        return float(self.device.get_channel_scale(self._channel))

    def set_volt_offset(self, offset):
        return self.device.set_channel_offset(offset, self._channel)

    def get_volt_offset(self) -> float:
        return float(self.device.get_channel_offset(self._channel))

    def set_time_per_div(self, time):
        return self.device.set_timebase_scale(time)

    def get_time_per_div(self) -> float:
        return float(self.device.get_timebase_scale())

    def set_time_offset(self, time):
        return self.device.set_timebase_offset(time)

    def get_time_offset(self) -> float:
        return float(self.device.get_timebase_offset())


class TriggerSettingsEdge_PicoScopeFunctionMapper(_PicoScopeSubMapper):
    """Edge trigger, the only kind these units offer."""

    def set_source(self, source=None):
        return self.device.set_trigger_source(
            source if source is not None else self._channel)

    def get_source(self):
        return self.device.get_trigger_source()

    def set_slope_rising(self):
        return self.device.set_trigger_slope("rising")

    def set_slope_falling(self):
        return self.device.set_trigger_slope("falling")

    def set_slope_both(self):
        return self.device.set_trigger_slope("either")

    def get_slope(self):
        return self.device.get_trigger_slope()

    def set_level(self, level):
        return self.device.set_trigger_level(level)

    def get_level(self) -> float:
        return float(self.device.get_trigger_level())


class TriggerSettings_PicoScopeFunctionMapper(_PicoScopeSubMapper):
    """Trigger mode and edge configuration."""

    def __init__(self, parent, net, device):
        super().__init__(parent, net, device)
        self.edge = TriggerSettingsEdge_PicoScopeFunctionMapper(parent, net, device)

    def set_mode_auto(self):
        return self.device.set_capture_mode("auto")

    def set_mode_normal(self):
        return self.device.set_capture_mode("normal")

    def set_mode_single(self):
        return self.device.set_capture_mode("single")

    def get_mode(self):
        return self.device.get_capture_mode()

    def get_status(self):
        """Whether a capture is waiting to be read.

        The Rigol reports a trigger state machine (``WAIT``/``AUTO``/``RUN``)
        that the PicoTech SDKs do not expose. Readiness is the part a script
        polls for, so that is what this answers.
        """
        return "READY" if self.device.is_ready() else "WAIT"

    # Input coupling on a PicoScope is per channel and set through
    # trace-level configuration; the Rigol additionally has a coupling filter
    # on the trigger path itself, which these units do not.
    set_coupling_DC = _unsupported(
        "trigger coupling filter",
        "set the channel's coupling with set_channel_coupling('dc')")
    set_coupling_AC = _unsupported(
        "trigger coupling filter",
        "set the channel's coupling with set_channel_coupling('ac')")
    set_coupling_low_freq_reject = _unsupported("trigger coupling filter")
    set_coupling_high_freq_reject = _unsupported("trigger coupling filter")
    get_coupling = _unsupported("trigger coupling filter")


class Measurement_PicoScopeFunctionMapper(_PicoScopeSubMapper):
    """Measurements, computed by the daemon from a captured block.

    Every one of these takes its own capture, so reading several costs
    several captures. ``device.measure_all()`` returns the whole set from one.

    The Rigol accepts ``display`` and ``measurement_cursor`` to put a reading
    on the instrument's screen. A PicoScope has no screen, so those are
    accepted and ignored rather than rejected -- a script that runs against
    both scopes should not have to strip the arguments.
    """

    def _measure(self, name, display=False, measurement_cursor=False):
        return self.device.measure(name, self._channel)

    # -- voltage ---------------------------------------------------------
    def voltage_max(self, *, display=False, measurement_cursor=False):
        return self._measure("vmax", display, measurement_cursor)

    def voltage_min(self, *, display=False, measurement_cursor=False):
        return self._measure("vmin", display, measurement_cursor)

    def voltage_peak_to_peak(self, *, display=False, measurement_cursor=False):
        return self._measure("vpp", display, measurement_cursor)

    def voltage_average(self, *, display=False, measurement_cursor=False):
        return self._measure("vavg", display, measurement_cursor)

    def voltage_rms(self, *, display=False, measurement_cursor=False):
        return self._measure("vrms", display, measurement_cursor)

    def voltage_overshoot(self, *, display=False, measurement_cursor=False):
        return self._measure("overshoot", display, measurement_cursor)

    # -- timing ----------------------------------------------------------
    def frequency(self, *, display=False, measurement_cursor=False):
        return self._measure("frequency", display, measurement_cursor)

    def period(self, *, display=False, measurement_cursor=False):
        return self._measure("period", display, measurement_cursor)

    def rise_time(self, *, display=False, measurement_cursor=False):
        return self._measure("rise_time", display, measurement_cursor)

    def fall_time(self, *, display=False, measurement_cursor=False):
        return self._measure("fall_time", display, measurement_cursor)

    def pulse_width_positive(self, *, display=False, measurement_cursor=False):
        return self._measure("pulse_width_pos", display, measurement_cursor)

    def pulse_width_negative(self, *, display=False, measurement_cursor=False):
        return self._measure("pulse_width_neg", display, measurement_cursor)

    def duty_cycle_positive(self, *, display=False, measurement_cursor=False):
        return self._measure("duty_cycle_pos", display, measurement_cursor)

    def duty_cycle_negative(self, *, display=False, measurement_cursor=False):
        return self._measure("duty_cycle_neg", display, measurement_cursor)

    def all(self) -> dict:
        """Every measurement the daemon computes, from a single capture."""
        return self.device.measure_all(self._channel)

    # -- what the daemon does not compute --------------------------------
    #
    # These are Rigol readings with no PicoScope equivalent. The SDKs return
    # samples and nothing else, so anything here would have to be derived in
    # the daemon; until it is, saying so beats returning a plausible zero.
    _NO_EQUIVALENT = "the daemon does not compute this from a capture yet"

    voltage_flat_top = _unsupported("flat-top measurement", _NO_EQUIVALENT)
    voltage_flat_base = _unsupported("flat-base measurement", _NO_EQUIVALENT)
    voltage_flat_amplitude = _unsupported("flat-amplitude measurement", _NO_EQUIVALENT)
    voltage_threshold_upper = _unsupported("threshold measurement", _NO_EQUIVALENT)
    voltage_threshold_lower = _unsupported("threshold measurement", _NO_EQUIVALENT)
    voltage_threshold_mid = _unsupported("threshold measurement", _NO_EQUIVALENT)
    voltage_preshoot = _unsupported("preshoot measurement", _NO_EQUIVALENT)
    voltage_rms_period = _unsupported("per-period RMS", _NO_EQUIVALENT)
    time_at_voltage_max = _unsupported("time-at-maximum measurement", _NO_EQUIVALENT)
    time_at_voltage_min = _unsupported("time-at-minimum measurement", _NO_EQUIVALENT)
    positive_slew_rate = _unsupported("slew-rate measurement", _NO_EQUIVALENT)
    negative_slew_rate = _unsupported("slew-rate measurement", _NO_EQUIVALENT)
    positive_edge_count = _unsupported("edge counter", _NO_EQUIVALENT)
    negative_edge_count = _unsupported("edge counter", _NO_EQUIVALENT)
    positive_pulse_count = _unsupported("pulse counter", _NO_EQUIVALENT)
    negative_pulse_count = _unsupported("pulse counter", _NO_EQUIVALENT)
    waveform_area = _unsupported("area measurement", _NO_EQUIVALENT)
    waveform_period_area = _unsupported("area measurement", _NO_EQUIVALENT)
    variance = _unsupported("variance measurement", _NO_EQUIVALENT)

    # Delay and phase compare two channels against each other, which needs a
    # two-source measurement the daemon has no command for.
    _TWO_SOURCE = "delay and phase need a two-channel measurement the daemon has no command for"
    delay_rising_rising_edge = _unsupported("delay measurement", _TWO_SOURCE)
    delay_rising_falling_edge = _unsupported("delay measurement", _TWO_SOURCE)
    delay_falling_rising_edge = _unsupported("delay measurement", _TWO_SOURCE)
    delay_falling_falling_edge = _unsupported("delay measurement", _TWO_SOURCE)
    phase_rising_rising_edge = _unsupported("phase measurement", _TWO_SOURCE)
    phase_rising_falling_edge = _unsupported("phase measurement", _TWO_SOURCE)
    phase_falling_rising_edge = _unsupported("phase measurement", _TWO_SOURCE)
    phase_falling_falling_edge = _unsupported("phase measurement", _TWO_SOURCE)


class Cursor_PicoScopeFunctionMapper(_PicoScopeSubMapper):
    """Absent by construction.

    Rigol cursors are markers drawn on the instrument's own display and read
    back over SCPI. A PicoScope has no display, so there is nothing to place
    or read. The web UI draws its own cursors client-side over the captured
    samples, which is where this belongs.
    """

    _NO_SCREEN = "measure from the capture, or use the cursors in the web UI"

    set_a = _unsupported("on-screen cursors", _NO_SCREEN)
    set_b = _unsupported("on-screen cursors", _NO_SCREEN)
    get_a = _unsupported("on-screen cursors", _NO_SCREEN)
    get_b = _unsupported("on-screen cursors", _NO_SCREEN)
    move_a = _unsupported("on-screen cursors", _NO_SCREEN)
    move_b = _unsupported("on-screen cursors", _NO_SCREEN)
    x_delta = _unsupported("on-screen cursors", _NO_SCREEN)
    y_delta = _unsupported("on-screen cursors", _NO_SCREEN)
    frequency = _unsupported("on-screen cursors", _NO_SCREEN)
    a_x = _unsupported("on-screen cursors", _NO_SCREEN)
    a_y = _unsupported("on-screen cursors", _NO_SCREEN)
    b_x = _unsupported("on-screen cursors", _NO_SCREEN)
    b_y = _unsupported("on-screen cursors", _NO_SCREEN)
    hide = _unsupported("on-screen cursors", _NO_SCREEN)


class PicoScopeAnalogMapper:
    """What ``Net.get(name, type=NetType.Analog)`` returns for a PicoScope."""

    def __init__(self, net, device):
        self.net = net
        self.device = device
        self.measurement = Measurement_PicoScopeFunctionMapper(self, net, device)
        self.trigger_settings = TriggerSettings_PicoScopeFunctionMapper(self, net, device)
        self.trace_settings = TraceSettings_PicoScopeFunctionMapper(self, net, device)
        self.cursor = Cursor_PicoScopeFunctionMapper(self, net, device)

    # -- acquisition, named as the Rigol mapper names it -----------------
    def start_capture(self):
        return self.device.run()

    def stop_capture(self):
        return self.device.stop()

    def start_single_capture(self):
        return self.device.single()

    def force_trigger(self):
        return self.device.trigger_force()

    def autoscale(self):
        # The driver raises with an explanation; going through it keeps the
        # message in one place.
        return self.device.autoscale()

    def __getattr__(self, attr):
        return getattr(self.device, attr)
