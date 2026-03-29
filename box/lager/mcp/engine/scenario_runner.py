#!/usr/bin/env python3
# Copyright 2024-2026 Lager Data LLC
# SPDX-License-Identifier: Apache-2.0

"""
On-box scenario interpreter.

This is a **fixed, stable Python script** -- it is NOT generated per scenario.
It is uploaded to the box via :5000/python and receives the scenario JSON
through the LAGER_SCENARIO environment variable.

The runner walks setup -> steps -> cleanup sequentially, dispatching each
step to a registered action handler. Results and assertion outcomes are
printed as a single JSON object to stdout.

Physical interaction model
--------------------------
Every action handler targets a **real net wired to the DUT** using the
canonical ``lager.Net.get(name, type=NetType.X)`` API -- the same code
path used by ``lager python`` scripts.

  - ``gpio_set`` / ``gpio_read`` / ``gpio_wait`` use ``NetType.GPIO``
  - ``uart_send`` / ``uart_expect`` use ``NetType.UART`` → ``net.connect()``
  - ``spi_*`` handlers use ``NetType.SPI``
  - ``i2c_*`` handlers use ``NetType.I2C``
  - ``set_voltage`` / ``enable_supply`` etc. use ``NetType.PowerSupply``
  - ``adc_read`` uses ``NetType.ADC``, ``dac_set`` uses ``NetType.DAC``
  - ``debug_*`` handlers use ``NetType.Debug``
  - ``rtt_*`` handlers use ``debug.rtt()`` context manager
  - ``watt_*`` uses ``NetType.WattMeter``
  - ``tc_read`` uses ``NetType.Thermocouple``
  - ``usb_enable`` / ``usb_disable`` use ``NetType.Usb``

There is no HTTP roundtrip to :8080/invoke and no direct dispatcher
imports -- every call goes through the public ``Net`` abstraction.

v0 simplifications (intentional, not final)
--------------------------------------------
  - The runner is **uploaded each time** via :5000/python.  A future
    version may pre-install it on the box or run it as a long-lived
    service.
  - No box lock is acquired before execution.  A future version will
    integrate with the existing ``/lock`` endpoint.
"""

from __future__ import annotations

import json
import os
import sys
import time
import traceback
from typing import Any, Callable

# ---------------------------------------------------------------------------
# Action handler registry
# ---------------------------------------------------------------------------

_HANDLER_REGISTRY: dict[str, Callable] = {}


def action(name: str):
    """Decorator to register an action handler."""
    def decorator(fn: Callable):
        _HANDLER_REGISTRY[name] = fn
        return fn
    return decorator


# ---------------------------------------------------------------------------
# Universal net / connection cache
# ---------------------------------------------------------------------------

_net_cache: dict[str, Any] = {}
_uart_serials: dict[str, Any] = {}
_rtt_sessions: dict[str, Any] = {}


def _get_net(target: str, net_type: Any) -> Any:
    """Get or create a cached ``Net`` object for *target*."""
    key = f"{target}:{net_type.name}"
    if key not in _net_cache:
        from lager import Net
        _net_cache[key] = Net.get(target, type=net_type)
    return _net_cache[key]


def _get_uart_serial(target: str, params: dict) -> Any:
    """Get or create a cached pyserial connection for a UART net."""
    if target not in _uart_serials:
        from lager import NetType
        net = _get_net(target, NetType.UART)
        overrides: dict[str, Any] = {}
        if "baudrate" in params:
            overrides["baudrate"] = params["baudrate"]
        if "timeout" in params:
            overrides["timeout"] = params["timeout"]
        else:
            overrides["timeout"] = 1
        _uart_serials[target] = net.connect(**overrides)
    return _uart_serials[target]


