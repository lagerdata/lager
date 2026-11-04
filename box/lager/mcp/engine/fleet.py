# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

"""Views of a STORED bench manifest, for a host that keeps many of them.

The box's MCP tools answer about the box they run on, from live state. A
fleet host (a control plane, a hosted MCP endpoint) instead holds manifests
that boxes published with ``GET /bench`` and asks the same questions of
them, sometimes for a box that is offline. This module answers those
questions from the manifest alone:

- ``summary`` / ``net_detail`` / ``dut_context`` / ``dut_overview``: what the
  bench is, without the box tools' advice on how to run a test (the host
  decides how tests run);
- ``cite``: which documents back one net;
- ``reference``: the driver API quick reference the manifest carries;
- ``suitability`` / ``search``: whether a bench can run a test, through the
  same ``heuristic_engine`` the box uses.

Every function takes the manifest as the plain dict a consumer stored, not
a ``BenchManifest``: a model would drop fields this release does not know
(pydantic ignores extras) and fill defaults the box never sent, and a view
must show what the box published. Values are read with the same
missing-versus-empty rules throughout: a key that is absent or ``None``
falls back, an empty value is kept as sent.

Any sentence that tells a reader where to go next is in ``MESSAGES`` and can
be replaced per call, so a host can point at its own screens without this
module naming them.

Pure: no I/O, no live box state, no imports from the server, the tools or
the resources (``test_engine_fleet`` enforces it).
"""

from __future__ import annotations

import json
from typing import Any, Iterable, Mapping

from ..schemas.capability import CapabilityGraph, CapabilityNode, CapabilityRole
from .heuristic_engine import assess_suitability, infer_requirements

Rec = dict[str, Any]

#: Replaceable reader-facing sentences. Keys are stable API; a caller passes
#: ``messages={key: text}`` to override any of them.
MESSAGES: dict[str, str] = {
    "advisory_note": (
        "Advisory only, not enforced by the box. Respect these in the test you write."
    ),
    "no_nets": (
        "No nets configured on this box. Tests cannot address any hardware until nets "
        "are registered (`lager nets add-all` on the box)."
    ),
    "no_dut": (
        "No DUT context has been authored for this box yet. Describe what the box tests, "
        "the DUT MCU and peripherals, and attach the schematic and datasheet references "
        "(`lager dut edit` on the box)."
    ),
    "cite_no_dut": "No DUT context has been authored on this box yet.",
    "docs_note": (
        "_The box does not host these files. `repo_path` entries live in the project, else "
        "under `~/.lager_dut_docs/`; `url` entries are fetched directly; `external_id` "
        "entries are documents a document store holds. Vision models read per-sheet images "
        "faster than whole PDFs._\n"
    ),
    "cite_guidance": (
        "`repo_path` entries live in the project, else under `~/.lager_dut_docs/`. `url` "
        "entries are fetched directly. `external_id` entries are documents a document store "
        "holds. Prefer per-sheet images for vision analysis and use `pages` to focus on the "
        "relevant sheet."
    ),
    "reference_unavailable": (
        "The driver API reference is not available for this box: its manifest carries no "
        "reference entries (a lager release before the entries were added). Update the box "
        "to get it."
    ),
}

#: Longest override accepted for one message.
MAX_MESSAGE_LEN = 2000


def resolve_messages(overrides: Mapping[str, Any] | None) -> dict[str, str]:
    """``MESSAGES`` with the caller's overrides applied.

    Raises ``ValueError`` for an unknown key or a value that is not a short
    string, so a typo in a caller is loud instead of silently ignored.
    """
    out = dict(MESSAGES)
    for key, value in (overrides or {}).items():
        if key not in MESSAGES:
            raise ValueError(f"unknown message key: {key!r}")
        if not isinstance(value, str) or len(value) > MAX_MESSAGE_LEN:
            raise ValueError(f"message {key!r} must be a string of at most {MAX_MESSAGE_LEN} characters")
        out[key] = value
    return out


# --- reading a raw manifest ---------------------------------------------------


