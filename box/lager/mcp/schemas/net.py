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
    gotchas, jumper positions, scope probe points, etc. ``tags`` are short
    keywords the planning tools match on.
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