def _get_rtt_session(target: str, params: dict) -> Any:
    """Get or create a cached RTT session via the debug net's context manager."""
    from lager import NetType
    channel = params.get("channel", 0)
    key = f"{target}:{channel}"
    if key not in _rtt_sessions:
        dbg = _get_net(target, NetType.Debug)
        ctx = dbg.rtt(channel=channel)
        _rtt_sessions[key] = ctx.__enter__()
    return _rtt_sessions[key]


def _cleanup_all() -> None:
    """Close all cached connections and clear caches."""
    for ser in _uart_serials.values():
        try:
            ser.close()
        except Exception:
            pass
    _uart_serials.clear()

    for rtt in _rtt_sessions.values():
        try:
            rtt.__exit__(None, None, None)
        except Exception:
            pass
    _rtt_sessions.clear()

    _net_cache.clear()


# ---------------------------------------------------------------------------
# GPIO handlers
# ---------------------------------------------------------------------------

@action("gpio_set")
def handle_gpio_set(target: str, params: dict, results: dict) -> dict:
    """Drive a box-controlled GPIO output into the DUT."""
    from lager import NetType
    level = params.get("level", 1)
    _get_net(target, NetType.GPIO).output(level)
    return {"target": target, "level": level}


@action("gpio_read")
def handle_gpio_read(target: str, params: dict, results: dict) -> dict:
    """Sample a DUT-driven signal through a box GPIO input."""
    from lager import NetType
    value = _get_net(target, NetType.GPIO).input()
    label = params.get("label")
    data = {"target": target, "value": value}
    if label:
        results[label] = data
    return data


@action("gpio_wait")
def handle_gpio_wait(target: str, params: dict, results: dict) -> dict:
    """Block until a GPIO net reaches a target level or timeout."""
    from lager import NetType
    level = params.get("level", 1)
    timeout = params.get("timeout", 5.0)
    elapsed = _get_net(target, NetType.GPIO).wait_for_level(level, timeout)
    label = params.get("label")
    data = {"target": target, "level": level, "elapsed_s": elapsed}
    if label:
        results[label] = data
    return data


# ---------------------------------------------------------------------------
# UART handlers
# ---------------------------------------------------------------------------

@action("uart_send")
def handle_uart_send(target: str, params: dict, results: dict) -> dict:
    """Send data to the DUT CLI over UART via pyserial."""
    ser = _get_uart_serial(target, params)
    data_str = params.get("data", "")
    ser.reset_input_buffer()
    ser.write(data_str.encode("utf-8", errors="ignore"))
    return {"target": target, "data": data_str}


@action("uart_expect")
def handle_uart_expect(target: str, params: dict, results: dict) -> dict:
    """Read DUT CLI output over UART until a pattern matches or timeout."""
    ser = _get_uart_serial(target, params)
    pattern = params.get("pattern", "")
    timeout_ms = params.get("timeout_ms", 5000)
    timeout_s = timeout_ms / 1000.0

    old_timeout = ser.timeout
    ser.timeout = timeout_s
    lines: list[str] = []
    matched = False
    try:
        deadline = time.time() + timeout_s
        while time.time() < deadline:
            line = ser.readline().decode("utf-8", errors="ignore").strip()
            if line:
                lines.append(line)
                if pattern in line:
                    matched = True
                    break
    finally:
        ser.timeout = old_timeout

    label = params.get("label")
    data = {"target": target, "pattern": pattern, "matched": matched, "output": lines}
    if label:
        results[label] = data
    return data


# ---------------------------------------------------------------------------
# SPI handlers
# ---------------------------------------------------------------------------

@action("spi_config")
def handle_spi_config(target: str, params: dict, results: dict) -> dict:
    """Configure SPI bus parameters."""
    from lager import NetType
    net = _get_net(target, NetType.SPI)
    cfg: dict[str, Any] = {}
    for key in ("mode", "bit_order", "frequency_hz", "word_size", "cs_active", "cs_mode"):
        if key in params:
            cfg[key] = params[key]
    net.config(**cfg)
    return {"target": target, "config": cfg}


