# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

"""The one table of what each net type means to the MCP engine.

Keyed by the raw ``role`` string a saved net carries (``power-supply``,
``spi``, ``scope``...), which is also what ``NetDescriptor.net_type`` holds
and what ``lager.nets.constants.NetType.from_role`` accepts. Every consumer
in ``lager.mcp`` reads from here:

- ``engine.bench_loader``: electrical type, directionality, controllability
  and the role list on each ``NetDescriptor``;
- ``engine.capability_graph``: the ``CapabilityNode`` roles and confidences;
- ``tools.authoring``: the test-plan phase a net belongs to;
- ``data.api_reference``: which ``API_REFERENCE`` entry documents the type.

It replaces four hand-written tables that had drifted apart (``solar`` had
an electrical type and roles but no API reference and no plan phase).
``test/mcp/unit/test_net_types.py`` checks that every ``NetType`` member has
a row here and that every row's role resolves through ``NetType.from_role``,
so a new net type cannot land in one place and not the other.

This module imports nothing from the MCP SDK, so the box's :9000 server can
use it without pulling the SDK into that process.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..schemas.capability import CapabilityRole

# Test-plan phases, in the order ``plan_firmware_test`` emits them.
PHASE_SETUP_POWER = 0
PHASE_FLASH_AND_BOOT = 1
PHASE_PROTOCOL_TESTS = 2
PHASE_IO_TESTS = 3
PHASE_MEASUREMENT = 4
PHASE_PERIPHERALS = 5

PHASE_LABELS: dict[int, str] = {
    PHASE_SETUP_POWER: "setup_power",
    PHASE_FLASH_AND_BOOT: "flash_and_boot",
    PHASE_PROTOCOL_TESTS: "protocol_tests",
    PHASE_IO_TESTS: "io_tests",
    PHASE_MEASUREMENT: "measurement",
    PHASE_PERIPHERALS: "peripherals",
}


@dataclass(frozen=True)
class NetTypeInfo:
    """What the engine knows about one saved-net role."""

    #: The ``NetType`` member the role resolves to (``PowerSupply``, ``SPI``).
    net_type: str
    #: "power", "analog", "digital", "protocol" or "other".
    electrical_type: str
    #: ``(role, confidence)`` pairs the capability graph derives for the net.
    capabilities: tuple[tuple[CapabilityRole, float], ...] = ()
    #: "input", "output" or "bidirectional".
    directionality: str = "bidirectional"
    #: False for nets a test can only read from.
    controllable: bool = True
    #: Test-plan phase; see ``PHASE_*``.
    phase: int = PHASE_PERIPHERALS
    #: The ``API_REFERENCE`` key that documents this role, or None when no
    #: entry exists. Not always the ``NetType`` name: a two-quadrant supply is
    #: driven through the ``PowerSupply`` API.
    reference: str | None = None
    #: Extra spellings ``get_reference_for_type`` accepts for this role.
    aliases: tuple[str, ...] = ()

    @property
    def role_names(self) -> list[str]:
        """The capability roles as their string values (``NetDescriptor.roles``)."""
        return [role.value for role, _ in self.capabilities]


_R = CapabilityRole

NET_TYPES: dict[str, NetTypeInfo] = {
    "power-supply": NetTypeInfo(
        net_type="PowerSupply", electrical_type="power",
        capabilities=((_R.SOURCE_POWER, 1.0), (_R.DRIVE, 1.0),
                      (_R.MEASURE, 0.9), (_R.SWEEP_VOLTAGE, 0.9)),
        phase=PHASE_SETUP_POWER, reference="PowerSupply", aliases=("supply",),
    ),
    "power-supply-2q": NetTypeInfo(
        net_type="PowerSupply2Q", electrical_type="power",
        capabilities=((_R.SOURCE_POWER, 1.0), (_R.SINK_POWER, 1.0), (_R.DRIVE, 1.0),
                      (_R.MEASURE, 0.9), (_R.SWEEP_VOLTAGE, 0.9)),
        phase=PHASE_SETUP_POWER, reference="PowerSupply",
    ),
    "battery": NetTypeInfo(
        net_type="Battery", electrical_type="power",
        capabilities=((_R.SOURCE_POWER, 1.0), (_R.DRIVE, 1.0),
                      (_R.MEASURE, 0.9), (_R.SWEEP_VOLTAGE, 0.9)),
        phase=PHASE_SETUP_POWER, reference="Battery", aliases=("batt",),
    ),
    "eload": NetTypeInfo(
        net_type="ELoad", electrical_type="power",
        capabilities=((_R.SINK_POWER, 1.0), (_R.MEASURE, 0.9)),
        phase=PHASE_SETUP_POWER, reference="ELoad",
    ),
    "solar": NetTypeInfo(
        # A solar-array simulator is a supply driven through the PowerSupply
        # API. Not a NetType member of its own; see ROLES_WITHOUT_NET_TYPE.
        net_type="PowerSupply", electrical_type="power",
        capabilities=((_R.SOURCE_POWER, 1.0), (_R.DRIVE, 1.0), (_R.MEASURE, 0.9)),
        phase=PHASE_SETUP_POWER, reference="PowerSupply",
    ),
    "analog": NetTypeInfo(
        net_type="Analog", electrical_type="analog",
        capabilities=((_R.OBSERVE, 1.0), (_R.MEASURE, 1.0), (_R.CAPTURE_WAVEFORM, 1.0)),
        directionality="input", phase=PHASE_MEASUREMENT, reference="Analog",
    ),
    "scope": NetTypeInfo(
        net_type="Analog", electrical_type="analog",
        capabilities=((_R.OBSERVE, 1.0), (_R.MEASURE, 1.0), (_R.CAPTURE_WAVEFORM, 1.0)),
        directionality="input", phase=PHASE_MEASUREMENT, reference="Analog",
    ),
    "logic": NetTypeInfo(
        net_type="Logic", electrical_type="digital",
        capabilities=((_R.OBSERVE, 1.0), (_R.CAPTURE_LOGIC, 1.0)),
        directionality="input", controllable=False, phase=PHASE_MEASUREMENT,
        reference="Logic",
    ),
    "waveform": NetTypeInfo(
        net_type="Waveform", electrical_type="analog",
        capabilities=((_R.OBSERVE, 1.0), (_R.CAPTURE_WAVEFORM, 1.0)),
        phase=PHASE_MEASUREMENT,
    ),
    "adc": NetTypeInfo(
        net_type="ADC", electrical_type="analog",
        capabilities=((_R.OBSERVE, 1.0), (_R.MEASURE, 1.0)),
        directionality="input", controllable=False, phase=PHASE_IO_TESTS,
        reference="ADC",
    ),
    "dac": NetTypeInfo(
        net_type="DAC", electrical_type="analog",
        capabilities=((_R.DRIVE, 1.0), (_R.SWEEP_ANALOG, 0.9), (_R.WAVEFORM_GEN, 0.7)),
        directionality="output", phase=PHASE_IO_TESTS, reference="DAC",
    ),
    "gpio": NetTypeInfo(
        net_type="GPIO", electrical_type="digital",
        capabilities=((_R.DRIVE, 1.0), (_R.OBSERVE, 1.0), (_R.CONTROL_STATE, 1.0)),
        phase=PHASE_IO_TESTS, reference="GPIO",
    ),
    "spi": NetTypeInfo(
        net_type="SPI", electrical_type="protocol",
        capabilities=((_R.PROTOCOL_MASTER, 1.0), (_R.CAPTURE_PROTOCOL, 0.8)),
        phase=PHASE_PROTOCOL_TESTS, reference="SPI",
    ),
    "i2c": NetTypeInfo(
        net_type="I2C", electrical_type="protocol",
        capabilities=((_R.PROTOCOL_CONTROLLER, 1.0), (_R.CAPTURE_PROTOCOL, 0.8)),
        phase=PHASE_PROTOCOL_TESTS, reference="I2C",
    ),
    "uart": NetTypeInfo(
        net_type="UART", electrical_type="protocol",
        capabilities=((_R.OBSERVE, 1.0), (_R.PROTOCOL_MASTER, 0.9)),
        phase=PHASE_PROTOCOL_TESTS, reference="UART",
    ),
    "debug": NetTypeInfo(
        net_type="Debug", electrical_type="digital",
        capabilities=((_R.FLASH_FIRMWARE, 1.0), (_R.CONTROL_STATE, 1.0)),
        phase=PHASE_FLASH_AND_BOOT, reference="Debug",
    ),
    "thermocouple": NetTypeInfo(
        net_type="Thermocouple", electrical_type="analog",
        capabilities=((_R.OBSERVE, 1.0), (_R.MEASURE, 1.0)),
        directionality="input", controllable=False, phase=PHASE_MEASUREMENT,
        reference="Thermocouple",
    ),
    "watt-meter": NetTypeInfo(
        net_type="WattMeter", electrical_type="analog",
        capabilities=((_R.OBSERVE, 1.0), (_R.MEASURE, 1.0)),
        directionality="input", controllable=False, phase=PHASE_MEASUREMENT,
        reference="WattMeter",
    ),
    "energy-analyzer": NetTypeInfo(
        net_type="EnergyAnalyzer", electrical_type="analog",
        capabilities=((_R.OBSERVE, 1.0), (_R.MEASURE, 1.0)),
        directionality="input", controllable=False, phase=PHASE_MEASUREMENT,
        reference="EnergyAnalyzer",
    ),
    "usb": NetTypeInfo(
        net_type="Usb", electrical_type="digital",
        capabilities=((_R.CONTROL_STATE, 0.9),),
        phase=PHASE_PERIPHERALS, reference="Usb",
    ),
    "wifi": NetTypeInfo(
        net_type="Wifi", electrical_type="protocol",
        capabilities=((_R.OBSERVE, 0.7),),
        phase=PHASE_PERIPHERALS, reference="Wifi",
    ),
    "webcam": NetTypeInfo(
        net_type="Webcam", electrical_type="other",
        capabilities=((_R.OBSERVE, 0.5),),
        phase=PHASE_PERIPHERALS, reference="Webcam",
    ),
    "arm": NetTypeInfo(
        net_type="Arm", electrical_type="other",
        phase=PHASE_PERIPHERALS, reference="Arm",
    ),
    "rotation": NetTypeInfo(
        net_type="Rotation", electrical_type="other",
        phase=PHASE_PERIPHERALS,
    ),
    "actuate": NetTypeInfo(
        net_type="Actuate", electrical_type="other",
        capabilities=((_R.DRIVE, 0.8), (_R.CONTROL_STATE, 0.8)),
        phase=PHASE_PERIPHERALS,
    ),
    "router": NetTypeInfo(
        net_type="Router", electrical_type="protocol",
        capabilities=((_R.OBSERVE, 0.7),),
        phase=PHASE_PERIPHERALS, reference="Router",
    ),
    "mikrotik": NetTypeInfo(
        net_type="Router", electrical_type="protocol",
        capabilities=((_R.OBSERVE, 0.7),),
        phase=PHASE_PERIPHERALS, reference="Router",
    ),
}

#: Roles the engine models that ``NetType.from_role`` does not accept. A net
#: saved with one of these is driven through the API named in its row.
ROLES_WITHOUT_NET_TYPE: frozenset[str] = frozenset({"solar"})


def info_for(role: str) -> NetTypeInfo | None:
    """The row for a saved-net role, or None for a role the engine does not know."""
    return NET_TYPES.get(role)


def reference_key(role_or_alias: str) -> str | None:
    """The ``API_REFERENCE`` key for a saved-net role or one of its aliases.

    ``"power-supply"`` and ``"supply"`` both give ``"PowerSupply"``. None for
    a role with no reference and for anything that is not a role, including
    a ``NetType`` name: ``data.api_reference`` looks those up directly, so
    that ``PowerSupply2Q`` (no entry of its own) is not answered with the
    single-quadrant reference under the enum's name.
    """
    lowered = role_or_alias.lower()
    row = NET_TYPES.get(lowered)
    if row is None:
        row = next((r for r in NET_TYPES.values() if lowered in r.aliases), None)
    return row.reference if row is not None else None


__all__ = [
    "NET_TYPES",
    "NetTypeInfo",
    "PHASE_FLASH_AND_BOOT",
    "PHASE_IO_TESTS",
    "PHASE_LABELS",
    "PHASE_MEASUREMENT",
    "PHASE_PERIPHERALS",
    "PHASE_PROTOCOL_TESTS",
    "PHASE_SETUP_POWER",
    "ROLES_WITHOUT_NET_TYPE",
    "info_for",
    "reference_key",
]
