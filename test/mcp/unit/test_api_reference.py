# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for lager.mcp.data.api_reference driver introspection.

The agent-facing API reference is introspected at import time from the driver
class named in ``_DRIVER_CLASSES``. That class must be the *user-facing* class
an agent's ``lager python`` script actually calls -- not a backend controller.

Regression this guards: ``NetType.Usb`` pointed at the hub-controller ABC
``usb_net.USBNet`` (whose methods take ``(net_name, port)``) instead of
``USBNetWrapper`` (whose methods take no args). Introspection therefore
advertised ``enable(net_name, port)`` to agents, a calling convention the
object returned by ``Net.get(name, NetType.Usb)`` rejects.

Two independent checks:

* ``TestDriverSurface`` -- no driver's public method may require the backend
  plumbing params ``net_name`` / ``port``. Runs with no dependency on
  ``lager.nets``; always catches the USBNet-style mistake.
* ``TestDriverClassMatchesDispatch`` -- for every net type ``Net.get``
  constructs directly, the returned class must be a subclass of the driver
  class. This is the "matches what Net.get dispatches to" guarantee; it also
  guards the other twelve entries.
"""

import ast
import inspect
import textwrap

import pytest

from lager.mcp.data.api_reference import (
    _DRIVER_CLASSES,
    _INTROSPECT_SKIP,
    _import_class,
)

# Params that only the backend hub-controller layer takes; the user-facing
# driver a script calls addresses a single already-resolved net/port.
_PLUMBING_PARAMS = {"net_name", "port"}

# Methods where one of those names is a DOMAIN parameter rather than backend
# plumbing. Keep this list short and justified: each entry is a hole in the
# check above, so it must name the exact method, never a whole class.
_PLUMBING_PARAM_ALLOWED = {
    # MikroTikRouter.block_port(port, protocol) blocks a TCP/UDP PORT NUMBER on
    # the router's firewall. Nothing to do with a USB hub port, and the caller
    # supplies it -- which is the whole point of the method.
    ("Router", "block_port"),
}


def _public_methods(cls):
    """(name, signature) for each method api_reference would introspect."""
    for name, member in inspect.getmembers(cls, predicate=inspect.isfunction):
        if name.startswith("_") or name in _INTROSPECT_SKIP:
            continue
        yield name, inspect.signature(member)


class TestDriverSurface:
    def test_every_driver_class_imports(self):
        for net_type, dotted in _DRIVER_CLASSES.items():
            cls = _import_class(dotted)
            assert isinstance(cls, type), f"{net_type}: {dotted} is not a class"

    def test_no_driver_exposes_backend_plumbing_params(self):
        offenders = {}
        for net_type, dotted in _DRIVER_CLASSES.items():
            cls = _import_class(dotted)
            leaky = [
                f"{name}{sig}"
                for name, sig in _public_methods(cls)
                if _PLUMBING_PARAMS & (set(sig.parameters) - {"self"})
                and (net_type, name) not in _PLUMBING_PARAM_ALLOWED
            ]
            if leaky:
                offenders[net_type] = leaky
        assert not offenders, (
            f"driver classes exposing backend plumbing params (net_name/port): "
            f"{offenders}"
        )


def _role_dispatch():
    """Return (net module, {NetType name -> set of directly-returned classes}).

    Statically walks the ``Net`` class source for ``if role == NetType.X`` /
    ``if mux_role == NetType.X`` blocks and records every ``return ClassName(...)``
    inside them. Role dispatch is split across ``Net.get`` (mux fallback) and
    ``Net.get_from_saved_json`` (main table), so the whole class is parsed.
    Net types that return a bare ``Net`` proxy (power supply / eload / battery)
    have no direct class and simply don't appear in the map.
    """
    netmod = pytest.importorskip("lager.nets.net")
    tree = ast.parse(textwrap.dedent(inspect.getsource(netmod.Net)))
    out: dict[str, set] = {}
    for node in ast.walk(tree):
        if not (isinstance(node, ast.If) and isinstance(node.test, ast.Compare)):
            continue
        test = node.test
        if not (
            len(test.ops) == 1
            and isinstance(test.left, ast.Name)
            and test.left.id in ("role", "mux_role")
        ):
            continue
        comp = test.comparators[0]
        if isinstance(test.ops[0], ast.Eq) and isinstance(comp, ast.Attribute):
            roles = [comp.attr]
        elif isinstance(test.ops[0], ast.In) and isinstance(comp, (ast.Tuple, ast.List)):
            roles = [e.attr for e in comp.elts if isinstance(e, ast.Attribute)]
        else:
            continue
        for ret in (
            n for n in ast.walk(node)
            if isinstance(n, ast.Return) and isinstance(n.value, ast.Call)
        ):
            fn = ret.value.func
            cname = fn.id if isinstance(fn, ast.Name) else getattr(fn, "attr", None)
            if cname:
                for role in roles:
                    out.setdefault(role, set()).add(cname)
    return netmod, out


class TestDriverClassMatchesDispatch:
    def test_dispatch_map_is_populated(self):
        # Guards against silently parsing the wrong method: the main role table
        # lives in get_from_saved_json, not Net.get, so a naive scan of Net.get
        # alone misses most types. Usb must resolve for the cross-check to mean
        # anything.
        _netmod, dispatch = _role_dispatch()
        assert dispatch.get("Usb"), (
            "no role dispatch parsed for NetType.Usb; the source walker is "
            "looking at the wrong method"
        )

    def test_driver_class_is_base_of_dispatched_class(self):
        netmod, dispatch = _role_dispatch()
        mismatches = []
        checked = 0
        for net_type, dotted in _DRIVER_CLASSES.items():
            returned = dispatch.get(net_type)
            if not returned:
                continue  # proxy type (returns a Net) -- covered by TestDriverSurface
            driver = _import_class(dotted)
            for cname in sorted(returned):
                cls = getattr(netmod, cname, None)
                if cls is None:
                    continue  # locally-scoped name, not resolvable on the module
                checked += 1
                if not issubclass(cls, driver):
                    mismatches.append(
                        f"NetType.{net_type}: _DRIVER_CLASSES -> {dotted}, but "
                        f"Net.get returns {cname}, which is not a subclass of it"
                    )
        assert checked, "no dispatched classes were cross-checked"
        assert not mismatches, "driver/dispatch mismatch:\n" + "\n".join(mismatches)

    def test_usb_entry_matches_wrapper(self):
        # Direct pin on the exact entry that was wrong.
        from lager.automation.usb_hub.usb_net_wrapper import USBNetWrapper

        _netmod, dispatch = _role_dispatch()
        assert "USBNetWrapper" in dispatch.get("Usb", set())
        assert _import_class(_DRIVER_CLASSES["Usb"]) is USBNetWrapper


# Types with no API_REFERENCE entry, each for a stated reason. A NetType that is
# merely undocumented does NOT belong here -- add an entry instead.
_NO_REFERENCE_EXPECTED = {
    # Provisioning/fixture motion controls, not test-time net access. No driver
    # surface an agent would script against.
    "Rotation",
    "Actuate",
    # A four-quadrant supply is driven through the PowerSupply entry; the alias
    # map already routes "power-supply-2q" there.
    "PowerSupply2Q",
    # No driver and no CLI surface ships for it yet.
    "Waveform",
}


class TestEveryNetTypeIsCovered:
    """The map was only ever checked in one direction.

    ``_apply_introspection`` warns when a _DRIVER_CLASSES key is missing from
    API_REFERENCE, but nothing checked that a NetType has an entry at all. A
    type that is absent is never introspected and ``lager://reference/<Type>``
    answers with an error payload -- silently, which is how Router shipped with
    37 undocumented methods including the bench's only fault-injection tooling.
    """

    def test_every_nettype_has_a_reference_or_a_stated_reason(self):
        from lager.mcp.data.api_reference import get_reference_for_type
        from lager.nets.constants import NetType

        missing = [
            member.name
            for member in NetType
            if get_reference_for_type(member.name) is None
            and member.name not in _NO_REFERENCE_EXPECTED
        ]
        assert not missing, (
            "NetType members with no API_REFERENCE entry and no stated reason: "
            f"{missing}. Add a curated entry, or add the name to "
            "_NO_REFERENCE_EXPECTED with a comment saying why."
        )

    def test_the_exclusion_list_has_no_stale_entries(self):
        """An excluded type that later gained an entry should leave this list."""
        from lager.mcp.data.api_reference import get_reference_for_type
        from lager.nets.constants import NetType

        known = {member.name for member in NetType}
        assert _NO_REFERENCE_EXPECTED <= known, (
            f"stale names: {_NO_REFERENCE_EXPECTED - known}"
        )
        covered = [
            name for name in _NO_REFERENCE_EXPECTED
            if get_reference_for_type(name) is not None
        ]
        assert not covered, (
            f"{covered} now have entries; remove them from _NO_REFERENCE_EXPECTED"
        )


class TestCuratedEntriesAreWellFormed:
    def test_every_entry_has_the_required_keys(self):
        from lager.mcp.data.api_reference import API_REFERENCE

        # guide.py reads all four unconditionally and raises KeyError otherwise.
        for name, ref in API_REFERENCE.items():
            for key in ("net_type_enum", "get_pattern", "methods", "gotchas",
                        "example_snippet"):
                assert key in ref, f"{name} is missing {key!r}"
            assert ref["methods"], f"{name} has an empty methods list"

    def test_no_method_description_is_empty(self):
        """Introspection overwrites the curated list with docstring first lines.

        A driver method with no docstring yields desc="", which reaches an agent
        as a named method with no explanation.
        """
        from lager.mcp.data.api_reference import API_REFERENCE

        blank = [
            f"{name}.{m['name']}"
            for name, ref in API_REFERENCE.items()
            for m in ref["methods"]
            if not m["desc"].strip()
        ]
        assert not blank, f"methods introspected with no description: {blank}"