@action("spi_transfer")
def handle_spi_transfer(target: str, params: dict, results: dict) -> dict:
    """Full-duplex SPI transfer (simultaneous read + write)."""
    from lager import NetType
    tx_data = params.get("data", [])
    rx = _get_net(target, NetType.SPI).read_write(tx_data)
    label = params.get("label")
    data = {"target": target, "tx_data": tx_data, "rx_data": rx}
    if label:
        results[label] = data
    return data


@action("spi_read")
def handle_spi_read(target: str, params: dict, results: dict) -> dict:
    """Read words from SPI bus."""
    from lager import NetType
    n_words = params.get("n_words", 1)
    fill = params.get("fill", 0x00)
    rx = _get_net(target, NetType.SPI).read(n_words, fill=fill)
    label = params.get("label")
    data = {"target": target, "n_words": n_words, "rx_data": rx}
    if label:
        results[label] = data
    return data


@action("spi_write")
def handle_spi_write(target: str, params: dict, results: dict) -> dict:
    """Write data to SPI bus (discard read)."""
    from lager import NetType
    tx_data = params.get("data", [])
    _get_net(target, NetType.SPI).write(tx_data)
    return {"target": target, "data": tx_data}


# ---------------------------------------------------------------------------
# I2C handlers
# ---------------------------------------------------------------------------

@action("i2c_config")
def handle_i2c_config(target: str, params: dict, results: dict) -> dict:
    """Configure I2C bus parameters."""
    from lager import NetType
    net = _get_net(target, NetType.I2C)
    cfg: dict[str, Any] = {}
    for key in ("frequency_hz", "pull_ups"):
        if key in params:
            cfg[key] = params[key]
    net.config(**cfg)
    return {"target": target, "config": cfg}


@action("i2c_scan")
def handle_i2c_scan(target: str, params: dict, results: dict) -> dict:
    """Scan I2C bus for connected devices."""
    from lager import NetType
    addresses = _get_net(target, NetType.I2C).scan()
    label = params.get("label")
    data = {"target": target, "addresses": addresses}
    if label:
        results[label] = data
    return data


@action("i2c_read")
def handle_i2c_read(target: str, params: dict, results: dict) -> dict:
    """Read bytes from an I2C device."""
    from lager import NetType
    address = params["address"]
    num_bytes = params.get("num_bytes", 1)
    rx = _get_net(target, NetType.I2C).read(address, num_bytes)
    label = params.get("label")
    data = {"target": target, "address": address, "rx_data": rx}
    if label:
        results[label] = data
    return data


@action("i2c_write")
def handle_i2c_write(target: str, params: dict, results: dict) -> dict:
    """Write bytes to an I2C device."""
    from lager import NetType
    address = params["address"]
    tx_data = params.get("data", [])
    _get_net(target, NetType.I2C).write(address, tx_data)
    return {"target": target, "address": address, "data": tx_data}


@action("i2c_write_read")
def handle_i2c_write_read(target: str, params: dict, results: dict) -> dict:
    """Write then read from an I2C device in one transaction."""
    from lager import NetType
    address = params["address"]
    tx_data = params.get("data", [])
    num_bytes = params.get("num_bytes", 1)
    rx = _get_net(target, NetType.I2C).write_read(address, tx_data, num_bytes)
    label = params.get("label")
    data = {"target": target, "address": address, "tx_data": tx_data, "rx_data": rx}
    if label:
        results[label] = data
    return data


# ---------------------------------------------------------------------------
# Power supply handlers
# ---------------------------------------------------------------------------

@action("set_voltage")
def handle_set_voltage(target: str, params: dict, results: dict) -> dict:
    """Set voltage on a power supply net."""
    from lager import NetType
    voltage = params["voltage"]
    _get_net(target, NetType.PowerSupply).set_voltage(voltage)
    return {"target": target, "voltage": voltage}


