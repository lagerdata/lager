# Copyright 2024-2026 Lager Data LLC
# SPDX-License-Identifier: Apache-2.0

"""
Heuristic engine -- maps test descriptions to bench capability requirements
and produces suitability reports.

v0: ~5 core test types covering the essential HIL patterns.
"""

from __future__ import annotations

import re
from typing import Sequence

from ..schemas.capability import CapabilityGraph, CapabilityNode, CapabilityRole
from ..schemas.heuristic import (
    CapabilityMatch,
    Substitution,
    SuitabilityReport,
    TestRequirement,
)

# ---------------------------------------------------------------------------
# Built-in test requirement library (~5 core types)
# ---------------------------------------------------------------------------

BUILTIN_REQUIREMENTS: list[TestRequirement] = [
    TestRequirement(
        test_type="gpio_validation",
        description="Drive and read GPIO lines to validate DUT GPIO behavior.",
        required_capabilities=[
            CapabilityRole.DRIVE,
            CapabilityRole.OBSERVE,
            CapabilityRole.SOURCE_POWER,
        ],
        required_protocols=[],
        required_net_types=["gpio", "power-supply"],
    ),
    TestRequirement(
        test_type="gpio_button_validation",
        description=(
            "Drive a DUT button input via GPIO, confirm electrical toggle "
            "with GPI, and verify the DUT CLI over UART reports press/release."
        ),
        required_capabilities=[
            CapabilityRole.DRIVE,
            CapabilityRole.OBSERVE,
            CapabilityRole.PROTOCOL_MASTER,
        ],
        recommended_capabilities=[
            CapabilityRole.SOURCE_POWER,
        ],
        required_protocols=["uart"],
        required_net_types=["gpio", "uart"],
    ),
    TestRequirement(
        test_type="firmware_flash_and_boot",
        description="Flash firmware, reset DUT, and verify it boots correctly.",
        required_capabilities=[
            CapabilityRole.FLASH_FIRMWARE,
            CapabilityRole.CONTROL_STATE,
            CapabilityRole.SOURCE_POWER,
        ],
        recommended_capabilities=[
            CapabilityRole.OBSERVE,
        ],
        required_protocols=[],
        required_net_types=["debug", "power-supply"],
    ),
    TestRequirement(
        test_type="power_consumption",
        description="Measure DUT power consumption across operating modes.",
        required_capabilities=[
            CapabilityRole.SOURCE_POWER,
            CapabilityRole.MEASURE,
        ],
        recommended_capabilities=[
            CapabilityRole.OBSERVE,
        ],
        required_protocols=[],
        required_net_types=["power-supply"],
        notes="Watt meter or energy analyzer recommended for accurate power measurement.",
    ),
    TestRequirement(
        test_type="spi_slave_validation",
        description="Bench acts as SPI master, sends scripted transactions to DUT SPI slave.",
        required_capabilities=[
            CapabilityRole.PROTOCOL_MASTER,
            CapabilityRole.SOURCE_POWER,
            CapabilityRole.CONTROL_STATE,
        ],
        recommended_capabilities=[
            CapabilityRole.CAPTURE_LOGIC,
        ],
        required_protocols=["spi"],
        required_net_types=["spi", "power-supply"],
        notes="Requires bench SPI master (e.g., Aardvark, LabJack).",
    ),
    TestRequirement(
        test_type="uart_loopback",
        description="UART loopback or echo test between bench and DUT.",
        required_capabilities=[
            CapabilityRole.PROTOCOL_MASTER,
            CapabilityRole.OBSERVE,
            CapabilityRole.SOURCE_POWER,
        ],
        required_protocols=["uart"],
        required_net_types=["uart", "power-supply"],
    ),
]

_REQUIREMENT_INDEX: dict[str, TestRequirement] = {r.test_type: r for r in BUILTIN_REQUIREMENTS}

# ---------------------------------------------------------------------------
# Keyword → test-type matching for free-form descriptions
# ---------------------------------------------------------------------------

_KEYWORD_MAP: list[tuple[list[str], str]] = [
    (["spi slave", "spi target", "spi master", "spi flash", "spi driver", "qspi"], "spi_slave_validation"),
    (["uart", "serial", "loopback"], "uart_loopback"),
    (["power consumption", "power measure", "current draw", "energy"], "power_consumption"),
    (["gpio button", "button press", "button release", "button validation"], "gpio_button_validation"),
    (["gpio", "digital io"], "gpio_validation"),
    (["flash", "firmware", "boot"], "firmware_flash_and_boot"),
]