def _get(obj: Any, key: str, default: Any = None) -> Any:
    """``obj[key]`` when it is present and not ``None``, else ``default``."""
    if isinstance(obj, Mapping):
        value = obj.get(key)
        if value is not None:
            return value
    return default


def _list(obj: Any, key: str) -> list:
    value = _get(obj, key, [])
    return value if isinstance(value, list) else []


def _text(obj: Any, key: str) -> str:
    """A field as text; anything that is not a non-empty string reads as ``""``."""
    value = obj.get(key) if isinstance(obj, Mapping) else None
    return value if isinstance(value, str) else ""


def _truthy(value: Any) -> bool:
    """JavaScript truthiness: an empty list or object is TRUE, unlike Python."""
    if value is None or value is False or value == "":
        return False
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return value == value and value != 0  # NaN and zero are falsy
    return True


def _is_empty(value: Any) -> bool:
    if value is None or value == "":
        return True
    if isinstance(value, (list, dict)) and not value:
        return True
    return False


def _drop_empty(obj: Mapping) -> Rec:
    return {k: v for k, v in obj.items() if not _is_empty(v)}


def compact_doc_ref(ref: Any) -> Rec:
    """A DocRef with ``None`` values dropped (the box serialises with ``exclude_none``)."""
    if not isinstance(ref, Mapping):
        return {}
    return {k: v for k, v in ref.items() if v is not None}


def _bench(manifest: Mapping) -> Mapping:
    bench = manifest.get("bench")
    return bench if isinstance(bench, Mapping) else {}


def box_id_of(manifest: Mapping) -> str:
    """``bench.box_id``, else the manifest's own ``box_id``, else ``""``. Empty is kept."""
    return _get(_bench(manifest), "box_id", _get(manifest, "box_id", ""))


def _nets(manifest: Mapping) -> list[Mapping]:
    return [n for n in _list(_bench(manifest), "nets") if isinstance(n, Mapping)]


def _bindings(manifest: Mapping) -> list[Mapping]:
    return [b for b in _list(_bench(manifest), "capability_bindings") if isinstance(b, Mapping)]


def _slots(manifest: Mapping) -> list[Mapping]:
    return [d for d in _list(_bench(manifest), "dut_slots") if isinstance(d, Mapping)]


def subsystem_for_net(dut: Mapping, net_name: str) -> Mapping | None:
    """The first subsystem of a DUT slot that lists the net, or ``None``."""
    for sub in _list(dut, "subsystems"):
        if isinstance(sub, Mapping) and net_name in _list(sub, "nets"):
            return sub
    return None


def primary_dut(manifest: Mapping) -> Mapping | None:
    """The first active DUT slot, else the first slot, else ``None``."""
    slots = _slots(manifest)
    for d in slots:
        if _truthy(d.get("active")):
            return d
    return slots[0] if slots else None


# --- bench summary ------------------------------------------------------------


def _instrument_entry(inst: Mapping) -> Rec:
    entry: Rec = {}
    for out_key, in_key in (("name", "name"), ("type", "instrument_type"), ("connection", "connection")):
        if in_key in inst:
            entry[out_key] = inst[in_key]
    for key in ("channels", "capabilities", "firmware_version", "metadata"):
        value = inst.get(key)
        if not _is_empty(value):
            entry[key] = value
    return entry


def _net_entry(net: Mapping) -> Rec:
    entry: Rec = {
        "name": net.get("name"),
        "type": net.get("net_type"),
        "instrument": _get(net, "instrument", ""),
        "roles": _get(net, "roles", []),
    }
    if _truthy(net.get("purpose")):
        entry["purpose"] = net["purpose"]
    if _list(net, "tags"):
        entry["tags"] = net["tags"]
    if _truthy(net.get("dut_connection")):
        entry["dut_connection"] = net["dut_connection"]
    if _list(net, "test_hints"):
        entry["test_hints"] = list(net["test_hints"])
    return entry


