# Copyright 2024-2026 Lager Data LLC
# SPDX-License-Identifier: Apache-2.0

"""
    Scope commands (analog nets using local nets)
"""
from __future__ import annotations

import json

import click
from ...context import get_impl_path, get_default_net
from ..development.python import run_python_internal
from ...core.net_helpers import (
    require_netname,
    resolve_box,
    run_net_py,
    validate_net,
    validate_net_exists,
    display_nets,
    run_backend,
)

SCOPE_ROLE = "scope"

# Validation constants for oscilloscope settings
# These are typical ranges for bench oscilloscopes (Rigol MSO5000, PicoScope, etc.)
MIN_VOLTS_PER_DIV = 0.001   # 1mV/div minimum
MAX_VOLTS_PER_DIV = 100.0   # 100V/div maximum
MIN_TIME_PER_DIV = 1e-9     # 1ns/div minimum
MAX_TIME_PER_DIV = 50.0     # 50s/div maximum
MIN_CAPTURE_DURATION = 0.001  # 1ms minimum capture
MAX_CAPTURE_DURATION = 3600.0  # 1 hour maximum capture
MIN_SAMPLES = 1
MAX_SAMPLES = 100_000_000   # 100M samples max


def _validate_scale(ctx, volts_per_div: float) -> None:
    """Validate volts per division is within acceptable range."""
    if volts_per_div < MIN_VOLTS_PER_DIV or volts_per_div > MAX_VOLTS_PER_DIV:
        click.secho(
            f"Error: Volts/div must be between {MIN_VOLTS_PER_DIV} and {MAX_VOLTS_PER_DIV}, got {volts_per_div}",
            fg='red', err=True
        )
        click.secho(f"  Typical values: 0.001 (1mV), 0.1 (100mV), 1.0 (1V), 10.0 (10V)", err=True)
        ctx.exit(1)


def _validate_timebase(ctx, seconds_per_div: float) -> None:
    """Validate timebase is within acceptable range."""
    if seconds_per_div < MIN_TIME_PER_DIV or seconds_per_div > MAX_TIME_PER_DIV:
        click.secho(
            f"Error: Timebase must be between {MIN_TIME_PER_DIV} and {MAX_TIME_PER_DIV} seconds/div, got {seconds_per_div}",
            fg='red', err=True
        )
        click.secho(f"  Typical values: 0.000001 (1us), 0.001 (1ms), 0.1 (100ms), 1.0 (1s)", err=True)
        ctx.exit(1)


def _validate_duration(ctx, duration: float) -> None:
    """Validate capture duration is within acceptable range."""
    if duration < MIN_CAPTURE_DURATION or duration > MAX_CAPTURE_DURATION:
        click.secho(
            f"Error: Duration must be between {MIN_CAPTURE_DURATION} and {MAX_CAPTURE_DURATION} seconds, got {duration}",
            fg='red', err=True
        )
        ctx.exit(1)


def _validate_samples(ctx, samples: int) -> None:
    """Validate sample count is within acceptable range."""
    if samples < MIN_SAMPLES or samples > MAX_SAMPLES:
        click.secho(
            f"Error: Sample count must be between {MIN_SAMPLES} and {MAX_SAMPLES:,}, got {samples:,}",
            fg='red', err=True
        )
        ctx.exit(1)


def _is_picoscope(instrument: str) -> bool:
    """Check if instrument is a PicoScope (case-insensitive)."""
    return "picoscope" in instrument.lower()


# ---------- helpers ----------

def _require_netname(ctx) -> str:
    return require_netname(ctx, "scope")


def _resolve_box(ctx, box):
    return resolve_box(ctx, box)


def _validate_scope_net(ctx, box_ip: str, netname: str) -> dict | None:
    """
    Validate that a net exists and is a scope net.

    Returns the net dict if valid, None otherwise.
    Error message with available nets is displayed if validation fails.
    """
    return validate_net_exists(ctx, box_ip, netname, SCOPE_ROLE)


def _run_backend(ctx, dut, action: str, **params):
    """Run backend command for scope operations"""
    return run_backend(ctx, dut, "scope.py", action, **params)


# ---------- CLI ----------

@click.group(invoke_without_command=True)
@click.argument("NETNAME", required=False)
@click.pass_context
@click.option("--box", required=False, help="Lagerbox name or IP")
def scope(ctx, box, netname):
    """Control oscilloscope settings"""
    # Use provided netname, or fall back to default if not provided
    if netname is None:
        netname = get_default_net(ctx, 'scope')

    if netname is not None:
        ctx.obj.netname = netname

    if ctx.invoked_subcommand is None:
        box_ip = _resolve_box(ctx, box)
        display_nets(ctx, box_ip, None, SCOPE_ROLE, "scope")


@scope.command()
@click.pass_context
@click.option("--box", required=False, help="Lagerbox name or IP")
@click.option("--mcu", required=False)
def disable(ctx, box, mcu):
    """Disable scope channel"""
    box_ip = _resolve_box(ctx, box)
    netname = _require_netname(ctx)

    if _validate_scope_net(ctx, box_ip, netname) is None:
        return  # Error already displayed with available nets

    _run_backend(ctx, box_ip, "disable_net", netname=netname, mcu=mcu)


@scope.command()
@click.pass_context
@click.option("--box", required=False, help="Lagerbox name or IP")
@click.option("--mcu", required=False)
def enable(ctx, box, mcu):
    """Enable scope channel"""
    box_ip = _resolve_box(ctx, box)
    netname = _require_netname(ctx)

    if _validate_scope_net(ctx, box_ip, netname) is None:
        return  # Error already displayed with available nets

    _run_backend(ctx, box_ip, "enable_net", netname=netname, mcu=mcu)


@scope.command()
@click.pass_context
@click.option("--box", required=False, help="Lagerbox name or IP")
@click.option("--mcu", required=False)
@click.option("--single", is_flag=True, help="Capture single waveform then stop")
def start(ctx, box, mcu, single):
    """Start waveform capture (continuous or single)"""
    box_ip = _resolve_box(ctx, box)
    netname = _require_netname(ctx)

    if _validate_scope_net(ctx, box_ip, netname) is None:
        return  # Error already displayed with available nets

    action = "start_single" if single else "start_capture"
    _run_backend(ctx, box_ip, action, netname=netname, mcu=mcu)