def infer_requirements(description: str) -> TestRequirement:
    """
    Given a natural-language test description, return the closest
    matching TestRequirement from the built-in library.

    Falls back to a generic requirement if no match is found.
    """
    desc_lower = description.lower()

    if desc_lower in _REQUIREMENT_INDEX:
        return _REQUIREMENT_INDEX[desc_lower]

    for keywords, test_type in _KEYWORD_MAP:
        for kw in keywords:
            if kw in desc_lower:
                return _REQUIREMENT_INDEX[test_type]

    return TestRequirement(
        test_type="custom",
        description=description,
        required_capabilities=[CapabilityRole.RUN_LOCAL_PROGRAM],
        notes="No built-in requirement matched. Provide explicit requirements.",
    )


# ---------------------------------------------------------------------------
# Suitability assessment
# ---------------------------------------------------------------------------

_SUBSTITUTIONS: list[tuple[CapabilityRole, CapabilityRole, float]] = [
    (CapabilityRole.CAPTURE_LOGIC, CapabilityRole.CAPTURE_WAVEFORM, 0.5),
    (CapabilityRole.CAPTURE_WAVEFORM, CapabilityRole.CAPTURE_LOGIC, 0.4),
    (CapabilityRole.PROTOCOL_MASTER, CapabilityRole.PROTOCOL_CONTROLLER, 0.3),
    (CapabilityRole.PROTOCOL_CONTROLLER, CapabilityRole.PROTOCOL_MASTER, 0.3),
]


def _match_role(
    role: CapabilityRole,
    graph: CapabilityGraph,
) -> CapabilityMatch | None:
    """Try to match a required role against the graph."""
    nodes = graph.by_role(role)
    if nodes:
        best = max(nodes, key=lambda n: n.confidence)
        return CapabilityMatch(
            role=role,
            matched_target=best.target,
            confidence=best.confidence,
        )
    return None


def _find_substitution(
    role: CapabilityRole,
    graph: CapabilityGraph,
) -> Substitution | None:
    for missing, sub, quality in _SUBSTITUTIONS:
        if missing == role:
            nodes = graph.by_role(sub)
            if nodes:
                best = max(nodes, key=lambda n: n.confidence)
                return Substitution(
                    missing_role=role,
                    substitute_role=sub,
                    substitute_target=best.target,
                    quality=quality,
                    explanation=f"{sub.value} on {best.target} partially substitutes for {role.value}",
                )
    return None


def assess_suitability(
    requirement: TestRequirement,
    graph: CapabilityGraph,
) -> SuitabilityReport:
    """
    Match a TestRequirement against a CapabilityGraph and return a
    structured SuitabilityReport.
    """
    matched_req: list[CapabilityMatch] = []
    missing_req: list[CapabilityRole] = []
    substitutions: list[Substitution] = []

    for role in requirement.required_capabilities:
        m = _match_role(role, graph)
        if m:
            matched_req.append(m)
        else:
            sub = _find_substitution(role, graph)
            if sub:
                substitutions.append(sub)
            else:
                missing_req.append(role)

    matched_rec: list[CapabilityMatch] = []
    missing_rec: list[CapabilityRole] = []
    for role in requirement.recommended_capabilities:
        m = _match_role(role, graph)
        if m:
            matched_rec.append(m)
        else:
            missing_rec.append(role)

    can_run = len(missing_req) == 0
    total_req = len(requirement.required_capabilities) or 1
    req_score = len(matched_req) / total_req
    sub_penalty = sum(s.quality * 0.5 for s in substitutions) / total_req if substitutions else 0
    rec_bonus = 0.1 * len(matched_rec) / max(len(requirement.recommended_capabilities), 1) if requirement.recommended_capabilities else 0
    confidence = min(1.0, req_score + sub_penalty + rec_bonus)

    candidate_nets: dict[str, list[str]] = {}
    for m in matched_req + matched_rec:
        key = m.role.value
        candidate_nets.setdefault(key, []).append(m.matched_target)

    parts: list[str] = []
    if can_run:
        parts.append(f"Box can run {requirement.test_type} (confidence {confidence:.2f}).")
    else:
        parts.append(f"Box CANNOT run {requirement.test_type}.")
        parts.append(f"Missing required: {[r.value for r in missing_req]}.")
    if substitutions:
        parts.append(f"Substitutions available: {[s.explanation for s in substitutions]}.")
    if missing_rec:
        parts.append(f"Missing recommended: {[r.value for r in missing_rec]}.")

    return SuitabilityReport(
        test_type=requirement.test_type,
        box_id=graph.box_id,
        can_run=can_run,
        confidence=round(confidence, 3),
        matched_required=matched_req,
        matched_recommended=matched_rec,
        missing_required=missing_req,
        missing_recommended=missing_rec,
        substitutions=substitutions,
        candidate_nets=candidate_nets,
        explanation=" ".join(parts),
    )


def assess_bench_suitability(test_type_or_description: str) -> SuitabilityReport:
    """
    High-level convenience: resolve test type, get the graph, and assess.

    Used directly by the MCP tool ``assess_suitability``.
    """
    from ..server_state import get_capability_graph

    req = infer_requirements(test_type_or_description)
    graph = get_capability_graph()
    return assess_suitability(req, graph)
