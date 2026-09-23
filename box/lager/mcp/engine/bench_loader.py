# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

"""
Load a BenchDefinition from the files on a Lager box.

Two files are combined:

1. /etc/lager/bench.json -- static bench metadata authored once per box
   (DUT slots, aliases, safety constraints, interface groupings, and
   ``net_overrides`` that shadow a saved net's own metadata)
2. /etc/lager/saved_nets.json -- dynamic net list maintained by
   ``lager nets add`` / ``lager nets add-all`` and ``lager nets describe``

Box identity (id, version, hostname) is seeded from its own files so an
unauthored box still reports it.

Instruments are NOT read here. They come from a live USB scan, which
``server_state.get_bench`` attaches from ``engine.instruments`` behind a
short cache, so this loader stays a pure function of the files and the
scan rate stays bounded.
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any

from ..schemas.bench import (
    BenchDefinition,
    CalibrationStatus,
    DocRef,
    DUTContext,
    InstrumentDescriptor,
    SubSystem,
    VoltageRange,
)
from ..schemas.net import InterfaceDescriptor, NetDescriptor, SafetyLimits
from ..schemas.safety_types import SafetyConstraints
from .instruments import instrument_from_record
from .net_types import info_for

logger = logging.getLogger(__name__)

#: The per-net fields a ``bench.json`` ``net_overrides`` entry may shadow,
#: and the keys ``BenchDefinition.metadata_sources`` reports on.
OVERRIDABLE_NET_FIELDS: tuple[str, ...] = (
    "purpose",
    "notes",
    "tags",
    "dut_connection",
    "test_hints",
    "aliases",
    "voltage_domain",
    "safety_limits",
)

#: The subset a ``PUT /nets/<name>/metadata`` write can set. The rest come
#: from ``lager nets add`` or bench.json only.
USER_METADATA_FIELDS: tuple[str, ...] = (
    "purpose",
    "notes",
    "tags",
    "dut_connection",
    "test_hints",
)


# ---------------------------------------------------------------------------
# Build NetDescriptor from a raw saved_nets entry
# ---------------------------------------------------------------------------

def _net_from_raw(raw: dict[str, Any]) -> NetDescriptor:
    """Convert a single entry from saved_nets.json to a NetDescriptor."""
    role = raw.get("role", "")
    instrument = raw.get("instrument", "")
    channel = str(raw.get("channel", raw.get("pin", "")))
    info = info_for(role)

    return NetDescriptor(
        name=raw.get("name") or "",
        aliases=raw.get("aliases") or [],
        net_type=role,
        electrical_type=info.electrical_type if info else "unknown",
        voltage_domain=None,
        directionality=info.directionality if info else "bidirectional",
        controllable=info.controllable if info else True,
        observable=True,
        roles=info.role_names if info else [],
        safety_limits=None,
        timing_constraints=None,
        instrument=instrument,
        channel=channel,
        params=raw.get("params") or {},
        purpose=raw.get("purpose") or "",
        notes=raw.get("notes") or "",
        tags=raw.get("tags") or [],
        dut_connection=raw.get("dut_connection") or "",
        test_hints=[str(h) for h in (raw.get("test_hints") or [])],
    )


# ---------------------------------------------------------------------------
# Build DUTContext / SubSystem / DocRef from bench.json
# ---------------------------------------------------------------------------

#: Keys _doc_ref_from_raw maps onto DocRef fields itself, aliases included.
#: Every other key in a raw reference is carried over as an extra field.
_DOC_REF_RAW_KEYS = frozenset({
    "title", "name", "kind", "url", "repo_path", "path",
    "external_id", "external_url", "pages", "notes",
})


def _doc_ref_from_raw(raw: dict[str, Any]) -> DocRef | None:
    """Build a DocRef from a raw dict. Returns None when malformed.

    A reference with no locator (no ``url``, ``repo_path``, ``external_id``
    or ``external_url``) is malformed: it names a document without saying
    where it is. Keys this loader does not map are passed through as extra
    fields, so a field added by a newer writer is not dropped on the way to
    the agent.
    """
    if not isinstance(raw, dict):
        return None
    title = raw.get("title") or raw.get("name") or ""
    if not title:
        return None
    kind = raw.get("kind") or "other"
    extras = {
        key: value for key, value in raw.items()
        if isinstance(key, str)
        and key not in _DOC_REF_RAW_KEYS
        and not key.startswith(("_", "model_"))
    }
    try:
        return DocRef(
            title=str(title),
            kind=kind,
            url=raw.get("url"),
            repo_path=raw.get("repo_path") or raw.get("path"),
            external_id=raw.get("external_id"),
            external_url=raw.get("external_url"),
            pages=raw.get("pages"),
            notes=raw.get("notes"),
            **extras,
        )
    except (TypeError, ValueError) as e:
        logger.warning("doc_ref: skipping malformed entry %r (%s)", raw, e)
        return None


def _doc_refs_from_raw(raw_list: Any) -> list[DocRef]:
    if not isinstance(raw_list, list):
        return []
    out: list[DocRef] = []
    for entry in raw_list:
        ref = _doc_ref_from_raw(entry) if isinstance(entry, dict) else None
        if ref is not None:
            out.append(ref)
    return out


def _subsystem_from_raw(raw: dict[str, Any]) -> SubSystem | None:
    if not isinstance(raw, dict):
        return None
    name = raw.get("name") or ""
    if not name:
        return None
    return SubSystem(
        name=str(name),
        summary=raw.get("summary") or "",
        nets=list(raw.get("nets") or []),
        doc_refs=_doc_refs_from_raw(raw.get("doc_refs")),
    )


def _dut_context_from_raw(raw: dict[str, Any]) -> DUTContext:
    """Build a DUTContext from a bench.json ``dut_slots`` entry.

    Accepts both the legacy minimal shape (just ``name`` / ``active`` /
    ``board_profile`` / ``firmware``) and the new richer shape with
    ``purpose``, ``summary``, ``mcu``, ``key_peripherals``, schematic
    refs and subsystems.
    """
    name = raw.get("name") or ""
    if not name:
        raise ValueError("dut slot entry missing 'name'")

    subsystems: list[SubSystem] = []
    for sub_raw in (raw.get("subsystems") or []):
        sub = _subsystem_from_raw(sub_raw)
        if sub is not None:
            subsystems.append(sub)

    return DUTContext(
        name=str(name),
        active=bool(raw.get("active", True)),
        board_profile=raw.get("board_profile"),
        firmware=raw.get("firmware"),
        purpose=raw.get("purpose") or "",
        summary=raw.get("summary") or raw.get("description") or "",
        mcu=raw.get("mcu"),
        key_peripherals=list(raw.get("key_peripherals") or []),
        schematic_refs=_doc_refs_from_raw(raw.get("schematic_refs")),
        datasheet_refs=_doc_refs_from_raw(raw.get("datasheet_refs")),
        firmware_refs=_doc_refs_from_raw(raw.get("firmware_refs")),
        extra_docs=_doc_refs_from_raw(raw.get("extra_docs") or raw.get("docs")),
        subsystems=subsystems,
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
# Loaders
# ---------------------------------------------------------------------------

def load_from_files(
    *,
    saved_nets_path: str = "/etc/lager/saved_nets.json",
    bench_json_path: str = "/etc/lager/bench.json",
    box_id_path: str = "/etc/lager/box_id",
    version_path: str = "/etc/lager/version",
    hostname_path: str = "/host/etc/hostname",
) -> BenchDefinition:
    """Build a BenchDefinition from on-disk JSON files (used on-box or in tests).

    Instruments are left empty here; see the module docstring.
    """

    raw_nets = _read_json(saved_nets_path, default=[])
    bench_cfg = _read_json(bench_json_path, default={})

    # Box identity (id / version / hostname) is NOT carried in saved_nets or
    # bench.json, so on an unauthored box discover_bench previously reported
    # these as empty even though the values sit in well-known files on disk
    # (box_manage already reads /etc/lager/version directly). Seed them here so
    # the two agree. bench.json still wins if it authored any of them (see
    # _assemble's precedence), so an operator can override.
    #
    # hostname's only correct source on-box is /host/etc/hostname (the host's
    # hostname bind-mounted into the container); the container's own
    # /etc/hostname is the container id, so we do NOT fall back to it -- an
    # empty hostname is better than a misleading one.
    hostname = _read_line(hostname_path)
    # Same fallback chain as config.get_box_id: a box installed before the id
    # file existed still has a hostname every operator knows it by, and an
    # empty box_id is useless to a client that keys many boxes on it.
    box_id = (
        _read_line(box_id_path)
        or os.environ.get("LAGER_BOX_ID", "").strip()
        or hostname
    )
    hello_data: dict[str, Any] = {"box_id": box_id}
    version = _read_box_version(version_path)
    if version:
        hello_data["version"] = version
    if hostname:
        hello_data["hostname"] = hostname

    return _assemble(
        hello_data=hello_data,
        raw_nets=raw_nets if isinstance(raw_nets, list) else [],
        raw_instruments=[],
        bench_cfg=bench_cfg if isinstance(bench_cfg, dict) else {},
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
    )


# ---------------------------------------------------------------------------
# Internal assembly
# ---------------------------------------------------------------------------

def _apply_override(nd: NetDescriptor, ovr: dict[str, Any]) -> list[str]:
    """Apply one ``net_overrides`` entry to a descriptor.

    Returns the fields the override set, malformed ones excluded, so the
    caller can record which source authored each field.
    """
    applied: list[str] = []
    if "aliases" in ovr:
        nd.aliases = ovr["aliases"] or []
        applied.append("aliases")
    if "voltage_domain" in ovr and isinstance(ovr["voltage_domain"], dict):
        try:
            nd.voltage_domain = VoltageRange(**ovr["voltage_domain"])
            applied.append("voltage_domain")
        except (TypeError, ValueError) as e:
            logger.warning("net %s: bad voltage_domain override (%s)", nd.name, e)
    if "safety_limits" in ovr and isinstance(ovr["safety_limits"], dict):
        try:
            nd.safety_limits = SafetyLimits(**ovr["safety_limits"])
            applied.append("safety_limits")
        except (TypeError, ValueError) as e:
            logger.warning("net %s: bad safety_limits override (%s)", nd.name, e)
    if "purpose" in ovr:
        nd.purpose = ovr["purpose"] or ""
        applied.append("purpose")
    if "notes" in ovr:
        nd.notes = ovr["notes"] or ""
        applied.append("notes")
    if "tags" in ovr:
        nd.tags = ovr["tags"] or []
        applied.append("tags")
    if "dut_connection" in ovr:
        nd.dut_connection = ovr["dut_connection"] or ""
        applied.append("dut_connection")
    if "test_hints" in ovr:
        nd.test_hints = [str(h) for h in (ovr["test_hints"] or [])]
        applied.append("test_hints")
    return applied


def _saved_fields(raw: dict[str, Any]) -> list[str]:
    """The overridable fields a saved-net record carries a value for."""
    return [
        field for field in OVERRIDABLE_NET_FIELDS
        if raw.get(field) not in (None, "", [], {})
    ]


def _assemble(
    *,
    hello_data: dict[str, Any],
    raw_nets: list[dict[str, Any]],
    raw_instruments: list[dict[str, Any]],
    bench_cfg: dict[str, Any],
) -> BenchDefinition:
    box_id = (
        bench_cfg.get("box_id")
        or hello_data.get("box_id")
        or hello_data.get("id")
        or ""
    )
    hostname = bench_cfg.get("hostname", hello_data.get("hostname", ""))
    version = bench_cfg.get("version", hello_data.get("version", ""))

    # Nets, with a record of which file authored each metadata field.
    nets: list[NetDescriptor] = []
    metadata_sources: dict[str, dict[str, str]] = {}
    net_overrides: dict[str, dict[str, Any]] = {
        o["name"]: o
        for o in (bench_cfg.get("net_overrides") or [])
        if isinstance(o, dict) and "name" in o
    }
    for raw in raw_nets:
        if not isinstance(raw, dict):
            logger.warning("saved_nets: skipping non-dict entry %r", raw)
            continue
        nd = _net_from_raw(raw)
        sources = {field: "saved_net" for field in _saved_fields(raw)}
        # Each override is applied independently so one malformed entry
        # can't corrupt the rest of the bench.
        ovr = net_overrides.get(nd.name)
        if ovr:
            for field in _apply_override(nd, ovr):
                sources[field] = "bench.json"
        nets.append(nd)
        if sources:
            metadata_sources[nd.name] = sources

    # Instruments (in-memory callers only; on a box they are attached live).
    instruments: list[InstrumentDescriptor] = [
        instrument_from_record(ri) for ri in raw_instruments if isinstance(ri, dict)
    ]

    # DUT slots — skip individual malformed entries instead of failing the
    # whole bench load. ``dut_slots`` is the legacy key; ``dut_context`` is
    # the newer single-DUT alternative for boxes with one slot.
    dut_slots: list[DUTContext] = []
    raw_slots = bench_cfg.get("dut_slots") or []
    if not raw_slots and isinstance(bench_cfg.get("dut_context"), dict):
        raw_slots = [bench_cfg["dut_context"]]
    for ds in raw_slots:
        if not isinstance(ds, dict):
            logger.warning("dut_slots: skipping non-dict entry %r", ds)
            continue
        try:
            dut_slots.append(_dut_context_from_raw(ds))
        except (TypeError, ValueError) as e:
            logger.warning("dut_slots: skipping malformed entry %r (%s)", ds, e)

    # Warn about subsystem net references that don't match any known net.
    # Dangling references silently break ``subsystem_for_net`` lookups (and
    # therefore the schematic-citation chain), so surface them at load time.
    if dut_slots:
        net_names = {n.name for n in nets}
        for dut in dut_slots:
            for sub in dut.subsystems:
                dangling = [ref for ref in sub.nets if ref not in net_names]
                if dangling:
                    logger.warning(
                        "DUT %r subsystem %r references unknown net(s) %s; "
                        "they will not resolve to any hardware. Check for typos "
                        "or stale entries in bench.json.",
                        dut.name, sub.name, dangling,
                    )

    # Interfaces — same per-entry tolerance.
    static_ifaces: list[InterfaceDescriptor] = []
    for iface in (bench_cfg.get("interfaces") or []):
        if not isinstance(iface, dict):
            logger.warning("interfaces: skipping non-dict entry %r", iface)
            continue
        try:
            static_ifaces.append(InterfaceDescriptor(**iface))
        except (TypeError, ValueError) as e:
            logger.warning("interfaces: skipping malformed entry %r (%s)", iface, e)
    inferred_ifaces = _infer_interfaces(nets)
    seen_names = {i.name for i in static_ifaces}
    interfaces = static_ifaces + [i for i in inferred_ifaces if i.name not in seen_names]

    # Safety constraints — bench-level, fall back to None on bad input.
    constraints = None
    if "constraints" in bench_cfg and isinstance(bench_cfg["constraints"], dict):
        try:
            constraints = SafetyConstraints(**bench_cfg["constraints"])
        except (TypeError, ValueError) as e:
            logger.warning("bench.json: bad constraints block (%s); ignoring", e)

    # Calibration — bench-level, fall back to default empty status.
    cal = CalibrationStatus()
    if "calibration" in bench_cfg and isinstance(bench_cfg["calibration"], dict):
        try:
            cal = CalibrationStatus(**bench_cfg["calibration"])
        except (TypeError, ValueError) as e:
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
        metadata_sources=metadata_sources,
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


def _read_line(path: str) -> str:
    """First line of *path*, stripped; empty string if it can't be read."""
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return fh.read().strip()
    except OSError:
        return ""


def _read_box_version(path: str) -> str:
    """Box software version from ``/etc/lager/version``.

    Mirrors ``config.get_box_version``: the file may carry a ``<ver>|<ver>``
    form (box|cli), so keep only the first field. Empty when absent.
    """
    content = _read_line(path)
    return content.split("|", 1)[0] if "|" in content else content
