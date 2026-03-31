# Copyright 2024-2026 Lager Data LLC
# SPDX-License-Identifier: Apache-2.0

"""
MCP tools for coarse-grained scenario execution.

run_scenario is the PRIMARY tool for hardware-in-the-loop testing.
It sends a multi-step plan to the on-box interpreter which executes
ALL steps locally with no agent round trips between steps.

run_hil_program is an escape hatch for arbitrary Python on the box.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from mcp.server.fastmcp import Context

from ..server import mcp

logger = logging.getLogger(__name__)


def _preflight_scenario(
    scenario_steps: list[dict[str, Any]],
) -> dict[str, Any] | None:
    """Validate scenario steps against the bench before execution.

    Returns an error dict if preflight fails, or None if OK.
    """
    from ..safety import preflight_check
    from ..server_state import get_bench

    bench = get_bench()
    net_names = {n.name for n in bench.nets}
    constraints = bench.constraints

    for i, step in enumerate(scenario_steps):
        target = step.get("target")
        if target and net_names and target not in net_names:
            return {
                "error": (
                    f"Step {i} targets net '{target}' which does not exist "
                    f"on this bench. Known nets: {sorted(net_names)}"
                ),
            }

        params = step.get("params", {})
        action = step.get("action", "")
        if constraints and (params.get("voltage") is not None or params.get("current") is not None):
            result = preflight_check(
                tool_name=action,
                params=params,
                constraints=constraints,
                target_net=target,
            )
            if not result.allowed:
                return {
                    "error": (
                        f"Safety preflight blocked step {i} ({action} on "
                        f"{target}): {result.blocked_reason}"
                    ),
                    "mitigations": result.mitigations,
                }

    return None


@mcp.tool()
async def run_scenario(scenario_json: str, ctx: Context) -> str:
    """Execute a multi-step HIL scenario entirely on-box.

    This is the PREFERRED tool for hardware testing. All steps execute
    locally with sub-millisecond latency between them — no round trips
    back to the agent. A 10-step scenario costs one round trip, not ten.

    Args:
        scenario_json: JSON string with ``name`` and ``steps``.
            Optional: ``setup``, ``cleanup``, ``assertions``, ``timeout_s``.
            Each step has ``action``, optional ``target`` and ``params``.

    Available actions:
        Power: set_voltage, set_current, enable_supply, disable_supply, measure
        Battery: battery_enable, battery_disable, battery_soc, battery_voc,
                 battery_set, battery_state
        ELoad: eload_set, eload_enable, eload_disable, eload_state
        Energy: energy_read, energy_stats
        GPIO: gpio_set, gpio_read, gpio_wait
        SPI: spi_config, spi_transfer, spi_read, spi_write
        I2C: i2c_config, i2c_scan, i2c_read, i2c_write, i2c_write_read
        UART: uart_send, uart_expect
        Debug: debug_connect, debug_disconnect, debug_flash, debug_reset,
               debug_erase, debug_read_memory
        RTT: rtt_write, rtt_expect
        ADC/DAC: adc_read, dac_set
        USB: usb_enable, usb_disable
        Measurement: watt_read, watt_read_all, tc_read
        Timing: wait (params: ms)

    Returns:
        JSON with status, step_results, assertion outcomes, and labeled results.
    """
    from ..schemas.scenario import Scenario
    from ..engine.scenario_executor import execute_scenario

    try:
        payload = json.loads(scenario_json)
    except json.JSONDecodeError as exc:
        return json.dumps({"error": f"Invalid JSON: {exc}"})

    try:
        scenario = Scenario(**payload)
    except Exception as exc:
        return json.dumps({"error": f"Invalid scenario schema: {exc}"})

    all_steps = (
        [s.model_dump() for s in scenario.setup]
        + [s.model_dump() for s in scenario.steps]
        + [s.model_dump() for s in scenario.cleanup]
    )
    total_steps = len(all_steps)

    preflight_err = _preflight_scenario(all_steps)
    if preflight_err:
        return json.dumps(preflight_err, indent=2)

    await ctx.report_progress(progress=0, total=total_steps)
    result = execute_scenario(scenario_json, timeout_s=scenario.timeout_s)

    completed = len(result.get("step_results", []))
    await ctx.report_progress(progress=completed, total=total_steps)
    return json.dumps(result, indent=2, default=str)


@mcp.tool()
async def run_hil_program(code: str, ctx: Context, timeout: int = 120) -> str:
    """Run a raw Python program on the box (escape hatch).

    Use this for ad-hoc hardware interaction when the structured
    scenario DSL is too restrictive. The code executes on-box with
    full access to the lager Python API (lager.Net, lager.NetType, etc.).

    Args:
        code: Python source code to execute on-box.
        timeout: Execution timeout in seconds (default: 120).

    Returns:
        stdout/stderr output from the program.
    """
    from ..engine.scenario_executor import execute_script

    await ctx.report_progress(progress=0, total=2)
    result = execute_script(code, timeout_s=timeout)
    await ctx.report_progress(progress=2, total=2)
    return json.dumps(result, indent=2, default=str)
