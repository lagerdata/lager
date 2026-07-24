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
