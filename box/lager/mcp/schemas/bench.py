# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

"""Bench definition schema -- describes a Lager box's physical configuration."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from .dut import DocRef, DUTContext, SubSystem


class VoltageRange(BaseModel):
    """Voltage range for a net or voltage domain."""

    min_v: float = 0.0
    max_v: float
    nominal_v: float | None = None


# Backward-compatible alias. ``DUTSlot`` was the v1 name; ``DUTContext`` is
# the richer v2 schema. Both names resolve to the same model so existing
# imports keep working.
DUTSlot = DUTContext


class InstrumentDescriptor(BaseModel):
    """Describes an instrument attached to the box."""

    name: str
    instrument_type: str  # e.g. "rigol_dp800", "labjack_t7", "aardvark"
    connection: str  # e.g. VISA address, serial path, USB ID
    channels: list[str] = Field(default_factory=list)
    firmware_version: str | None = None
    capabilities: list[str] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)


class RoutingEntry(BaseModel):
    """Describes how a net is routed through muxes or switches."""

    net_name: str
    instrument: str
    channel: str
    mux_path: list[str] = Field(default_factory=list)


class InstrumentHealth(BaseModel):
    """Health/calibration status of a single instrument."""

    instrument: str
    reachable: bool = True
    last_calibration: str | None = None
    notes: str | None = None


class CalibrationStatus(BaseModel):
    """Aggregate calibration/health status for the bench."""

    healthy: bool = True
    instruments: list[InstrumentHealth] = Field(default_factory=list)
    last_check: str | None = None


class BenchDefinition(BaseModel):
    """
    Top-level bench definition for a single Lager box.

    Loaded from /etc/lager/bench.json (static config) and augmented at
    runtime from /etc/lager/saved_nets.json (dynamic net config) and
    instrument discovery.
    """

    box_id: str = ""
    hostname: str = ""
    version: str = ""
    dut_slots: list[DUTContext] = Field(default_factory=list)
    instruments: list[InstrumentDescriptor] = Field(default_factory=list)
    nets: list["NetDescriptor"] = Field(default_factory=list)  # forward ref
    interfaces: list["InterfaceDescriptor"] = Field(default_factory=list)  # forward ref
    routing: list[RoutingEntry] = Field(default_factory=list)
    constraints: "SafetyConstraints | None" = None
    calibration: CalibrationStatus = Field(default_factory=CalibrationStatus)

    # Populated by the capability graph engine after loading
    capability_bindings: list[dict[str, Any]] = Field(default_factory=list)

    def primary_dut(self) -> DUTContext | None:
        """Return the first active DUTContext, or the first one if none active."""
        for d in self.dut_slots:
            if d.active:
                return d
        return self.dut_slots[0] if self.dut_slots else None


# Deferred imports resolved after all schemas are defined
def _rebuild_refs() -> None:
    from .net import InterfaceDescriptor, NetDescriptor  # noqa: F811
    from .safety_types import SafetyConstraints  # noqa: F811

    BenchDefinition.model_rebuild()


_rebuild_refs()


__all__ = [
    "BenchDefinition",
    "CalibrationStatus",
    "DocRef",
    "DUTContext",
    "DUTSlot",
    "InstrumentDescriptor",
    "InstrumentHealth",
    "RoutingEntry",
    "SubSystem",
    "VoltageRange",
]