@scope.command()
@click.pass_context
@click.option("--box", required=False, help="Lagerbox name or IP")
@click.option("--mcu", required=False)
def stop(ctx, box, mcu):
    """Stop waveform capture"""
    box_ip = _resolve_box(ctx, box)
    netname = _require_netname(ctx)

    if _validate_scope_net(ctx, box_ip, netname) is None:
        return  # Error already displayed with available nets

    _run_backend(ctx, box_ip, "stop_capture", netname=netname, mcu=mcu)


@scope.command()
@click.pass_context
@click.option("--box", required=False, help="Lagerbox name or IP")
@click.option("--mcu", required=False)
def force(ctx, box, mcu):
    """Force trigger manually (bypass trigger condition)"""
    box_ip = _resolve_box(ctx, box)
    netname = _require_netname(ctx)

    if _validate_scope_net(ctx, box_ip, netname) is None:
        return  # Error already displayed with available nets

    _run_backend(ctx, box_ip, "force_trigger", netname=netname, mcu=mcu)


@scope.command()
@click.argument("volts_per_div", type=float)
@click.pass_context
@click.option("--box", required=False, help="Lagerbox name or IP")
@click.option("--mcu", required=False)
def scale(ctx, volts_per_div, box, mcu):
    """
    Set vertical scale (volts per division)
    """
    box_ip = _resolve_box(ctx, box)
    netname = _require_netname(ctx)

    # Validate scale range
    _validate_scale(ctx, volts_per_div)

    if _validate_scope_net(ctx, box_ip, netname) is None:
        return  # Error already displayed with available nets

    _run_backend(ctx, box_ip, "set_scale", netname=netname, mcu=mcu,
                 volts_per_div=volts_per_div)


CHANNEL_COUPLING_CHOICES = click.Choice(("dc", "ac", "gnd"))


@scope.command()
@click.argument("mode", type=CHANNEL_COUPLING_CHOICES)
@click.pass_context
@click.option("--box", required=False, help="Lagerbox name or IP")
@click.option("--mcu", required=False)
def coupling(ctx, mode, box, mcu):
    """
    Set channel coupling mode (dc, ac, or gnd)
    """
    box_ip = _resolve_box(ctx, box)
    netname = _require_netname(ctx)

    if _validate_scope_net(ctx, box_ip, netname) is None:
        return  # Error already displayed with available nets

    _run_backend(ctx, box_ip, "set_coupling", netname=netname, mcu=mcu, mode=mode)


PROBE_CHOICES = click.Choice(("1", "10", "100", "1000"))


@scope.command()
@click.argument("ratio", type=PROBE_CHOICES)
@click.pass_context
@click.option("--box", required=False, help="Lagerbox name or IP")
@click.option("--mcu", required=False)
def probe(ctx, ratio, box, mcu):
    """
    Set probe attenuation ratio (1x, 10x, 100x, 1000x)
    """
    box_ip = _resolve_box(ctx, box)
    netname = _require_netname(ctx)

    if _validate_scope_net(ctx, box_ip, netname) is None:
        return  # Error already displayed with available nets

    _run_backend(ctx, box_ip, "set_probe", netname=netname, mcu=mcu, ratio=int(ratio))


@scope.command()
@click.argument("seconds_per_div", type=float)
@click.pass_context
@click.option("--box", required=False, help="Lagerbox name or IP")
@click.option("--mcu", required=False)
def timebase(ctx, seconds_per_div, box, mcu):
    """
    Set horizontal timebase (seconds per division)
    """
    box_ip = _resolve_box(ctx, box)
    netname = _require_netname(ctx)

    # Validate timebase range
    _validate_timebase(ctx, seconds_per_div)

    if _validate_scope_net(ctx, box_ip, netname) is None:
        return  # Error already displayed with available nets

    _run_backend(ctx, box_ip, "set_timebase", netname=netname, mcu=mcu,
                 seconds_per_div=seconds_per_div)


@scope.command()
@click.pass_context
@click.option("--box", required=False, help="Lagerbox name or IP")
@click.option("--mcu", required=False)
def autoscale(ctx, box, mcu):
    """
    Automatically adjust vertical scale and timebase (Rigol only)
    """
    box_ip = _resolve_box(ctx, box)
    netname = _require_netname(ctx)

    if _validate_scope_net(ctx, box_ip, netname) is None:
        return  # Error already displayed with available nets

    click.echo("Running autoscale (this may take 10-15 seconds)...")
    _run_backend(ctx, box_ip, "autoscale", netname=netname, mcu=mcu)


@scope.group()
def measure():
    """Measure waveform characteristics (Rigol only)"""
    pass


@measure.command()
@click.pass_context
@click.option("--mcu", required=False)
@click.option("--box", required=False, help="Lagerbox name or IP")
@click.option("--display", is_flag=True, help="Display measurement on screen")
@click.option("--cursor", is_flag=True, help="Enable measurement cursor")
def period(ctx, mcu, box, display, cursor):
    """Measure waveform period"""
    box_ip = _resolve_box(ctx, box)
    netname = _require_netname(ctx)

    if _validate_scope_net(ctx, box_ip, netname) is None:
        return  # Error already displayed with available nets

    data = {
        "action": "measure_period",
        "mcu": mcu,
        "params": {
            "netname": netname,
            "display": display,
            "cursor": cursor
        }
    }

    run_python_internal(
        ctx,
        get_impl_path("scope.py"),
        box_ip,
        env=(f"LAGER_COMMAND_DATA={json.dumps(data)}",),
        passenv=(),
        kill=False,
        download=(),
        allow_overwrite=False,
        signum="SIGTERM",
        timeout=0,
        detach=False,
        port=(),
        org=None,
        args=(),
    )