@action("set_current")
def handle_set_current(target: str, params: dict, results: dict) -> dict:
    """Set current limit on a power supply net."""
    from lager import NetType
    current = params["current"]
    _get_net(target, NetType.PowerSupply).set_current(current)
    return {"target": target, "current": current}


@action("enable_supply")
def handle_enable_supply(target: str, params: dict, results: dict) -> dict:
    """Enable a power supply output."""
    from lager import NetType
    _get_net(target, NetType.PowerSupply).enable()
    return {"target": target, "enabled": True}


@action("disable_supply")
def handle_disable_supply(target: str, params: dict, results: dict) -> dict:
    """Disable a power supply output."""
    from lager import NetType
    _get_net(target, NetType.PowerSupply).disable()
    return {"target": target, "enabled": False}


@action("measure")
def handle_measure(target: str, params: dict, results: dict) -> dict:
    """Read voltage, current, or power from a power supply net."""
    from lager import NetType
    measurement_type = params.get("type", "voltage")
    net = _get_net(target, NetType.PowerSupply)
    value = getattr(net, measurement_type)()
    label = params.get("label")
    data = {"target": target, "type": measurement_type, "value": value}
    if label:
        results[label] = data
    return data


# ---------------------------------------------------------------------------
# ADC / DAC handlers
# ---------------------------------------------------------------------------

@action("adc_read")
def handle_adc_read(target: str, params: dict, results: dict) -> dict:
    """Read voltage from an ADC net."""
    from lager import NetType
    value = _get_net(target, NetType.ADC).input()
    label = params.get("label")
    data = {"target": target, "value": value}
    if label:
        results[label] = data
    return data


@action("dac_set")
def handle_dac_set(target: str, params: dict, results: dict) -> dict:
    """Set output voltage on a DAC net."""
    from lager import NetType
    voltage = params["voltage"]
    _get_net(target, NetType.DAC).output(voltage)
    return {"target": target, "voltage": voltage}


# ---------------------------------------------------------------------------
# USB handlers
# ---------------------------------------------------------------------------

@action("usb_enable")
def handle_usb_enable(target: str, params: dict, results: dict) -> dict:
    """Enable a USB hub port."""
    from lager import NetType
    _get_net(target, NetType.Usb).enable()
    return {"target": target, "enabled": True}


@action("usb_disable")
def handle_usb_disable(target: str, params: dict, results: dict) -> dict:
    """Disable a USB hub port."""
    from lager import NetType
    _get_net(target, NetType.Usb).disable()
    return {"target": target, "enabled": False}


# ---------------------------------------------------------------------------
# Debug handlers
# ---------------------------------------------------------------------------

@action("debug_connect")
def handle_debug_connect(target: str, params: dict, results: dict) -> dict:
    """Connect the debug probe to the DUT."""
    from lager import NetType
    kwargs: dict[str, Any] = {}
    if "speed" in params:
        kwargs["speed"] = params["speed"]
    if "transport" in params:
        kwargs["transport"] = params["transport"]
    _get_net(target, NetType.Debug).connect(**kwargs)
    return {"target": target, "connected": True, **kwargs}


@action("debug_disconnect")
def handle_debug_disconnect(target: str, params: dict, results: dict) -> dict:
    """Disconnect the debug probe."""
    from lager import NetType
    _get_net(target, NetType.Debug).disconnect()
    return {"target": target, "connected": False}


@action("debug_flash")
def handle_debug_flash(target: str, params: dict, results: dict) -> dict:
    """Flash firmware to the DUT."""
    from lager import NetType
    firmware_path = params["firmware_path"]
    _get_net(target, NetType.Debug).flash(firmware_path)
    return {"target": target, "firmware_path": firmware_path}


@action("debug_reset")
def handle_debug_reset(target: str, params: dict, results: dict) -> dict:
    """Reset the DUT via debug probe."""
    from lager import NetType
    halt = params.get("halt", False)
    _get_net(target, NetType.Debug).reset(halt=halt)
    return {"target": target, "halt": halt}


