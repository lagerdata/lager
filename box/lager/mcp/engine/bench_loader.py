# Copyright 2024-2026 Lager Data LLC
# SPDX-License-Identifier: Apache-2.0

"""
Load a BenchDefinition from a Lager box.

Two data sources are combined:

1. /etc/lager/bench.json -- static bench metadata authored once per box
   (DUT slots, aliases, safety constraints, interface groupings)
2. /etc/lager/saved_nets.json -- dynamic net list maintained by
   ``lager nets add`` / ``lager nets add-all``
3. Live instrument and hello data from box HTTP endpoints

The loader can operate in two modes:

* **remote** -- fetches data over HTTP from a running box (used by the
  MCP server at runtime).
* **local** -- reads JSON files from disk (used in tests or when running
  on-box).
"""

from __future__ import annotations

import json
import logging
from typing import Any

import requests

from ..schemas.bench import (
    BenchDefinition,
    CalibrationStatus,
    DUTSlot,
    InstrumentDescriptor,
    InstrumentHealth,
    RoutingEntry,
    VoltageRange,
)
from ..schemas.net import InterfaceDescriptor, NetDescriptor, SafetyLimits
from ..schemas.safety_types import SafetyConstraints

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Net-type → electrical-type mapping
# ---------------------------------------------------------------------------

_ELECTRICAL_TYPE_MAP: dict[str, str] = {
    "power-supply": "power",
    "power-supply-2q": "power",
    "battery": "power",
    "eload": "power",
    "solar": "power",
    "analog": "analog",
    "adc": "analog",
    "dac": "analog",
    "thermocouple": "analog",
    "watt-meter": "analog",
    "energy-analyzer": "analog",
    "gpio": "digital",
    "logic": "digital",
    "spi": "protocol",
    "i2c": "protocol",
    "uart": "protocol",
    "debug": "digital",
    "usb": "digital",
    "wifi": "protocol",
    "webcam": "other",
    "arm": "other",
    "rotation": "other",
    "actuate": "other",
    "scope": "analog",
    "waveform": "analog",
    "mikrotik": "protocol",
    "router": "protocol",
}

# Roles each net type supports -- used by capability_graph.py as well
NET_TYPE_ROLES: dict[str, list[str]] = {
    "power-supply": [
        "source_power", "drive", "measure", "sweep_voltage",
    ],
    "power-supply-2q": [
        "source_power", "sink_power", "drive", "measure", "sweep_voltage",
    ],
    "battery": [
        "source_power", "drive", "measure", "sweep_voltage",
    ],
    "eload": [
        "sink_power", "measure",
    ],
    "solar": [
        "source_power", "drive", "measure",
    ],
    "analog": [
        "observe", "measure", "capture_waveform",
    ],
    "scope": [
        "observe", "measure", "capture_waveform",
    ],
    "logic": [
        "observe", "capture_logic",
    ],
    "adc": [
        "observe", "measure",
    ],
    "dac": [
        "drive", "sweep_analog", "waveform_gen",
    ],
    "gpio": [
        "drive", "observe", "control_state",
    ],
    "spi": [
        "protocol_master", "capture_protocol",
    ],
    "i2c": [
        "protocol_controller", "capture_protocol",
    ],
    "uart": [
        "observe", "protocol_master",
    ],
    "debug": [
        "flash_firmware", "control_state",
    ],
    "thermocouple": [
        "observe", "measure",
    ],
    "watt-meter": [
        "observe", "measure",
    ],
    "energy-analyzer": [
        "observe", "measure",
    ],
    "usb": [
        "control_state",
    ],
    "wifi": [
        "observe",
    ],
    "waveform": [
        "observe", "capture_waveform",
    ],
    "webcam": [
        "observe",
    ],
    "arm": [],
    "rotation": [],
    "actuate": [
        "drive", "control_state",
    ],
    "mikrotik": ["observe"],
    "router": ["observe"],
}


def _directionality_for(net_type: str) -> str:
    if net_type in ("adc", "thermocouple", "watt-meter", "energy-analyzer", "analog", "scope", "logic"):
        return "input"
    if net_type in ("dac",):
        return "output"
    return "bidirectional"


# ---------------------------------------------------------------------------
# Build NetDescriptor from a raw saved_nets entry
# ---------------------------------------------------------------------------

def _net_from_raw(raw: dict[str, Any]) -> NetDescriptor:
    """Convert a single entry from saved_nets.json to a NetDescriptor."""
    role = raw.get("role", "")
    instrument = raw.get("instrument", "")
    address = raw.get("address", "")
    channel = str(raw.get("channel", raw.get("pin", "")))

    return NetDescriptor(
        name=raw.get("name") or "",
        aliases=raw.get("aliases") or [],
        net_type=role,
        electrical_type=_ELECTRICAL_TYPE_MAP.get(role, "unknown"),
        voltage_domain=None,
        directionality=_directionality_for(role),
        controllable=role not in ("adc", "thermocouple", "watt-meter", "energy-analyzer", "logic"),
        observable=True,
        roles=list(NET_TYPE_ROLES.get(role, [])),
        safety_limits=None,
        timing_constraints=None,
        instrument=instrument,
        channel=channel,
        params=raw.get("params") or {},
        description=raw.get("description") or "",
        dut_connection=raw.get("dut_connection") or "",
        test_hints=raw.get("test_hints") or [],
        tags=raw.get("tags") or [],
    )


