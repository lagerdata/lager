# Copyright 2024-2026 Lager Data LLC
# SPDX-License-Identifier: Apache-2.0

"""Heuristic engine schemas -- test requirements and suitability reports."""

from __future__ import annotations

from pydantic import BaseModel, Field

from .capability import CapabilityRole


class TestRequirement(BaseModel):
    """What a test type needs from a bench."""

    test_type: str  # e.g. "qspi_flash_driver", "spi_slave_validation"
    description: str = ""
    required_capabilities: list[CapabilityRole] = Field(default_factory=list)
    recommended_capabilities: list[CapabilityRole] = Field(default_factory=list)
    optional_capabilities: list[CapabilityRole] = Field(default_factory=list)
    required_protocols: list[str] = Field(default_factory=list)
    required_net_types: list[str] = Field(default_factory=list)
    notes: str = ""


class CapabilityMatch(BaseModel):
    """Records how a required capability was matched to a bench element."""

    role: CapabilityRole
    matched_target: str
    confidence: float = 1.0
    notes: str | None = None


class Substitution(BaseModel):
    """Describes a substitution when the ideal capability is missing."""

    missing_role: CapabilityRole
    substitute_role: CapabilityRole
    substitute_target: str
    quality: float = 0.5  # 0.0 = poor, 1.0 = equivalent
    explanation: str = ""


class SuitabilityReport(BaseModel):
    """Result of matching a TestRequirement against a box's CapabilityGraph."""

    test_type: str
    box_id: str = ""
    can_run: bool = False
    confidence: float = 0.0
    matched_required: list[CapabilityMatch] = Field(default_factory=list)
    matched_recommended: list[CapabilityMatch] = Field(default_factory=list)
    missing_required: list[CapabilityRole] = Field(default_factory=list)
    missing_recommended: list[CapabilityRole] = Field(default_factory=list)
    substitutions: list[Substitution] = Field(default_factory=list)
    candidate_nets: dict[str, list[str]] = Field(default_factory=dict)
    explanation: str = ""