@measure.command()
@click.pass_context
@click.option("--mcu", required=False)
@click.option("--box", required=False, help="Lagerbox name or IP")
@click.option("--display", is_flag=True, help="Display measurement on screen")
@click.option("--cursor", is_flag=True, help="Enable measurement cursor")
def freq(ctx, mcu, box, display, cursor):
    """Measure waveform frequency"""
    box_ip = _resolve_box(ctx, box)
    netname = _require_netname(ctx)

    if _validate_scope_net(ctx, box_ip, netname) is None:
        return  # Error already displayed with available nets

    data = {
        "action": "measure_freq",
        "mcu": mcu,
        "params": {
            "netname": netname,
            "display": display,
            "cursor": cursor
        }
    }

    run_python_internal(
        ctx,
        get_impl_path("scope.py"),
        box_ip,
        env=(f"LAGER_COMMAND_DATA={json.dumps(data)}",),
        passenv=(),
        kill=False,
        download=(),
        allow_overwrite=False,
        signum="SIGTERM",
        timeout=0,
        detach=False,
        port=(),
        org=None,
        args=(),
    )


@measure.command()
@click.pass_context
@click.option("--mcu", required=False)
@click.option("--box", required=False, help="Lagerbox name or IP")
@click.option("--display", is_flag=True, help="Display measurement on screen")
@click.option("--cursor", is_flag=True, help="Enable measurement cursor")
def vpp(ctx, mcu, box, display, cursor):
    """Measure peak-to-peak voltage"""
    box_ip = _resolve_box(ctx, box)
    netname = _require_netname(ctx)

    if _validate_scope_net(ctx, box_ip, netname) is None:
        return  # Error already displayed with available nets

    data = {
        "action": "measure_vpp",
        "mcu": mcu,
        "params": {
            "netname": netname,
            "display": display,
            "cursor": cursor
        }
    }

    run_python_internal(
        ctx,
        get_impl_path("scope.py"),
        box_ip,
        env=(f"LAGER_COMMAND_DATA={json.dumps(data)}",),
        passenv=(),
        kill=False,
        download=(),
        allow_overwrite=False,
        signum="SIGTERM",
        timeout=0,
        detach=False,
        port=(),
        org=None,
        args=(),
    )


@measure.command()
@click.pass_context
@click.option("--mcu", required=False)
@click.option("--box", required=False, help="Lagerbox name or IP")
@click.option("--display", is_flag=True, help="Display measurement on screen")
@click.option("--cursor", is_flag=True, help="Enable measurement cursor")
def vmax(ctx, mcu, box, display, cursor):
    """Measure maximum voltage"""
    box_ip = _resolve_box(ctx, box)
    netname = _require_netname(ctx)

    if _validate_scope_net(ctx, box_ip, netname) is None:
        return  # Error already displayed with available nets

    data = {
        "action": "measure_vmax",
        "mcu": mcu,
        "params": {
            "netname": netname,
            "display": display,
            "cursor": cursor
        }
    }

    run_python_internal(
        ctx,
        get_impl_path("scope.py"),
        box_ip,
        env=(f"LAGER_COMMAND_DATA={json.dumps(data)}",),
        passenv=(),
        kill=False,
        download=(),
        allow_overwrite=False,
        signum="SIGTERM",
        timeout=0,
        detach=False,
        port=(),
        org=None,
        args=(),
    )


@measure.command()
@click.pass_context
@click.option("--mcu", required=False)
@click.option("--box", required=False, help="Lagerbox name or IP")
@click.option("--display", is_flag=True, help="Display measurement on screen")
@click.option("--cursor", is_flag=True, help="Enable measurement cursor")
def vmin(ctx, mcu, box, display, cursor):
    """Measure minimum voltage"""
    box_ip = _resolve_box(ctx, box)
    netname = _require_netname(ctx)

    if _validate_scope_net(ctx, box_ip, netname) is None:
        return  # Error already displayed with available nets

    data = {
        "action": "measure_vmin",
        "mcu": mcu,
        "params": {
            "netname": netname,
            "display": display,
            "cursor": cursor
        }
    }

    run_python_internal(
        ctx,
        get_impl_path("scope.py"),
        box_ip,
        env=(f"LAGER_COMMAND_DATA={json.dumps(data)}",),
        passenv=(),
        kill=False,
        download=(),
        allow_overwrite=False,
        signum="SIGTERM",
        timeout=0,
        detach=False,
        port=(),
        org=None,
        args=(),
    )


@measure.command()
@click.pass_context
@click.option("--mcu", required=False)
@click.option("--box", required=False, help="Lagerbox name or IP")
@click.option("--display", is_flag=True, help="Display measurement on screen")
@click.option("--cursor", is_flag=True, help="Enable measurement cursor")
def vrms(ctx, mcu, box, display, cursor):
    """Measure RMS voltage"""
    box_ip = _resolve_box(ctx, box)
    netname = _require_netname(ctx)

    if _validate_scope_net(ctx, box_ip, netname) is None:
        return  # Error already displayed with available nets

    data = {
        "action": "measure_vrms",
        "mcu": mcu,
        "params": {
            "netname": netname,
            "display": display,
            "cursor": cursor
        }
    }

    run_python_internal(
        ctx,
        get_impl_path("scope.py"),
        box_ip,
        env=(f"LAGER_COMMAND_DATA={json.dumps(data)}",),
        passenv=(),
        kill=False,
        download=(),
        allow_overwrite=False,
        signum="SIGTERM",
        timeout=0,
        detach=False,
        port=(),
        org=None,
        args=(),
    )


@measure.command()
@click.pass_context
@click.option("--mcu", required=False)
@click.option("--box", required=False, help="Lagerbox name or IP")
@click.option("--display", is_flag=True, help="Display measurement on screen")
@click.option("--cursor", is_flag=True, help="Enable measurement cursor")
def vavg(ctx, mcu, box, display, cursor):
    """Measure average voltage"""
    box_ip = _resolve_box(ctx, box)
    netname = _require_netname(ctx)

    if _validate_scope_net(ctx, box_ip, netname) is None:
        return  # Error already displayed with available nets

    data = {
        "action": "measure_vavg",
        "mcu": mcu,
        "params": {
            "netname": netname,
            "display": display,
            "cursor": cursor
        }
    }

    run_python_internal(
        ctx,
        get_impl_path("scope.py"),
        box_ip,
        env=(f"LAGER_COMMAND_DATA={json.dumps(data)}",),
        passenv=(),
        kill=False,
        download=(),
        allow_overwrite=False,
        signum="SIGTERM",
        timeout=0,
        detach=False,
        port=(),
        org=None,
        args=(),
    )