# ---------------------------------------------------------------------------
# Infer interfaces from nets
# ---------------------------------------------------------------------------

_PROTOCOL_NET_TYPES = {"spi", "i2c", "uart"}


def _infer_interfaces(nets: list[NetDescriptor]) -> list[InterfaceDescriptor]:
    """Group protocol-typed nets into InterfaceDescriptors."""
    interfaces: list[InterfaceDescriptor] = []
    for net in nets:
        if net.net_type in _PROTOCOL_NET_TYPES:
            interfaces.append(
                InterfaceDescriptor(
                    name=net.name,
                    protocol=net.net_type,
                    nets=[net.name],
                    roles=list(net.roles),
                )
            )
    return interfaces


# ---------------------------------------------------------------------------
# Remote loader (HTTP to box)
# ---------------------------------------------------------------------------

def load_from_box(
    box_ip: str,
    *,
    timeout: float = 10.0,
    bench_json_override: dict[str, Any] | None = None,
) -> BenchDefinition:
    """
    Build a BenchDefinition by querying a live Lager box over HTTP.

    Fetches /hello, /nets, /instruments, and optionally /bench.json.
    """
    base = f"http://{box_ip}:5000"
    session = requests.Session()

    # -- hello ---------------------------------------------------------------
    hello_data: dict[str, Any] = {}
    try:
        resp = session.get(f"{base}/hello", timeout=timeout)
        if resp.ok:
            hello_data = resp.json() if resp.headers.get("content-type", "").startswith("application/json") else {}
    except Exception as exc:
        logger.warning("Could not reach /hello on %s: %s", box_ip, exc)

    # -- nets ----------------------------------------------------------------
    raw_nets: list[dict[str, Any]] = []
    try:
        resp = session.get(f"{base}/nets", timeout=timeout)
        if resp.ok:
            body = resp.json()
            if isinstance(body, list):
                raw_nets = body
            elif isinstance(body, dict) and "nets" in body:
                raw_nets = body["nets"]
    except Exception as exc:
        logger.warning("Could not fetch /nets on %s: %s", box_ip, exc)

    # -- instruments ---------------------------------------------------------
    raw_instruments: list[dict[str, Any]] = []
    try:
        resp = session.get(f"{base}/instruments", timeout=timeout)
        if resp.ok:
            body = resp.json()
            if isinstance(body, list):
                raw_instruments = body
            elif isinstance(body, dict) and "instruments" in body:
                raw_instruments = body["instruments"]
    except Exception as exc:
        logger.warning("Could not fetch /instruments on %s: %s", box_ip, exc)

    # -- bench.json (optional static config) ---------------------------------
    bench_cfg = bench_json_override or {}
    if not bench_cfg:
        try:
            resp = session.get(f"{base}/bench.json", timeout=timeout)
            if resp.ok:
                bench_cfg = resp.json()
        except Exception:
            pass  # bench.json is optional

    return _assemble(
        hello_data=hello_data,
        raw_nets=raw_nets,
        raw_instruments=raw_instruments,
        bench_cfg=bench_cfg,
        box_ip=box_ip,
    )


# ---------------------------------------------------------------------------
# Local loader (files on disk)
# ---------------------------------------------------------------------------

def load_from_files(
    *,
    saved_nets_path: str = "/etc/lager/saved_nets.json",
    bench_json_path: str = "/etc/lager/bench.json",
    box_id_path: str = "/etc/lager/box_id",
) -> BenchDefinition:
    """Build a BenchDefinition from on-disk JSON files (used on-box or in tests)."""

    raw_nets = _read_json(saved_nets_path, default=[])
    bench_cfg = _read_json(bench_json_path, default={})

    box_id = ""
    try:
        with open(box_id_path, "r") as fh:
            box_id = fh.read().strip()
    except FileNotFoundError:
        pass

    return _assemble(
        hello_data={"box_id": box_id},
        raw_nets=raw_nets if isinstance(raw_nets, list) else [],
        raw_instruments=[],
        bench_cfg=bench_cfg if isinstance(bench_cfg, dict) else {},
        box_ip="",
    )


def load_from_dicts(
    *,
    raw_nets: list[dict[str, Any]] | None = None,
    bench_cfg: dict[str, Any] | None = None,
    hello_data: dict[str, Any] | None = None,
    raw_instruments: list[dict[str, Any]] | None = None,
) -> BenchDefinition:
    """Build a BenchDefinition from in-memory dicts (primarily for tests)."""
    return _assemble(
        hello_data=hello_data or {},
        raw_nets=raw_nets or [],
        raw_instruments=raw_instruments or [],
        bench_cfg=bench_cfg or {},
        box_ip="",
    )