def summary(manifest: Mapping, messages: Mapping[str, Any] | None = None) -> Rec:
    """The bench in one structure: identity, DUT slots, instruments, nets, interfaces, capability counts."""
    msg = resolve_messages(messages)
    bench = _bench(manifest)
    bindings = _bindings(manifest)
    role_counts: dict[Any, int] = {}
    for b in bindings:
        role = b.get("role")
        role_counts[role] = role_counts.get(role, 0) + 1

    out: Rec = {
        "box_id": box_id_of(manifest),
        "hostname": _get(bench, "hostname", ""),
        "version": _get(bench, "version", ""),
        "dut_slots": [
            {
                "name": d.get("name"),
                "active": _get(d, "active", True),
                "board_profile": d.get("board_profile"),
                "purpose": _get(d, "purpose", ""),
                "mcu": d.get("mcu"),
            }
            for d in _slots(manifest)
        ],
        "instruments": [
            _instrument_entry(i) for i in _list(bench, "instruments") if isinstance(i, Mapping)
        ],
        "nets": [_net_entry(n) for n in _nets(manifest)],
        "interfaces": [
            {
                "name": _text(i, "name"),
                "protocol": _text(i, "protocol"),
                "roles": i["roles"] if isinstance(i.get("roles"), list) else [],
            }
            for i in _list(bench, "interfaces")
            if isinstance(i, Mapping)
        ],
        "capability_summary": role_counts,
        "total_capabilities": len(bindings),
    }

    constraints = bench.get("constraints")
    if isinstance(constraints, Mapping) and (_truthy(constraints.get("max_voltage")) or _truthy(constraints.get("max_current"))):
        out["advisory_limits"] = {
            "note": msg["advisory_note"],
            "max_voltage": constraints.get("max_voltage"),
            "max_current": constraints.get("max_current"),
        }
    if not _nets(manifest):
        out["warning"] = msg["no_nets"]
    return out


# --- one net ------------------------------------------------------------------


def net_detail(manifest: Mapping, net_name: str) -> Rec | None:
    """Everything the manifest says about one net, or ``None`` when the net is unknown."""
    bench = _bench(manifest)
    net = next((n for n in _nets(manifest) if n.get("name") == net_name), None)
    if net is None:
        return None
    payload: Rec = {"box_id": box_id_of(manifest), **_drop_empty(net)}
    payload["name"] = net.get("name")
    if "net_type" in net:
        payload["net_type"] = net["net_type"]
    else:
        payload.pop("net_type", None)
    payload["capabilities"] = [dict(b) for b in _bindings(manifest) if b.get("target") == net_name]

    sources = _get(bench, "metadata_sources", {})
    net_sources = sources.get(net_name) if isinstance(sources, Mapping) else None
    if isinstance(net_sources, Mapping) and net_sources:
        payload["metadata_sources"] = net_sources
    for dut in _slots(manifest):
        sub = subsystem_for_net(dut, net_name)
        if sub is None:
            continue
        payload["dut"] = dut.get("name")
        payload["subsystem"] = {
            "name": sub.get("name"),
            "summary": _get(sub, "summary", ""),
            "doc_refs": [compact_doc_ref(r) for r in _list(sub, "doc_refs")],
        }
        if _list(dut, "schematic_refs"):
            payload["dut_schematic_refs"] = [compact_doc_ref(r) for r in dut["schematic_refs"]]
        break
    return payload


# --- DUT context ----------------------------------------------------------------