@measure.command("pulse-width-pos")
@click.pass_context
@click.option("--mcu", required=False)
@click.option("--box", required=False, help="Lagerbox name or IP")
@click.option("--display", is_flag=True, help="Display measurement on screen")
@click.option("--cursor", is_flag=True, help="Enable measurement cursor")
def pulse_width_pos(ctx, mcu, box, display, cursor):
    """Measure positive pulse width"""
    box_ip = _resolve_box(ctx, box)
    netname = _require_netname(ctx)

    if _validate_scope_net(ctx, box_ip, netname) is None:
        return  # Error already displayed with available nets

    data = {
        "action": "measure_pulse_width_pos",
        "mcu": mcu,
        "params": {
            "netname": netname,
            "display": display,
            "cursor": cursor
        }
    }

    run_python_internal(
        ctx,
        get_impl_path("scope.py"),
        box_ip,
        env=(f"LAGER_COMMAND_DATA={json.dumps(data)}",),
        passenv=(),
        kill=False,
        download=(),
        allow_overwrite=False,
        signum="SIGTERM",
        timeout=0,
        detach=False,
        port=(),
        org=None,
        args=(),
    )


@measure.command("pulse-width-neg")
@click.pass_context
@click.option("--mcu", required=False)
@click.option("--box", required=False, help="Lagerbox name or IP")
@click.option("--display", is_flag=True, help="Display measurement on screen")
@click.option("--cursor", is_flag=True, help="Enable measurement cursor")
def pulse_width_neg(ctx, mcu, box, display, cursor):
    """Measure negative pulse width"""
    box_ip = _resolve_box(ctx, box)
    netname = _require_netname(ctx)

    if _validate_scope_net(ctx, box_ip, netname) is None:
        return  # Error already displayed with available nets

    data = {
        "action": "measure_pulse_width_neg",
        "mcu": mcu,
        "params": {
            "netname": netname,
            "display": display,
            "cursor": cursor
        }
    }

    run_python_internal(
        ctx,
        get_impl_path("scope.py"),
        box_ip,
        env=(f"LAGER_COMMAND_DATA={json.dumps(data)}",),
        passenv=(),
        kill=False,
        download=(),
        allow_overwrite=False,
        signum="SIGTERM",
        timeout=0,
        detach=False,
        port=(),
        org=None,
        args=(),
    )


@measure.command("duty-cycle-pos")
@click.pass_context
@click.option("--mcu", required=False)
@click.option("--box", required=False, help="Lagerbox name or IP")
@click.option("--display", is_flag=True, help="Display measurement on screen")
@click.option("--cursor", is_flag=True, help="Enable measurement cursor")
def duty_cycle_pos(ctx, mcu, box, display, cursor):
    """Measure positive duty cycle"""
    box_ip = _resolve_box(ctx, box)
    netname = _require_netname(ctx)

    if _validate_scope_net(ctx, box_ip, netname) is None:
        return  # Error already displayed with available nets

    data = {
        "action": "measure_dc_pos",
        "mcu": mcu,
        "params": {
            "netname": netname,
            "display": display,
            "cursor": cursor
        }
    }

    run_python_internal(
        ctx,
        get_impl_path("scope.py"),
        box_ip,
        env=(f"LAGER_COMMAND_DATA={json.dumps(data)}",),
        passenv=(),
        kill=False,
        download=(),
        allow_overwrite=False,
        signum="SIGTERM",
        timeout=0,
        detach=False,
        port=(),
        org=None,
        args=(),
    )


@measure.command("duty-cycle-neg")
@click.pass_context
@click.option("--mcu", required=False)
@click.option("--box", required=False, help="Lagerbox name or IP")
@click.option("--display", is_flag=True, help="Display measurement on screen")
@click.option("--cursor", is_flag=True, help="Enable measurement cursor")
def duty_cycle_neg(ctx, mcu, box, display, cursor):
    """Measure negative duty cycle"""
    box_ip = _resolve_box(ctx, box)
    netname = _require_netname(ctx)

    if _validate_scope_net(ctx, box_ip, netname) is None:
        return  # Error already displayed with available nets

    data = {
        "action": "measure_dc_neg",
        "mcu": mcu,
        "params": {
            "netname": netname,
            "display": display,
            "cursor": cursor
        }
    }

    run_python_internal(
        ctx,
        get_impl_path("scope.py"),
        box_ip,
        env=(f"LAGER_COMMAND_DATA={json.dumps(data)}",),
        passenv=(),
        kill=False,
        download=(),
        allow_overwrite=False,
        signum="SIGTERM",
        timeout=0,
        detach=False,
        port=(),
        org=None,
        args=(),
    )


@scope.group()
def trigger():
    """Configure trigger settings"""
    pass


MODE_CHOICES = click.Choice(("normal", "auto", "single"))
COUPLING_CHOICES = click.Choice(("dc", "ac", "low_freq_rej", "high_freq_rej"))


@trigger.command()
@click.pass_context
@click.option("--mcu", required=False)
@click.option("--box", required=False, help="Lagerbox name or IP")
@click.option("--mode", default="normal", type=MODE_CHOICES, help="Trigger mode", show_default=True)
@click.option("--coupling", default="dc", type=COUPLING_CHOICES, help="Coupling mode", show_default=True)
@click.option("--source", required=False, help="Trigger source", metavar="NET")
@click.option("--slope", type=click.Choice(("rising", "falling", "both")), help="Trigger slope")
@click.option("--level", type=click.FLOAT, help="Trigger level")
def edge(ctx, mcu, box, mode, coupling, source, slope, level):
    """Set edge trigger (works with both PicoScope and Rigol)"""
    box_ip = _resolve_box(ctx, box)
    netname = _require_netname(ctx)

    if _validate_scope_net(ctx, box_ip, netname) is None:
        return  # Error already displayed with available nets

    data = {
        "action": "trigger_edge",
        "mcu": mcu,
        "params": {
            "netname": netname,
            "mode": mode,
            "coupling": coupling,
            "source": source,
            "slope": slope,
            "level": level,
        }
    }

    run_python_internal(
        ctx,
        get_impl_path("scope.py"),
        box_ip,
        env=(f"LAGER_COMMAND_DATA={json.dumps(data)}",),
        passenv=(),
        kill=False,
        download=(),
        allow_overwrite=False,
        signum="SIGTERM",
        timeout=0,
        detach=False,
        port=(),
        org=None,
        args=(),
    )


