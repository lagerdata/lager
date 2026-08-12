# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0
"""Bootstrap for the measurement unit suite: the REAL ``lager`` package.

Same recipe as test/unit/box/conftest.py: box/ goes to the FRONT of
sys.path, the two genuinely-absent third-party modules are stubbed, the
real ``lager`` is imported exactly once, and we assert it is the on-disk
package. Every hardware SDK reachable from lager.measurement (joulescope,
ppk2_api, yoctopuce, Phidget22) sits behind a guarded try/except in the
module that uses it, so the real import needs nothing else -- the tests
patch the resulting module attributes directly (e.g. ppk2_watt.PPK2_API,
which is None without the lib), which is the standard mock idiom and
needs no help from this file.

History: this file used to register a placeholder ``lager`` whose
``__init__`` never executed and hand-load ten modules by path, re-binding
each onto its parent so ``mock.patch`` dotted lookups could resolve. That
machinery faked what the import system does for free, and produced a
py3.10-only failure class (mock's dotted lookup falls back to a real
import on 3.11+, which masked missing parent bindings). It predates the
box suite proving that the real package imports cleanly on a hosted
runner with two targeted stubs.

Process contract, unchanged: this suite wants the real ``lager``; a suite
that stubs the name cannot share a pytest process with it. One suite per
pytest invocation -- never bare ``pytest test/unit/``.
"""

from __future__ import annotations

import os
import sys
import types
from unittest.mock import MagicMock

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
BOX_DIR = os.path.join(REPO_ROOT, "box")

# FRONT of sys.path, remove-then-insert -- see test/unit/box/conftest.py for
# why a later entry losing to a stray `lager` directory matters.
if BOX_DIR in sys.path:
    sys.path.remove(BOX_DIR)
sys.path.insert(0, BOX_DIR)


def _stub_module(dotted: str) -> types.ModuleType:
    """Register a MagicMock-backed placeholder module, and its parents."""
    parts = dotted.split(".")
    for i in range(1, len(parts) + 1):
        key = ".".join(parts[:i])
        if key not in sys.modules:
            mod = types.ModuleType(key)
            mod.__path__ = []  # importable as a package
            mod.__getattr__ = lambda attr: MagicMock()  # type: ignore[method-assign]
            sys.modules[key] = mod
    return sys.modules[dotted]


class GdbTimeoutError(Exception):
    """Stand-in for ``pygdbmi.constants.GdbTimeoutError``.

    Must be a real exception class, not a MagicMock attribute -- see the
    matching definition in test/unit/box/conftest.py.
    """


# The same two stubs the box suite uses: the only third-party imports
# reachable from box/lager that are neither guarded by try/except nor
# provided by test/requirements-unit.txt. Nothing in this suite touches
# them today; they are here so box/ and measurement/ share ONE bootstrap
# recipe and a future import chain cannot silently diverge the two.
_stub_module("flask_socketio")
_stub_module("pygdbmi")
_gdbcontroller = _stub_module("pygdbmi.gdbcontroller")
_gdbcontroller.GdbController = object  # type: ignore[attr-defined]
_constants = _stub_module("pygdbmi.constants")
_constants.GdbTimeoutError = GdbTimeoutError  # type: ignore[attr-defined]

import lager  # noqa: E402

# A namespace package has __file__ is None. Fail loudly, once, here.
_lager_file = getattr(lager, "__file__", None)
assert _lager_file is not None, (
    "`lager` resolved to a placeholder or namespace package with __path__="
    f"{list(getattr(lager, '__path__', []))!r} instead of the real package "
    f"under {BOX_DIR}.\n"
    "Either something named `lager` shadows box/lager on sys.path, or another "
    "suite's conftest registered a stub `lager` first -- run this suite in "
    "its own pytest invocation."
)
assert os.path.realpath(_lager_file).startswith(os.path.realpath(BOX_DIR)), (
    f"`lager` resolved to {_lager_file}, which is not the in-repo box "
    f"package under {BOX_DIR}."
)
