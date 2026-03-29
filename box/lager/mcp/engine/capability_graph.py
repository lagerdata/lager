# Copyright 2024-2026 Lager Data LLC
# SPDX-License-Identifier: Apache-2.0

"""
Build a CapabilityGraph from a BenchDefinition.

The graph is *derived* -- each net's type and instrument metadata are
mapped to a set of CapabilityNodes describing what the bench can
actually *do*, not just what is physically connected.

Rules are intentionally conservative: confidence < 1.0 when the role
depends on instrument firmware or configuration that cannot be
verified statically.
"""

from __future__ import annotations

from ..schemas.bench import BenchDefinition
from ..schemas.capability import CapabilityGraph, CapabilityNode, CapabilityRole
from ..schemas.net import NetDescriptor

# ---------------------------------------------------------------------------
# Per-net-type derivation rules
# ---------------------------------------------------------------------------

_ROLE_MAP: dict[str, list[tuple[CapabilityRole, float, dict | None]]] = {
    # (role, confidence, optional parameter hints)
    "power-supply": [
        (CapabilityRole.SOURCE_POWER, 1.0, None),
        (CapabilityRole.DRIVE, 1.0, None),
        (CapabilityRole.MEASURE, 0.9, None),
        (CapabilityRole.SWEEP_VOLTAGE, 0.9, None),
    ],
    "power-supply-2q": [
        (CapabilityRole.SOURCE_POWER, 1.0, None),
        (CapabilityRole.SINK_POWER, 1.0, None),
        (CapabilityRole.DRIVE, 1.0, None),
        (CapabilityRole.MEASURE, 0.9, None),
        (CapabilityRole.SWEEP_VOLTAGE, 0.9, None),
    ],
    "battery": [
        (CapabilityRole.SOURCE_POWER, 1.0, None),
        (CapabilityRole.DRIVE, 1.0, None),
        (CapabilityRole.MEASURE, 0.9, None),
        (CapabilityRole.SWEEP_VOLTAGE, 0.9, None),
    ],
    "eload": [
        (CapabilityRole.SINK_POWER, 1.0, None),
        (CapabilityRole.MEASURE, 0.9, None),
    ],
    "solar": [
        (CapabilityRole.SOURCE_POWER, 1.0, None),
        (CapabilityRole.DRIVE, 1.0, None),
        (CapabilityRole.MEASURE, 0.9, None),
    ],
    "analog": [
        (CapabilityRole.OBSERVE, 1.0, None),
        (CapabilityRole.MEASURE, 1.0, None),
        (CapabilityRole.CAPTURE_WAVEFORM, 1.0, None),
    ],
    "scope": [
        (CapabilityRole.OBSERVE, 1.0, None),
        (CapabilityRole.MEASURE, 1.0, None),
        (CapabilityRole.CAPTURE_WAVEFORM, 1.0, None),
    ],
    "logic": [
        (CapabilityRole.OBSERVE, 1.0, None),
        (CapabilityRole.CAPTURE_LOGIC, 1.0, None),
    ],
    "adc": [
        (CapabilityRole.OBSERVE, 1.0, None),
        (CapabilityRole.MEASURE, 1.0, None),
    ],
    "dac": [
        (CapabilityRole.DRIVE, 1.0, None),
        (CapabilityRole.SWEEP_ANALOG, 0.9, None),
        (CapabilityRole.WAVEFORM_GEN, 0.7, None),
    ],
    "gpio": [
        (CapabilityRole.DRIVE, 1.0, None),
        (CapabilityRole.OBSERVE, 1.0, None),
        (CapabilityRole.CONTROL_STATE, 1.0, None),
    ],
    "spi": [
        (CapabilityRole.PROTOCOL_MASTER, 1.0, None),
        (CapabilityRole.CAPTURE_PROTOCOL, 0.8, None),
    ],
    "i2c": [
        (CapabilityRole.PROTOCOL_CONTROLLER, 1.0, None),
        (CapabilityRole.CAPTURE_PROTOCOL, 0.8, None),
    ],
    "uart": [
        (CapabilityRole.OBSERVE, 1.0, None),
        (CapabilityRole.PROTOCOL_MASTER, 0.9, None),
    ],
    "debug": [
        (CapabilityRole.FLASH_FIRMWARE, 1.0, None),
        (CapabilityRole.CONTROL_STATE, 1.0, None),
    ],
    "thermocouple": [
        (CapabilityRole.OBSERVE, 1.0, None),
        (CapabilityRole.MEASURE, 1.0, None),
    ],
    "watt-meter": [
        (CapabilityRole.OBSERVE, 1.0, None),
        (CapabilityRole.MEASURE, 1.0, None),
    ],
    "energy-analyzer": [
        (CapabilityRole.OBSERVE, 1.0, None),
        (CapabilityRole.MEASURE, 1.0, None),
    ],
    "usb": [
        (CapabilityRole.CONTROL_STATE, 0.9, None),
    ],
    "wifi": [
        (CapabilityRole.OBSERVE, 0.7, None),
    ],
    "waveform": [
        (CapabilityRole.OBSERVE, 1.0, None),
        (CapabilityRole.CAPTURE_WAVEFORM, 1.0, None),
    ],
    "webcam": [
        (CapabilityRole.OBSERVE, 0.5, None),
    ],
    "actuate": [
        (CapabilityRole.DRIVE, 0.8, None),
        (CapabilityRole.CONTROL_STATE, 0.8, None),
    ],
    "mikrotik": [
        (CapabilityRole.OBSERVE, 0.7, None),
    ],
    "router": [
        (CapabilityRole.OBSERVE, 0.7, None),
    ],
}


def _nodes_for_net(net: NetDescriptor) -> list[CapabilityNode]:
    """Derive capability nodes from a single net."""
    entries = _ROLE_MAP.get(net.net_type, [])
    nodes: list[CapabilityNode] = []
    for role, confidence, params in entries:
        nodes.append(
            CapabilityNode(
                role=role,
                target=net.name,
                parameters=params,
                confidence=confidence,
            )
        )
    return nodes


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def build_capability_graph(bench: BenchDefinition) -> CapabilityGraph:
    """
    Derive the full capability graph for a bench definition.

    Returns a CapabilityGraph whose nodes represent every role the bench
    can play, keyed to the specific nets / interfaces that provide it.
    """
    nodes: list[CapabilityNode] = []

    for net in bench.nets:
        nodes.extend(_nodes_for_net(net))

    # Every box supports local program execution
    nodes.append(
        CapabilityNode(
            role=CapabilityRole.RUN_LOCAL_PROGRAM,
            target="_box",
            confidence=1.0,
            notes="On-box Python execution via lager python service",
        )
    )

    return CapabilityGraph(box_id=bench.box_id, nodes=nodes)