@action("debug_erase")
def handle_debug_erase(target: str, params: dict, results: dict) -> dict:
    """Erase the DUT flash memory."""
    from lager import NetType
    _get_net(target, NetType.Debug).erase()
    return {"target": target, "erased": True}


@action("debug_read_memory")
def handle_debug_read_memory(target: str, params: dict, results: dict) -> dict:
    """Read a region of DUT memory."""
    from lager import NetType
    address = params["address"]
    length = params.get("length", 4)
    data = _get_net(target, NetType.Debug).read_memory(address, length)
    label = params.get("label")
    result = {"target": target, "address": address, "length": length, "data": data}
    if label:
        results[label] = result
    return result


# ---------------------------------------------------------------------------
# RTT handlers (via debug.rtt() context manager)
# ---------------------------------------------------------------------------

@action("rtt_write")
def handle_rtt_write(target: str, params: dict, results: dict) -> dict:
    """Write data to a DUT RTT channel."""
    rtt = _get_rtt_session(target, params)
    data_str = params.get("data", "")
    rtt.write(data_str.encode("utf-8", errors="ignore"))
    return {"target": target, "data": data_str}


@action("rtt_expect")
def handle_rtt_expect(target: str, params: dict, results: dict) -> dict:
    """Read from an RTT channel until a pattern matches or timeout."""
    rtt = _get_rtt_session(target, params)
    pattern = params.get("pattern", "")
    timeout_ms = params.get("timeout_ms", 5000)
    timeout_s = timeout_ms / 1000.0

    chunks: list[str] = []
    matched = False
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        raw = rtt.read_some(timeout=min(0.5, deadline - time.time()))
        if raw:
            text = raw.decode("utf-8", errors="ignore")
            chunks.append(text)
            if pattern in "".join(chunks):
                matched = True
                break

    label = params.get("label")
    output_text = "".join(chunks)
    data = {"target": target, "pattern": pattern, "matched": matched, "output": output_text}
    if label:
        results[label] = data
    return data


# ---------------------------------------------------------------------------
# WattMeter / Thermocouple handlers
# ---------------------------------------------------------------------------

@action("watt_read")
def handle_watt_read(target: str, params: dict, results: dict) -> dict:
    """Read power (watts) from a watt meter net."""
    from lager import NetType
    value = _get_net(target, NetType.WattMeter).read()
    label = params.get("label")
    data = {"target": target, "value": value}
    if label:
        results[label] = data
    return data


@action("watt_read_all")
def handle_watt_read_all(target: str, params: dict, results: dict) -> dict:
    """Read all measurements (voltage, current, power) from a watt meter."""
    from lager import NetType
    readings = _get_net(target, NetType.WattMeter).read_all()
    label = params.get("label")
    data = {"target": target, "readings": readings}
    if label:
        results[label] = data
    return data


@action("tc_read")
def handle_tc_read(target: str, params: dict, results: dict) -> dict:
    """Read temperature from a thermocouple net."""
    from lager import NetType
    value = _get_net(target, NetType.Thermocouple).read()
    label = params.get("label")
    data = {"target": target, "value": value}
    if label:
        results[label] = data
    return data


# ---------------------------------------------------------------------------
# Wait handler
# ---------------------------------------------------------------------------

@action("wait")
def handle_wait(target: str | None, params: dict, results: dict) -> dict:
    """Sleep for a given number of milliseconds."""
    ms = params.get("ms", 0)
    time.sleep(ms / 1000.0)
    return {"ms": ms}


# ---------------------------------------------------------------------------
# Step execution
# ---------------------------------------------------------------------------