def dut_context(manifest: Mapping, messages: Mapping[str, Any] | None = None) -> Rec:
    """The DUT slots in full, as a structure. A box with none gets a warning instead."""
    msg = resolve_messages(messages)
    bench = _bench(manifest)
    box_id = box_id_of(manifest)
    slots = _slots(manifest)
    if not slots:
        return {"box_id": box_id, "dut_slots": [], "warning": msg["no_dut"]}
    calibration = bench.get("calibration")
    if not _truthy(calibration):
        healthy = True
    elif isinstance(calibration, Mapping):
        healthy = _truthy(calibration.get("healthy"))
    else:
        healthy = False

    def refs(obj: Mapping, key: str) -> list[Rec]:
        return [compact_doc_ref(r) for r in _list(obj, key)]

    return {
        "box_id": box_id,
        "hostname": _get(bench, "hostname", ""),
        "calibration_healthy": healthy,
        "dut_updated_at": _get(bench, "dut_updated_at", ""),
        "dut_slots": [
            {
                "name": d.get("name"),
                "active": _get(d, "active", True),
                "purpose": _get(d, "purpose", ""),
                "summary": _get(d, "summary", ""),
                "board_profile": d.get("board_profile"),
                "mcu": d.get("mcu"),
                "firmware": d.get("firmware"),
                "key_peripherals": _get(d, "key_peripherals", []),
                "subsystems": [
                    {
                        "name": s.get("name"),
                        "summary": _get(s, "summary", ""),
                        "nets": _get(s, "nets", []),
                        "doc_refs": refs(s, "doc_refs"),
                    }
                    for s in _list(d, "subsystems")
                    if isinstance(s, Mapping)
                ],
                "schematic_refs": refs(d, "schematic_refs"),
                "datasheet_refs": refs(d, "datasheet_refs"),
                "firmware_refs": refs(d, "firmware_refs"),
                "extra_docs": refs(d, "extra_docs"),
            }
            for d in slots
        ],
    }


def _doc_ref_markdown(ref: Any) -> str:
    ref = ref if isinstance(ref, Mapping) else {}
    url = _text(ref, "url")
    repo_path = _text(ref, "repo_path")
    external_url = _text(ref, "external_url")
    external_id = _text(ref, "external_id")
    pages = _text(ref, "pages")
    notes = _text(ref, "notes")
    parts = [f"**{_text(ref, 'title')}**"]
    where = []
    if url:
        where.append(f"[{url}]({url})")
    if repo_path:
        where.append(f"`{repo_path}` (in the project, else under `~/.lager_dut_docs/`)")
    if external_url:
        where.append(f"[{external_url}]({external_url}) (document store)")
    if external_id:
        where.append(f"`{external_id}` (document store ID)")
    if where:
        parts.append(" - " + " / ".join(where))
    if pages:
        parts.append(f"  \n  pages: {pages}")
    if notes:
        parts.append(f"  \n  {notes}")
    return "- " + "".join(parts)


def _net_row(net: Mapping) -> str:
    purpose = _get(net, "purpose", "")
    purpose = purpose.replace("|", "\\|") if isinstance(purpose, str) else str(purpose)
    return f"| `{net.get('name')}` | {net.get('net_type')} | {purpose} |"