# ---------------------------------------------------------------------------
# Internal assembly
# ---------------------------------------------------------------------------

def _assemble(
    *,
    hello_data: dict[str, Any],
    raw_nets: list[dict[str, Any]],
    raw_instruments: list[dict[str, Any]],
    bench_cfg: dict[str, Any],
    box_ip: str,
) -> BenchDefinition:
    box_id = (
        bench_cfg.get("box_id")
        or hello_data.get("box_id")
        or hello_data.get("id")
        or box_ip
        or ""
    )
    hostname = bench_cfg.get("hostname", hello_data.get("hostname", ""))
    version = bench_cfg.get("version", hello_data.get("version", ""))

    # Nets
    nets = [_net_from_raw(rn) for rn in raw_nets]

    # Merge bench_cfg net overrides (aliases, safety limits, voltage domains).
    # Each override is applied independently so one malformed entry can't
    # corrupt the rest of the bench.
    net_overrides: dict[str, dict[str, Any]] = {
        o["name"]: o
        for o in (bench_cfg.get("net_overrides") or [])
        if isinstance(o, dict) and "name" in o
    }
    for nd in nets:
        ovr = net_overrides.get(nd.name)
        if not ovr:
            continue
        if "aliases" in ovr:
            nd.aliases = ovr["aliases"]
        if "voltage_domain" in ovr and isinstance(ovr["voltage_domain"], dict):
            try:
                nd.voltage_domain = VoltageRange(**ovr["voltage_domain"])
            except TypeError as e:
                logger.warning("net %s: bad voltage_domain override (%s)", nd.name, e)
        if "safety_limits" in ovr and isinstance(ovr["safety_limits"], dict):
            try:
                nd.safety_limits = SafetyLimits(**ovr["safety_limits"])
            except TypeError as e:
                logger.warning("net %s: bad safety_limits override (%s)", nd.name, e)
        if "description" in ovr:
            nd.description = ovr["description"]
        if "dut_connection" in ovr:
            nd.dut_connection = ovr["dut_connection"]
        if "test_hints" in ovr:
            nd.test_hints = ovr["test_hints"]
        if "tags" in ovr:
            nd.tags = ovr["tags"]

    # Instruments
    instruments = [
        InstrumentDescriptor(
            name=ri.get("name", ri.get("instrument", "")),
            instrument_type=ri.get("type", ri.get("instrument", "")),
            connection=ri.get("address", ri.get("connection", "")),
            channels=ri.get("channels") or [],
        )
        for ri in raw_instruments
        if isinstance(ri, dict)
    ]

    # DUT slots — skip individual malformed entries instead of failing the
    # whole bench load.
    dut_slots: list[DUTSlot] = []
    for ds in (bench_cfg.get("dut_slots") or []):
        if not isinstance(ds, dict):
            logger.warning("dut_slots: skipping non-dict entry %r", ds)
            continue
        try:
            dut_slots.append(DUTSlot(**ds))
        except TypeError as e:
            logger.warning("dut_slots: skipping malformed entry %r (%s)", ds, e)

    # Interfaces — same per-entry tolerance.
    static_ifaces: list[InterfaceDescriptor] = []
    for iface in (bench_cfg.get("interfaces") or []):
        if not isinstance(iface, dict):
            logger.warning("interfaces: skipping non-dict entry %r", iface)
            continue
        try:
            static_ifaces.append(InterfaceDescriptor(**iface))
        except TypeError as e:
            logger.warning("interfaces: skipping malformed entry %r (%s)", iface, e)
    inferred_ifaces = _infer_interfaces(nets)
    seen_names = {i.name for i in static_ifaces}
    interfaces = static_ifaces + [i for i in inferred_ifaces if i.name not in seen_names]

    # Safety constraints — bench-level, fall back to None on bad input.
    constraints = None
    if "constraints" in bench_cfg and isinstance(bench_cfg["constraints"], dict):
        try:
            constraints = SafetyConstraints(**bench_cfg["constraints"])
        except TypeError as e:
            logger.warning("bench.json: bad constraints block (%s); ignoring", e)

    # Calibration — bench-level, fall back to default empty status.
    cal = CalibrationStatus()
    if "calibration" in bench_cfg and isinstance(bench_cfg["calibration"], dict):
        try:
            cal = CalibrationStatus(**bench_cfg["calibration"])
        except TypeError as e:
            logger.warning("bench.json: bad calibration block (%s); ignoring", e)

    return BenchDefinition(
        box_id=box_id,
        hostname=hostname,
        version=version,
        dut_slots=dut_slots,
        instruments=instruments,
        nets=nets,
        interfaces=interfaces,
        routing=[],
        constraints=constraints,
        calibration=cal,
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _read_json(path: str, default: Any = None) -> Any:
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except (FileNotFoundError, json.JSONDecodeError):
        return default if default is not None else {}