@trigger.command()
@click.pass_context
@click.option("--mcu", required=False)
@click.option("--box", required=False, help="Lagerbox name or IP")
@click.option("--mode", default="normal", type=MODE_CHOICES, help="Trigger mode", show_default=True)
@click.option("--coupling", default="dc", type=COUPLING_CHOICES, help="Coupling mode", show_default=True)
@click.option("--source", required=False, help="Trigger source", metavar="NET")
@click.option("--level", type=click.FLOAT, help="Trigger level")
@click.option("--baud", type=click.INT, default=9600, help="Baud rate", show_default=True)
@click.option("--parity", type=click.Choice(("none", "even", "odd")), default="none", help="Parity", show_default=True)
@click.option("--stop-bits", type=click.Choice(("1", "1.5", "2")), default="1", help="Stop bits", show_default=True)
@click.option("--data-width", type=click.INT, default=8, help="Data width (bits)", show_default=True)
@click.option("--trigger-on", type=click.Choice(("start", "stop", "data", "error")), default="start", help="Trigger condition", show_default=True)
@click.option("--data", type=click.STRING, required=False, help="Data pattern to match (hex)")
def uart(ctx, mcu, box, mode, coupling, source, level, baud, parity, stop_bits, data_width, trigger_on, data):
    """Set UART trigger (Rigol only)"""
    box_ip = _resolve_box(ctx, box)
    netname = _require_netname(ctx)

    if _validate_scope_net(ctx, box_ip, netname) is None:
        return  # Error already displayed with available nets

    _run_backend(ctx, box_ip, "trigger_uart", netname=netname, mcu=mcu, mode=mode,
                 coupling=coupling, source=source, level=level, trigger_on=trigger_on,
                 parity=parity, stop_bits=stop_bits, baud=baud, data_width=data_width, data=data)


@trigger.command()
@click.pass_context
@click.option("--mcu", required=False)
@click.option("--box", required=False, help="Lagerbox name or IP")
@click.option("--mode", default="normal", type=MODE_CHOICES, help="Trigger mode", show_default=True)
@click.option("--coupling", default="dc", type=COUPLING_CHOICES, help="Coupling mode", show_default=True)
@click.option("--source-scl", required=False, help="SCL source net", metavar="NET")
@click.option("--source-sda", required=False, help="SDA source net", metavar="NET")
@click.option("--level-scl", type=click.FLOAT, help="SCL trigger level")
@click.option("--level-sda", type=click.FLOAT, help="SDA trigger level")
@click.option("--trigger-on", type=click.Choice(("start", "restart", "stop", "ack_miss", "address", "data", "addr_data")), default="start", help="Trigger condition", show_default=True)
@click.option("--address", type=click.STRING, required=False, help="I2C address (hex)")
@click.option("--addr-width", type=click.Choice(("7", "8", "10")), default="7", help="Address width (bits)", show_default=True)
@click.option("--data", type=click.STRING, required=False, help="Data pattern to match (hex)")
@click.option("--data-width", type=click.INT, default=8, help="Data width (bits)", show_default=True)
@click.option("--direction", type=click.Choice(("read", "write", "read_write")), default="read_write", help="Transfer direction", show_default=True)
def i2c(ctx, mcu, box, mode, coupling, source_scl, source_sda, level_scl, level_sda,
        trigger_on, address, addr_width, data, data_width, direction):
    """Set I2C trigger (Rigol only)"""
    box_ip = _resolve_box(ctx, box)
    netname = _require_netname(ctx)

    if _validate_scope_net(ctx, box_ip, netname) is None:
        return  # Error already displayed with available nets

    _run_backend(ctx, box_ip, "trigger_i2c", netname=netname, mcu=mcu, mode=mode,
                 coupling=coupling, source_scl=source_scl, level_scl=level_scl,
                 source_sda=source_sda, level_sda=level_sda, trigger_on=trigger_on,
                 address=address, addr_width=addr_width, data=data, data_width=data_width,
                 direction=direction)


@trigger.command()
@click.pass_context
@click.option("--mcu", required=False)
@click.option("--box", required=False, help="Lagerbox name or IP")
@click.option("--mode", default="normal", type=MODE_CHOICES, help="Trigger mode", show_default=True)
@click.option("--coupling", default="dc", type=COUPLING_CHOICES, help="Coupling mode", show_default=True)
@click.option("--source-mosi-miso", required=False, help="MOSI/MISO source net", metavar="NET")
@click.option("--source-sck", required=False, help="SCK source net", metavar="NET")
@click.option("--source-cs", required=False, help="CS source net", metavar="NET")
@click.option("--level-mosi-miso", type=click.FLOAT, help="MOSI/MISO trigger level")
@click.option("--level-sck", type=click.FLOAT, help="SCK trigger level")
@click.option("--level-cs", type=click.FLOAT, help="CS trigger level")
@click.option("--trigger-on", type=click.Choice(("timeout", "cs")), default="cs", help="Trigger condition", show_default=True)
@click.option("--data", type=click.STRING, required=False, help="Data pattern to match (hex)")
@click.option("--data-width", type=click.INT, default=8, help="Data width (bits)", show_default=True)
@click.option("--clk-slope", type=click.Choice(("rising", "falling")), default="rising", help="Clock edge", show_default=True)
@click.option("--cs-idle", type=click.Choice(("high", "low")), default="high", help="CS idle state", show_default=True)
@click.option("--timeout", type=click.FLOAT, required=False, help="Timeout value (seconds)")
def spi(ctx, mcu, box, mode, coupling, source_mosi_miso, source_sck, source_cs,
        level_mosi_miso, level_sck, level_cs, trigger_on, data, data_width, clk_slope, cs_idle, timeout):
    """Set SPI trigger (Rigol only)"""
    box_ip = _resolve_box(ctx, box)
    netname = _require_netname(ctx)

    if _validate_scope_net(ctx, box_ip, netname) is None:
        return  # Error already displayed with available nets

    _run_backend(ctx, box_ip, "trigger_spi", netname=netname, mcu=mcu, mode=mode,
                 coupling=coupling, source_mosi_miso=source_mosi_miso, source_sck=source_sck,
                 source_cs=source_cs, level_mosi_miso=level_mosi_miso, level_sck=level_sck,
                 level_cs=level_cs, data=data, data_width=data_width, clk_slope=clk_slope,
                 trigger_on=trigger_on, cs_idle=cs_idle, timeout=timeout)