def dut_overview(manifest: Mapping, messages: Mapping[str, Any] | None = None) -> str:
    """A markdown briefing: what the box tests, the documents to read, nets by subsystem."""
    msg = resolve_messages(messages)
    bench = _bench(manifest)
    box_id = box_id_of(manifest)
    lines = ["# DUT Overview\n"]
    if _truthy(box_id):
        hostname = bench.get("hostname")
        lines.append(f"**Box:** `{box_id}`" + (f" ({hostname})" if _truthy(hostname) else ""))
        lines.append("")
    slots = _slots(manifest)
    if not slots:
        lines.append(f"_{msg['no_dut']}_")
        return "\n".join(lines)
    nets = _nets(manifest)
    for dut in slots:
        lines.append(f"## DUT: {dut.get('name')}" + ("  _(inactive)_" if dut.get("active") is False else ""))
        if _truthy(dut.get("purpose")):
            lines.append(f"\n**Purpose:** {dut['purpose']}")
        if _truthy(dut.get("board_profile")):
            lines.append(f"**Board profile:** `{dut['board_profile']}`")
        if _truthy(dut.get("mcu")):
            lines.append(f"**MCU:** `{dut['mcu']}`")
        if _truthy(dut.get("firmware")):
            lines.append(f"**Firmware:** `{dut['firmware']}`")
        if _list(dut, "key_peripherals"):
            lines.append("**Key peripherals:** " + ", ".join(f"`{p}`" for p in dut["key_peripherals"]))
        if _truthy(dut.get("summary")):
            lines.append("\n### Summary\n")
            lines.append(dut["summary"])

        doc_sections = [
            ("Schematics", _list(dut, "schematic_refs")),
            ("Datasheets", _list(dut, "datasheet_refs")),
            ("Firmware references", _list(dut, "firmware_refs")),
            ("Other documents", _list(dut, "extra_docs")),
        ]
        if any(refs for _, refs in doc_sections):
            lines.append("\n### Documents to read")
            lines.append(msg["docs_note"])
            for title, refs in doc_sections:
                if not refs:
                    continue
                lines.append(f"**{title}**")
                for ref in refs:
                    lines.append(_doc_ref_markdown(ref))
                lines.append("")

        subsystems = [s for s in _list(dut, "subsystems") if isinstance(s, Mapping)]
        if subsystems:
            lines.append("### Subsystems\n")
            for sub in subsystems:
                lines.append(f"#### {sub.get('name')}")
                if _truthy(sub.get("summary")):
                    lines.append(sub["summary"])
                sub_nets = _list(sub, "nets")
                if sub_nets:
                    in_sub = [n for n in nets if n.get("name") in sub_nets]
                    if in_sub:
                        lines.append("\n| Net | Type | Purpose |")
                        lines.append("| --- | --- | --- |")
                        lines.extend(_net_row(n) for n in in_sub)
                if _list(sub, "doc_refs"):
                    lines.append("\n_Subsystem documents:_")
                    lines.extend(_doc_ref_markdown(r) for r in sub["doc_refs"])
                lines.append("")

        assigned = {n for sub in subsystems for n in _list(sub, "nets")}
        orphans = [n for n in nets if n.get("name") not in assigned]
        if orphans:
            lines.append("### Other nets\n")
            lines.append("| Net | Type | Purpose |")
            lines.append("| --- | --- | --- |")
            lines.extend(_net_row(n) for n in orphans)
            lines.append("")
    return "\n".join(lines)


# --- citations ------------------------------------------------------------------


def _js_json(value: Any, indent: int | None = None) -> str:
    """JSON text laid out the way ``JSON.stringify(value, null, indent)`` lays it out."""
    if indent is None:
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    return json.dumps(value, ensure_ascii=False, indent=indent, separators=(",", ": "))


def to_doc_reference(ref: Any) -> Rec | None:
    """A doc ref in the citation shape (camelCase, non-empty strings only), or ``None`` without a title."""
    if not isinstance(ref, Mapping):
        return None
    title = ref.get("title")
    if not isinstance(title, str) or not title:
        return None
    out: Rec = {"title": title}
    for out_key, in_key in (
        ("kind", "kind"),
        ("url", "url"),
        ("repoPath", "repo_path"),
        ("externalId", "external_id"),
        ("externalUrl", "external_url"),
        ("pages", "pages"),
        ("notes", "notes"),
    ):
        value = ref.get(in_key)
        if isinstance(value, str) and value:
            out[out_key] = value
    return out


