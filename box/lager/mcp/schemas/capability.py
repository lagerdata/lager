# Copyright 2024-2026 Lager Data LLC
# SPDX-License-Identifier: Apache-2.0

"""Capability graph schema -- models what a bench *can do*, not just what is connected."""

from __future__ import annotations

from enum import Enum
from typing import Any

from pydantic import BaseModel, Field


class CapabilityRole(str, Enum):
    """Typed roles a bench element can play."""

    OBSERVE = "observe"
    DRIVE = "drive"
    MEASURE = "measure"
    SOURCE_POWER = "source_power"
    SINK_POWER = "sink_power"
    PROTOCOL_MASTER = "protocol_master"
    PROTOCOL_CONTROLLER = "protocol_controller"
    PROTOCOL_TARGET = "protocol_target"
    EMULATE_DEVICE = "emulate_device"
    FAULT_INJECT = "fault_inject"
    CONTROL_STATE = "control_state"
    SWEEP_VOLTAGE = "sweep_voltage"
    SWEEP_ANALOG = "sweep_analog"
    WAVEFORM_GEN = "waveform_gen"
    CAPTURE_WAVEFORM = "capture_waveform"
    CAPTURE_LOGIC = "capture_logic"
    CAPTURE_PROTOCOL = "capture_protocol"
    RUN_LOCAL_PROGRAM = "run_local_program"
    FLASH_FIRMWARE = "flash_firmware"


class CapabilityNode(BaseModel):
    """A single capability the bench can perform."""

    role: CapabilityRole
    target: str  # net or interface name
    parameters: dict[str, Any] | None = None
    confidence: float = 1.0  # 0.0-1.0
    notes: str | None = None


class CapabilityGraph(BaseModel):
    """
    The full capability graph for a bench.

    Built by engine/capability_graph.py from the BenchDefinition.
    """

    box_id: str = ""
    nodes: list[CapabilityNode] = Field(default_factory=list)

    def by_role(self, role: CapabilityRole) -> list[CapabilityNode]:
        """Return all nodes with the given role."""
        return [n for n in self.nodes if n.role == role]

    def by_target(self, target: str) -> list[CapabilityNode]:
        """Return all capability nodes for a specific net/interface."""
        return [n for n in self.nodes if n.target == target]

    def has_role(self, role: CapabilityRole) -> bool:
        """Check whether any node provides the given role."""
        return any(n.role == role for n in self.nodes)

    def roles_for_target(self, target: str) -> list[CapabilityRole]:
        """List all roles available on a specific target."""
        return [n.role for n in self.nodes if n.target == target]

    def targets_for_role(self, role: CapabilityRole) -> list[str]:
        """List all targets that provide a given role."""
        return [n.target for n in self.nodes if n.role == role]