@trigger.command()
@click.pass_context
@click.option("--mcu", required=False)
@click.option("--box", required=False, help="Lagerbox name or IP")
@click.option("--mode", default="normal", type=MODE_CHOICES, help="Trigger mode", show_default=True)
@click.option("--coupling", default="dc", type=COUPLING_CHOICES, help="Coupling mode", show_default=True)
@click.option("--source", required=False, help="Trigger source", metavar="NET")
@click.option("--level", type=click.FLOAT, help="Trigger level")
@click.option("--trigger-on", type=click.Choice(("positive", "negative", "positive_greater", "negative_greater", "positive_less", "negative_less")), default="positive", help="Trigger condition", show_default=True)
@click.option("--upper", type=click.FLOAT, required=False, help="Upper pulse width limit (seconds)")
@click.option("--lower", type=click.FLOAT, required=False, help="Lower pulse width limit (seconds)")
def pulse(ctx, mcu, box, mode, coupling, source, level, trigger_on, upper, lower):
    """Set pulse width trigger (Rigol only)"""
    box_ip = _resolve_box(ctx, box)
    netname = _require_netname(ctx)

    if _validate_scope_net(ctx, box_ip, netname) is None:
        return  # Error already displayed with available nets

    _run_backend(ctx, box_ip, "trigger_pulse", netname=netname, mcu=mcu, mode=mode,
                 coupling=coupling, source=source, level=level, trigger_on=trigger_on,
                 upper=upper, lower=lower)


@scope.group()
def cursor():
    """Control scope cursor (Rigol only)"""
    pass


@cursor.command()
@click.pass_context
@click.option("--box", required=False, help="Lagerbox name or IP")
@click.option("--mcu", required=False)
@click.option("--x", required=False, type=click.FLOAT, help="Cursor A x coordinate")
@click.option("--y", required=False, type=click.FLOAT, help="Cursor A y coordinate")
def set_a(ctx, box, mcu, x, y):
    """Set cursor A position"""
    box_ip = _resolve_box(ctx, box)
    netname = _require_netname(ctx)

    if _validate_scope_net(ctx, box_ip, netname) is None:
        return  # Error already displayed with available nets

    data = {
        "action": "set_a",
        "mcu": mcu,
        "params": {
            "netname": netname,
            "x": x,
            "y": y,
        }
    }

    run_python_internal(
        ctx,
        get_impl_path("scope.py"),
        box_ip,
        env=(f"LAGER_COMMAND_DATA={json.dumps(data)}",),
        passenv=(),
        kill=False,
        download=(),
        allow_overwrite=False,
        signum="SIGTERM",
        timeout=0,
        detach=False,
        port=(),
        org=None,
        args=(),
    )


@cursor.command()
@click.pass_context
@click.option("--box", required=False, help="Lagerbox name or IP")
@click.option("--mcu", required=False)
@click.option("--x", required=False, type=click.FLOAT, help="Cursor B x coordinate")
@click.option("--y", required=False, type=click.FLOAT, help="Cursor B y coordinate")
def set_b(ctx, box, mcu, x, y):
    """Set cursor B position"""
    box_ip = _resolve_box(ctx, box)
    netname = _require_netname(ctx)

    if _validate_scope_net(ctx, box_ip, netname) is None:
        return  # Error already displayed with available nets

    data = {
        "action": "set_b",
        "mcu": mcu,
        "params": {
            "netname": netname,
            "x": x,
            "y": y,
        }
    }

    run_python_internal(
        ctx,
        get_impl_path("scope.py"),
        box_ip,
        env=(f"LAGER_COMMAND_DATA={json.dumps(data)}",),
        passenv=(),
        kill=False,
        download=(),
        allow_overwrite=False,
        signum="SIGTERM",
        timeout=0,
        detach=False,
        port=(),
        org=None,
        args=(),
    )


@cursor.command()
@click.pass_context
@click.option("--box", required=False, help="Lagerbox name or IP")
@click.option("--mcu", required=False)
@click.option("--x", required=False, type=click.FLOAT, help="Relative x movement (delta)")
@click.option("--y", required=False, type=click.FLOAT, help="Relative y movement (delta)")
def move_a(ctx, box, mcu, x, y):
    """Move cursor A by relative offset"""
    box_ip = _resolve_box(ctx, box)
    netname = _require_netname(ctx)

    if _validate_scope_net(ctx, box_ip, netname) is None:
        return  # Error already displayed with available nets

    data = {
        "action": "move_a",
        "mcu": mcu,
        "params": {
            "netname": netname,
            "del_x": x,
            "del_y": y,
        }
    }

    run_python_internal(
        ctx,
        get_impl_path("scope.py"),
        box_ip,
        env=(f"LAGER_COMMAND_DATA={json.dumps(data)}",),
        passenv=(),
        kill=False,
        download=(),
        allow_overwrite=False,
        signum="SIGTERM",
        timeout=0,
        detach=False,
        port=(),
        org=None,
        args=(),
    )