def cite(manifest: Mapping, net_name: str, messages: Mapping[str, Any] | None = None) -> Rec:
    """Which documents back one net: the DUT and subsystem that own it, and the refs to open.

    ``error`` is set when the net is not on the bench and ``warning`` when no
    DUT is authored. ``raw`` is the full payload as JSON text, the shape the
    box's ``cite_schematic`` returns.
    """
    msg = resolve_messages(messages)
    box_id = box_id_of(manifest)
    net = next((n for n in _nets(manifest) if n.get("name") == net_name), None)
    if net is None:
        error = f"Net '{net_name}' not found."
        return {
            "net": net_name,
            "refs": [],
            "error": error,
            "raw": _js_json({"box_id": box_id, "error": error}),
        }

    dut = sub = None
    for d in _slots(manifest):
        s = subsystem_for_net(d, net_name)
        if s is not None:
            dut, sub = d, s
            break
    if dut is None:
        dut = primary_dut(manifest)

    head: Rec = {"net": net_name}
    if _truthy(net.get("purpose")):
        head["purpose"] = net["purpose"]

    if dut is None:
        warning = msg["cite_no_dut"]
        return {
            **head,
            "refs": [],
            "warning": warning,
            "raw": _js_json({"box_id": box_id, "net": net_name, "warning": warning, "doc_refs": []}, indent=2),
        }

    schematic_refs = [compact_doc_ref(r) for r in _list(dut, "schematic_refs")]
    datasheet_refs = [compact_doc_ref(r) for r in _list(dut, "datasheet_refs")]
    subsystem_refs = [compact_doc_ref(r) for r in _list(sub, "doc_refs")] if sub is not None else []
    payload = {
        "box_id": box_id,
        "net": net_name,
        "net_purpose": _get(net, "purpose", ""),
        "net_notes": _get(net, "notes", ""),
        "net_dut_connection": _get(net, "dut_connection", ""),
        "net_test_hints": _get(net, "test_hints", []),
        "dut": dut.get("name"),
        "subsystem": sub.get("name") if sub is not None else None,
        "subsystem_summary": _get(sub, "summary") if sub is not None else None,
        "schematic_refs": schematic_refs,
        "datasheet_refs": datasheet_refs,
        "subsystem_doc_refs": subsystem_refs,
        "guidance": msg["cite_guidance"],
    }
    refs = [r for r in (to_doc_reference(x) for x in schematic_refs + datasheet_refs + subsystem_refs) if r]
    out: Rec = {**head, "dut": dut.get("name")}
    if sub is not None:
        out["subsystem"] = sub.get("name")
    out["refs"] = refs
    out["raw"] = _js_json(payload, indent=2)
    return out


# --- API reference ------------------------------------------------------------------


def has_api_reference(manifest: Mapping) -> bool:
    """Whether the box serves the driver reference at all (the field's presence, not its size)."""
    return manifest.get("reference_entries") is not None


def reference_keys_for(manifest: Mapping, net_names: Iterable[str] | None = None) -> list[str]:
    """The API reference keys for the given nets (every net when omitted), sorted. Unknown nets are ignored."""
    keys = _get(manifest, "reference_keys", {})
    keys = keys if isinstance(keys, Mapping) else {}
    wanted = list(net_names) if net_names is not None else list(keys)
    return sorted({keys[n] for n in wanted if keys.get(n)})


def reference(
    manifest: Mapping,
    net_names: Iterable[str] | None = None,
    include_gotchas: bool = False,
    include_examples: bool = False,
    messages: Mapping[str, Any] | None = None,
) -> Rec:
    """The ``lager.Net`` API quick reference for the bench's net types, as markdown.

    ``available`` is false when the manifest predates ``reference_entries``;
    ``keys`` are the reference keys for the nets asked about.
    """
    msg = resolve_messages(messages)
    net_names = list(net_names) if net_names is not None else None
    keys = reference_keys_for(manifest, net_names)
    if not has_api_reference(manifest):
        return {"available": False, "keys": keys, "text": msg["reference_unavailable"]}
    entries = manifest.get("reference_entries")
    entries = entries if isinstance(entries, Mapping) else {}
    present = [k for k in keys if k in entries]
    lines = [
        "# lager.Net API Quick Reference\n",
        "```python",
        "from lager import Net, NetType",
        "net = Net.get('name', type=NetType.X)",
        "```\n",
    ]
    if not present:
        lines.append("_No net of a documented type is on this bench._")
        return {"available": True, "keys": keys, "text": "\n".join(lines)}
    for key in present:
        ref = entries[key] if isinstance(entries[key], Mapping) else {}
        lines.append(f"## {key}  (`{_get(ref, 'net_type_enum', '')}`)")
        if _truthy(ref.get("get_pattern")):
            lines.append("```python\n" + ref["get_pattern"] + "\n```")
        for m in _list(ref, "methods"):
            m = m if isinstance(m, Mapping) else {}
            sig = _get(m, "sig", _get(m, "name", ""))
            lines.append(f"- `{sig}`: {_get(m, 'desc', '')}")
        if include_gotchas and _list(ref, "gotchas"):
            lines.append("\nGotchas:")
            lines.extend(f"- {g}" for g in ref["gotchas"])
        if include_examples and _truthy(ref.get("example_snippet")):
            snippet = ref["example_snippet"]
            if snippet.endswith("\n"):
                snippet = snippet[:-1]
            lines.append("\nExample:\n```python\n" + snippet + "\n```")
        lines.append("")
    return {"available": True, "keys": keys, "text": "\n".join(lines)}


