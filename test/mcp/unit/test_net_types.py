# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

"""The one net-type table agrees with ``NetType`` and with every consumer.

Four hand-written tables used to carry this knowledge (the bench loader's
electrical types and roles, the capability graph's role map, the planner's
phase order, the API reference's alias map). They drifted: ``solar`` had
roles but no API reference and no phase. Now there is one table, and these
tests pin it to the enum and to each consumer so a new net type cannot land
in one place and not the others.
"""

import pytest

from lager.mcp.data.api_reference import API_REFERENCE, get_reference_for_type
from lager.mcp.engine import capability_graph
from lager.mcp.engine.bench_loader import _net_from_raw, load_from_dicts
from lager.mcp.engine.net_types import (
    NET_TYPES,
    PHASE_LABELS,
    PHASE_SETUP_POWER,
    ROLES_WITHOUT_NET_TYPE,
    info_for,
    reference_key,
)
from lager.nets.constants import NetType


class TestTableMatchesTheEnum:
    def test_every_nettype_member_has_a_row(self):
        covered = {row.net_type for row in NET_TYPES.values()}
        missing = {member.name for member in NetType} - covered
        assert not missing, f"NetType members with no net_types row: {sorted(missing)}"

    def test_every_row_names_a_real_member(self):
        names = {member.name for member in NetType}
        bad = {role: row.net_type for role, row in NET_TYPES.items() if row.net_type not in names}
        assert not bad, f"rows naming a NetType that does not exist: {bad}"

    def test_every_role_resolves_through_from_role_to_its_row(self):
        for role, row in NET_TYPES.items():
            if role in ROLES_WITHOUT_NET_TYPE:
                continue
            assert NetType.from_role(role) is NetType[row.net_type], role

    def test_the_no_enum_allowlist_stays_honest(self):
        """A role listed as having no NetType must really have none."""
        for role in ROLES_WITHOUT_NET_TYPE:
            assert role in NET_TYPES
            with pytest.raises(KeyError):
                NetType.from_role(role)

    def test_every_phase_has_a_label(self):
        assert {row.phase for row in NET_TYPES.values()} <= set(PHASE_LABELS)


class TestReferenceColumn:
    def test_every_reference_names_an_api_reference_entry(self):
        bad = {
            role: row.reference for role, row in NET_TYPES.items()
            if row.reference is not None and row.reference not in API_REFERENCE
        }
        assert not bad

    def test_rows_without_a_reference_are_the_documented_exceptions(self):
        # The same set test_api_reference.py justifies one by one.
        assert {row.net_type for row in NET_TYPES.values() if row.reference is None} == {
            "Waveform", "Rotation", "Actuate",
        }

    @pytest.mark.parametrize("spelling, expected", [
        ("power-supply", "PowerSupply"),
        ("supply", "PowerSupply"),
        ("power-supply-2q", "PowerSupply"),
        ("solar", "PowerSupply"),
        ("batt", "Battery"),
        ("SCOPE", "Analog"),
        ("mikrotik", "Router"),
        ("rotation", None),
        ("not-a-role", None),
    ])
    def test_reference_key(self, spelling, expected):
        assert reference_key(spelling) == expected

    def test_an_enum_name_is_not_aliased(self):
        """``PowerSupply2Q`` has no entry; the enum name must not be answered
        with the single-quadrant reference just because the role is."""
        assert reference_key("PowerSupply2Q") is None
        assert get_reference_for_type("PowerSupply2Q") is None
        assert get_reference_for_type("power-supply-2q") is API_REFERENCE["PowerSupply"]

    def test_solar_now_has_a_reference(self):
        """The drift this table exists to end: solar nets had roles but no
        API reference, so plan_firmware_test handed back no methods for them."""
        assert get_reference_for_type("solar") is API_REFERENCE["PowerSupply"]


class TestConsumersReadTheTable:
    @pytest.mark.parametrize("role", sorted(NET_TYPES))
    def test_bench_loader_uses_the_row(self, role):
        row = NET_TYPES[role]
        net = _net_from_raw({"name": "n", "role": role})
        assert net.electrical_type == row.electrical_type
        assert net.directionality == row.directionality
        assert net.controllable is row.controllable
        assert net.roles == row.role_names

    @pytest.mark.parametrize("role", sorted(NET_TYPES))
    def test_capability_graph_uses_the_row(self, role):
        row = NET_TYPES[role]
        bench = load_from_dicts(raw_nets=[{"name": "n", "role": role}])
        nodes = capability_graph.build_capability_graph(bench).by_target("n")
        assert [(n.role, n.confidence) for n in nodes] == list(row.capabilities)

    @pytest.mark.parametrize("role", sorted(NET_TYPES))
    def test_planner_phase_uses_the_row(self, role):
        from lager.mcp.tools.authoring import _infer_phase

        row = NET_TYPES[role]
        net = _net_from_raw({"name": "n", "role": role})
        # A net whose roles include source_power is promoted to setup_power
        # whatever its type; every other net takes the table's phase.
        expected = PHASE_SETUP_POWER if "source_power" in net.roles else row.phase
        assert _infer_phase(net) == expected

    def test_unknown_role_degrades_the_same_everywhere(self):
        assert info_for("nonexistent") is None
        net = _net_from_raw({"name": "x", "role": "nonexistent"})
        assert net.electrical_type == "unknown"
        assert net.roles == []
        bench = load_from_dicts(raw_nets=[{"name": "x", "role": "nonexistent"}])
        assert capability_graph.build_capability_graph(bench).by_target("x") == []