@cursor.command()
@click.pass_context
@click.option("--box", required=False, help="Lagerbox name or IP")
@click.option("--mcu", required=False)
@click.option("--x", required=False, type=click.FLOAT, help="Relative x movement (delta)")
@click.option("--y", required=False, type=click.FLOAT, help="Relative y movement (delta)")
def move_b(ctx, box, mcu, x, y):
    """Move cursor B by relative offset"""
    box_ip = _resolve_box(ctx, box)
    netname = _require_netname(ctx)

    if _validate_scope_net(ctx, box_ip, netname) is None:
        return  # Error already displayed with available nets

    data = {
        "action": "move_b",
        "mcu": mcu,
        "params": {
            "netname": netname,
            "del_x": x,
            "del_y": y,
        }
    }

    run_python_internal(
        ctx,
        get_impl_path("scope.py"),
        box_ip,
        env=(f"LAGER_COMMAND_DATA={json.dumps(data)}",),
        passenv=(),
        kill=False,
        download=(),
        allow_overwrite=False,
        signum="SIGTERM",
        timeout=0,
        detach=False,
        port=(),
        org=None,
        args=(),
    )


@cursor.command()
@click.pass_context
@click.option("--box", required=False, help="Lagerbox name or IP")
@click.option("--mcu", required=False)
def hide(ctx, box, mcu):
    """Hide cursor"""
    box_ip = _resolve_box(ctx, box)
    netname = _require_netname(ctx)

    if _validate_scope_net(ctx, box_ip, netname) is None:
        return  # Error already displayed with available nets

    data = {
        "action": "hide_cursor",
        "mcu": mcu,
        "params": {
            "netname": netname,
        }
    }

    run_python_internal(
        ctx,
        get_impl_path("scope.py"),
        box_ip,
        env=(f"LAGER_COMMAND_DATA={json.dumps(data)}",),
        passenv=(),
        kill=False,
        download=(),
        allow_overwrite=False,
        signum="SIGTERM",
        timeout=0,
        detach=False,
        port=(),
        org=None,
        args=(),
    )


# ---------- STREAMING COMMANDS (PicoScope) ----------

@scope.group()
def stream():
    """Stream oscilloscope data (PicoScope)"""
    pass


CAPTURE_MODE_CHOICES = click.Choice(("auto", "normal", "single"))
COUPLING_STREAM_CHOICES = click.Choice(("dc", "ac"))
TRIGGER_SLOPE_CHOICES = click.Choice(("rising", "falling", "either"))
CHANNEL_CHOICES = click.Choice(("A", "B", "1", "2"))


@stream.command("start")
@click.pass_context
@click.option("--box", required=False, help="Lagerbox name or IP")
@click.option("--channel", "-c", type=CHANNEL_CHOICES, default="A", help="Channel to enable (A, B, 1, or 2)")
@click.option("--volts-per-div", "-v", type=float, default=1.0, help="Vertical scale in volts per division (default: 1.0V/div)")
@click.option("--time-per-div", "-t", type=float, default=0.001, help="Horizontal scale in seconds per division (default: 1ms/div)")
@click.option("--trigger-level", type=float, default=0.0, help="Trigger threshold voltage (default: 0V)")
@click.option("--trigger-slope", type=TRIGGER_SLOPE_CHOICES, default="rising", help="Trigger edge direction (rising, falling, or either)")
@click.option("--capture-mode", type=CAPTURE_MODE_CHOICES, default="auto", help="Triggering mode (auto, normal, or single)")
@click.option("--coupling", type=COUPLING_STREAM_CHOICES, default="dc", help="Input coupling type (dc or ac)")
@click.option("--quiet", "-q", is_flag=True, help="Minimal output")
@click.option("--json", "json_output", is_flag=True, help="JSON output format")
@click.option("--verbose", is_flag=True, help="Verbose debugging output")
def stream_start(ctx, box, channel, volts_per_div, time_per_div, trigger_level, trigger_slope, capture_mode, coupling, quiet, json_output, verbose):
    """
    Start oscilloscope streaming with web visualization.
    """
    box_ip = _resolve_box(ctx, box)
    netname = _require_netname(ctx)

    # Validate scale and timebase ranges
    _validate_scale(ctx, volts_per_div)
    _validate_timebase(ctx, time_per_div)

    # Validate net and check if this is a PicoScope
    net_info = _validate_scope_net(ctx, box_ip, netname)
    if net_info is None:
        return  # Error already displayed with available nets

    instrument = net_info.get("instrument", "")
    if not _is_picoscope(instrument):
        click.secho(f"Error: '{netname}' is not a PicoScope (instrument: {instrument})", fg="red", err=True)
        click.secho("Streaming is only supported for PicoScope devices.", err=True)
        click.secho("For Rigol scopes, use: lager scope <net> start --single", err=True)
        ctx.exit(1)

    data = {
        "action": "stream_start",
        "params": {
            "netname": netname,
            "channel": channel,
            "volts_per_div": volts_per_div,
            "time_per_div": time_per_div,
            "trigger_level": trigger_level,
            "trigger_slope": trigger_slope,
            "capture_mode": capture_mode,
            "coupling": coupling,
            "box_ip": box_ip,  # Pass box IP for browser URL
            "quiet": quiet,
            "json_output": json_output,
            "verbose": verbose,
        }
    }

    run_python_internal(
        ctx,
        get_impl_path("scope_stream.py"),
        box_ip,
        env=(f"LAGER_COMMAND_DATA={json.dumps(data)}",),
        passenv=(),
        kill=False,
        download=(),
        allow_overwrite=False,
        signum="SIGTERM",
        timeout=0,
        detach=False,
        port=(),
        org=None,
        args=(),
    )


@stream.command("stop")
@click.pass_context
@click.option("--box", required=False, help="Lagerbox name or IP")
def stream_stop(ctx, box):
    """Stop oscilloscope streaming acquisition"""
    box_ip = _resolve_box(ctx, box)
    netname = _require_netname(ctx)

    if _validate_scope_net(ctx, box_ip, netname) is None:
        return  # Error already displayed with available nets

    data = {
        "action": "stream_stop",
        "params": {
            "netname": netname,
        }
    }

    run_python_internal(
        ctx,
        get_impl_path("scope_stream.py"),
        box_ip,
        env=(f"LAGER_COMMAND_DATA={json.dumps(data)}",),
        passenv=(),
        kill=False,
        download=(),
        allow_overwrite=False,
        signum="SIGTERM",
        timeout=0,
        detach=False,
        port=(),
        org=None,
        args=(),
    )


