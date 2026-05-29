# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

"""Net and interface descriptor schemas."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from .bench import VoltageRange


class SafetyLimits(BaseModel):
    """Electrical safety limits for a net."""

    max_voltage: float | None = None
    max_current: float | None = None
    max_power: float | None = None
    notes: str | None = None


class NetDescriptor(BaseModel):
    """
    Describes a single net (named hardware connection) on the bench.

    Enriched beyond the raw saved_nets.json entry with electrical metadata,
    roles, safety limits, and aliases for agent reasoning.

    User-authored metadata is intentionally minimal: a single ``purpose``
    sentence (what this wire does on the DUT) plus optional ``notes`` for
    gotchas, jumper positions, scope probe points, etc. The legacy
    ``description`` / ``dut_connection`` / ``test_hints`` fields are
    preserved on the model so old ``saved_nets.json`` files keep loading,
    but the bench loader folds them into ``purpose`` / ``notes`` and the
    TUI no longer exposes them as separate inputs.
    """

    name: str
    aliases: list[str] = Field(default_factory=list)
    net_type: str  # matches NetType enum values: "spi", "i2c", "power-supply", etc.
    electrical_type: str = "unknown"  # "power", "analog", "digital", "protocol"
    voltage_domain: VoltageRange | None = None
    directionality: str = "bidirectional"  # "input", "output", "bidirectional"
    controllable: bool = True
    observable: bool = True
    roles: list[str] = Field(default_factory=list)  # CapabilityRole values
    safety_limits: SafetyLimits | None = None
    timing_constraints: dict[str, Any] | None = None
    instrument: str = ""
    channel: str = ""
    params: dict[str, Any] = Field(default_factory=dict)

    # Canonical user-authored metadata.
    purpose: str = ""
    """One-sentence description of what this net does on the DUT.

    Example: *"DUT debug CLI over UART; primary command/response channel"*.
    """

    notes: str = ""
    """Optional markdown for gotchas, jumper positions, scope probe points."""

    tags: list[str] = Field(default_factory=list)

    # ---- Legacy fields ---------------------------------------------------
    # Kept for backward compatibility with older saved_nets.json / bench.json
    # files. The bench loader merges these into ``purpose``/``notes`` at
    # load time, and the TUI no longer renders them as standalone inputs.
    description: str = ""
    dut_connection: str = ""
    test_hints: list[str] = Field(default_factory=list)


class InterfaceDescriptor(BaseModel):
    """
    Describes a protocol interface composed of one or more nets.

    For example an SPI interface bundles MOSI, MISO, SCK, and CS nets.
    """

    name: str
    protocol: str  # "spi", "i2c", "uart", "jtag"
    nets: list[str] = Field(default_factory=list)
    roles: list[str] = Field(default_factory=list)
    max_frequency: int | None = None
    supported_modes: list[str] | None = None
    notes: str | None = None