def execute_step(step: dict, results: dict, step_results: list, errors: list) -> None:
    """Execute a single scenario step."""
    action_name = step.get("action", "")
    target = step.get("target")
    params = step.get("params", {})
    on_failure = step.get("on_failure", "abort")

    handler = _HANDLER_REGISTRY.get(action_name)
    if handler is None:
        msg = f"Unknown action: {action_name}"
        errors.append(msg)
        step_results.append({
            "action": action_name,
            "target": target,
            "success": False,
            "error": msg,
        })
        if on_failure == "abort":
            raise RuntimeError(msg)
        return

    t0 = time.time()
    try:
        data = handler(target, params, results)
        elapsed_ms = (time.time() - t0) * 1000
        step_results.append({
            "action": action_name,
            "target": target,
            "label": params.get("label"),
            "success": True,
            "data": data,
            "duration_ms": round(elapsed_ms, 2),
        })
    except Exception as exc:
        elapsed_ms = (time.time() - t0) * 1000
        msg = f"{action_name} on {target}: {exc}"
        errors.append(msg)
        step_results.append({
            "action": action_name,
            "target": target,
            "label": params.get("label"),
            "success": False,
            "error": str(exc),
            "duration_ms": round(elapsed_ms, 2),
        })
        if on_failure == "abort":
            raise


# ---------------------------------------------------------------------------
# Assertion evaluation
# ---------------------------------------------------------------------------

def evaluate_assertions(assertions: list[dict], results: dict) -> list[dict]:
    """Evaluate assertion expressions against collected results."""
    outcomes = []
    for assertion in assertions:
        name = assertion.get("name", "unnamed")
        expression = assertion.get("expression", "False")
        severity = assertion.get("severity", "error")
        try:
            passed = bool(eval(expression, {"__builtins__": {}}, {"results": results}))
            outcomes.append({"name": name, "passed": passed, "severity": severity})
        except Exception as exc:
            outcomes.append({
                "name": name,
                "passed": False,
                "severity": severity,
                "detail": f"eval error: {exc}",
            })
    return outcomes


# ---------------------------------------------------------------------------
# Main runner
# ---------------------------------------------------------------------------

def run(scenario_json: str) -> dict:
    """Execute a scenario from JSON and return structured results."""
    scenario = json.loads(scenario_json)
    results: dict[str, Any] = {}
    step_results: list[dict] = []
    errors: list[str] = []

    t0 = time.time()

    try:
        for step in scenario.get("setup", []):
            execute_step(step, results, step_results, errors)

        aborted = False
        try:
            for step in scenario.get("steps", []):
                execute_step(step, results, step_results, errors)
        except Exception:
            aborted = True

        for step in scenario.get("cleanup", []):
            try:
                execute_step(step, results, step_results, errors)
            except Exception:
                pass
    finally:
        _cleanup_all()

    assertion_outcomes = evaluate_assertions(
        scenario.get("assertions", []), results
    )

    elapsed_ms = (time.time() - t0) * 1000

    all_assertions_passed = all(
        a["passed"] for a in assertion_outcomes
        if a.get("severity") == "error"
    )
    all_steps_ok = all(s["success"] for s in step_results)

    if aborted:
        status = "aborted"
    elif errors:
        status = "error"
    elif not all_assertions_passed:
        status = "failed"
    elif all_steps_ok:
        status = "passed"
    else:
        status = "failed"

    return {
        "scenario_name": scenario.get("name", "unnamed"),
        "status": status,
        "duration_ms": round(elapsed_ms, 2),
        "step_results": step_results,
        "assertions": assertion_outcomes,
        "results": results,
        "errors": errors,
    }


# ---------------------------------------------------------------------------
# Entry point -- invoked by lager python
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    scenario_json = os.environ.get("LAGER_SCENARIO", "")
    if not scenario_json:
        print(json.dumps({"status": "error", "errors": ["LAGER_SCENARIO env var not set"]}))
        sys.exit(1)

    try:
        output = run(scenario_json)
        print(json.dumps(output))
    except Exception:
        print(json.dumps({
            "status": "error",
            "errors": [traceback.format_exc()],
        }))
        sys.exit(1)