@stream.command("status")
@click.pass_context
@click.option("--box", required=False, help="Lagerbox name or IP")
def stream_status(ctx, box):
    """Check oscilloscope streaming daemon status"""
    box_ip = _resolve_box(ctx, box)

    data = {
        "action": "stream_status",
        "params": {}
    }

    run_python_internal(
        ctx,
        get_impl_path("scope_stream.py"),
        box_ip,
        env=(f"LAGER_COMMAND_DATA={json.dumps(data)}",),
        passenv=(),
        kill=False,
        download=(),
        allow_overwrite=False,
        signum="SIGTERM",
        timeout=0,
        detach=False,
        port=(),
        org=None,
        args=(),
    )


@stream.command("web")
@click.pass_context
@click.option("--box", required=False, help="Lagerbox name or IP")
@click.option("--port", type=int, default=8080, help="HTTP server port for oscilloscope UI")
def stream_web(ctx, box, port):
    """Open web browser for oscilloscope visualization"""
    import webbrowser

    box_ip = _resolve_box(ctx, box)

    # Construct the URL for the web visualization
    # Port 8080 serves the HTML UI which connects to WebTransport on 8083
    url = f"http://{box_ip}:{port}/web_oscilloscope.html"

    click.secho(f"Opening oscilloscope visualization at {url}", fg="green")
    click.secho("Note: Make sure streaming is started with 'lager scope <net> stream start'", fg="yellow")

    try:
        webbrowser.open(url)
    except Exception as e:
        click.secho(f"Could not open browser: {e}", fg="red", err=True)
        click.secho(f"Please open {url} manually in your browser", fg="yellow")


@stream.command("capture")
@click.pass_context
@click.option("--box", required=False, help="Lagerbox name or IP")
@click.option("--output", "-o", type=click.Path(), default="scope_data.csv", help="CSV output file path (default: scope_data.csv)")
@click.option("--duration", "-d", type=float, default=1.0, help="Capture duration in seconds (default: 1.0)")
@click.option("--samples", "-n", type=int, default=None, help="Maximum number of samples to capture (optional)")
@click.option("--quiet", "-q", is_flag=True, help="Minimal output")
@click.option("--json", "json_output", is_flag=True, help="JSON output format")
@click.option("--verbose", is_flag=True, help="Verbose debugging output")
def stream_capture(ctx, box, output, duration, samples, quiet, json_output, verbose):
    """
    Capture oscilloscope waveform data to CSV file.
    """
    box_ip = _resolve_box(ctx, box)
    netname = _require_netname(ctx)

    # Validate duration and samples if provided
    _validate_duration(ctx, duration)
    if samples is not None:
        _validate_samples(ctx, samples)

    if _validate_scope_net(ctx, box_ip, netname) is None:
        return  # Error already displayed with available nets

    data = {
        "action": "stream_capture",
        "params": {
            "netname": netname,
            "output": output,
            "duration": duration,
            "samples": samples,
            "quiet": quiet,
            "json_output": json_output,
            "verbose": verbose,
        }
    }

    # Note: File is saved on box at the specified output path
    # For direct connections, download isn't supported - file stays on box
    run_python_internal(
        ctx,
        get_impl_path("scope_stream.py"),
        box_ip,
        env=(f"LAGER_COMMAND_DATA={json.dumps(data)}",),
        passenv=(),
        kill=False,
        download=(),  # Disabled - DirectHTTPSession doesn't support download
        allow_overwrite=True,
        signum="SIGTERM",
        timeout=int(duration * 2 + 30) if duration else 60,
        detach=False,
        port=(),
        org=None,
        args=(),
    )

    click.secho(f"\nNote: Data file saved on box at: {output}", fg="yellow")
    click.secho(f"To retrieve: scp lagerdata@{box_ip}:/tmp/{output} .", fg="yellow")


@stream.command("config")
@click.pass_context
@click.option("--box", required=False, help="Lagerbox name or IP")
@click.option("--channel", "-c", type=CHANNEL_CHOICES, help="Channel to configure")
@click.option("--volts-per-div", "-v", type=float, help="Volts per division")
@click.option("--time-per-div", "-t", type=float, help="Time per division (seconds)")
@click.option("--trigger-level", type=float, help="Trigger level (volts)")
@click.option("--trigger-source", type=CHANNEL_CHOICES, help="Trigger source channel")
@click.option("--trigger-slope", type=TRIGGER_SLOPE_CHOICES, help="Trigger slope")
@click.option("--capture-mode", type=CAPTURE_MODE_CHOICES, help="Capture mode")
@click.option("--coupling", type=COUPLING_STREAM_CHOICES, help="Input coupling")
@click.option("--enable/--disable", default=None, help="Enable or disable channel")
def stream_config(ctx, box, channel, volts_per_div, time_per_div, trigger_level, trigger_source, trigger_slope, capture_mode, coupling, enable):
    """Configure oscilloscope streaming settings (PicoScope)"""
    box_ip = _resolve_box(ctx, box)
    netname = _require_netname(ctx)

    if _validate_scope_net(ctx, box_ip, netname) is None:
        return  # Error already displayed with available nets

    # Build config dict with only provided options
    config_params = {"netname": netname}
    if channel is not None:
        config_params["channel"] = channel
    if volts_per_div is not None:
        config_params["volts_per_div"] = volts_per_div
    if time_per_div is not None:
        config_params["time_per_div"] = time_per_div
    if trigger_level is not None:
        config_params["trigger_level"] = trigger_level
    if trigger_source is not None:
        config_params["trigger_source"] = trigger_source
    if trigger_slope is not None:
        config_params["trigger_slope"] = trigger_slope
    if capture_mode is not None:
        config_params["capture_mode"] = capture_mode
    if coupling is not None:
        config_params["coupling"] = coupling
    if enable is not None:
        config_params["enable"] = enable

    data = {
        "action": "stream_config",
        "params": config_params,
    }

    run_python_internal(
        ctx,
        get_impl_path("scope_stream.py"),
        box_ip,
        env=(f"LAGER_COMMAND_DATA={json.dumps(data)}",),
        passenv=(),
        kill=False,
        download=(),
        allow_overwrite=False,
        signum="SIGTERM",
        timeout=0,
        detach=False,
        port=(),
        org=None,
        args=(),
    )
