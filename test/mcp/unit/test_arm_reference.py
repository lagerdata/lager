# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

"""The robot-arm entries an MCP agent reads before it drives a Dexarm.

There are no arm-specific MCP tools: an agent drives the arm from a
``lager python`` script it writes from ``lager://reference/Arm``. So what that
reference says is the arm's only MCP surface, and these tests pin the parts
that keep an agent from damaging the arm or misreading its API.
"""

from lager.mcp.data.api_reference import API_REFERENCE, get_reference_for_type
from lager.mcp.engine.net_types import reference_key


def _methods():
    return {m["name"]: m for m in API_REFERENCE["Arm"]["methods"]}


def test_arm_net_type_resolves_to_the_arm_reference():
    assert reference_key("arm") == "Arm"
    assert get_reference_for_type("arm") is API_REFERENCE["Arm"]


def test_reference_is_introspected_from_the_dexarm_driver():
    assert API_REFERENCE["Arm"]["source_module"] == "lager.automation.arm.rotrics.Dexarm"


def test_acceleration_read_and_write_are_advertised():
    methods = _methods()
    assert "get_acceleration" in methods
    # An omitted retract keeps the arm's value; it used to default to 60.
    assert "retract_acceleration: Optional[int] = None" in methods["set_acceleration"]["sig"]


def test_agents_are_warned_off_m889():
    # save_position and read_and_save_position are public driver methods, so
    # introspection advertises them. Calling either overwrites the arm's stored
    # calibration.
    methods = _methods()
    assert {"save_position", "read_and_save_position"} <= set(methods)
    gotchas = " ".join(API_REFERENCE["Arm"]["gotchas"])
    assert "Never call save_position() or read_and_save_position()" in gotchas
    assert "M889" in gotchas


def test_agents_are_warned_that_disable_motor_drops_the_arm():
    gotchas = " ".join(API_REFERENCE["Arm"]["gotchas"])
    assert "disable_motor()" in gotchas
    assert "sag" in gotchas