# --- suitability ------------------------------------------------------------------

_KNOWN_ROLES = {r.value for r in CapabilityRole}


def _confidence(value: Any) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 1.0
    return float(value)


def graph_from_bindings(box_id: str, bindings: Iterable[Any]) -> tuple[CapabilityGraph, list[str]]:
    """The capability graph a manifest carries, and warnings for nodes it had to skip.

    Built from the stored bindings, never re-derived from the nets: the box's
    own view is what it published, and a newer box's net-type table may
    differ from this release's. A node whose role this release does not know
    is skipped and named in the warnings; it could never satisfy a requirement
    this release defines, so the assessment is unchanged by skipping it.
    """
    nodes: list[CapabilityNode] = []
    unknown: list[str] = []
    for b in bindings:
        if not isinstance(b, Mapping):
            continue
        role = b.get("role")
        if role not in _KNOWN_ROLES:
            unknown.append(str(role))
            continue
        nodes.append(
            CapabilityNode(role=CapabilityRole(role), target=str(_get(b, "target", "")), confidence=_confidence(b.get("confidence")))
        )
    warnings = [f"Skipped capability bindings with a role this engine does not know: {sorted(set(unknown))}"] if unknown else []
    return CapabilityGraph(box_id=box_id, nodes=nodes), warnings


def assess_bindings(description: str, box_id: str, bindings: Iterable[Any]) -> Rec:
    """``{requirement, report}`` for one bench's capability bindings."""
    req = infer_requirements(description)
    graph, warnings = graph_from_bindings(box_id, bindings)
    out: Rec = {
        "requirement": req.model_dump(mode="json"),
        "report": assess_suitability(req, graph).model_dump(mode="json"),
    }
    if warnings:
        out["warnings"] = warnings
    return out


def suitability(manifest: Mapping, description: str) -> Rec:
    """Resolve a test type or description and assess it against a stored manifest."""
    return assess_bindings(description, box_id_of(manifest), _bindings(manifest))


def search(description: str, benches: Iterable[Mapping]) -> Rec:
    """Assess one description against many benches.

    Each bench is ``{key, box_id, capability_bindings}``. A bench that cannot
    be assessed gets ``{error}`` under its key; the others are unaffected.
    Ranking is the caller's: it knows which boxes are online.
    """
    req = infer_requirements(description)
    reports: dict[str, Rec] = {}
    for bench in benches:
        key = str(bench.get("key", "")) if isinstance(bench, Mapping) else ""
        try:
            if not key:
                raise ValueError("bench has no key")
            bindings = bench.get("capability_bindings") or []
            if not isinstance(bindings, list):
                raise ValueError("capability_bindings is not a list")
            graph, warnings = graph_from_bindings(str(bench.get("box_id") or ""), bindings)
            entry: Rec = {"report": assess_suitability(req, graph).model_dump(mode="json")}
            if warnings:
                entry["warnings"] = warnings
            reports[key] = entry
        except Exception as exc:  # pylint: disable=broad-except
            reports[key or f"#{len(reports)}"] = {"error": str(exc)}
    return {"requirement": req.model_dump(mode="json"), "reports": reports}


__all__ = [
    "MESSAGES",
    "assess_bindings",
    "box_id_of",
    "cite",
    "compact_doc_ref",
    "dut_context",
    "dut_overview",
    "graph_from_bindings",
    "has_api_reference",
    "net_detail",
    "primary_dut",
    "reference",
    "reference_keys_for",
    "resolve_messages",
    "search",
    "subsystem_for_net",
    "suitability",
    "summary",
    "to_doc_reference",
]
