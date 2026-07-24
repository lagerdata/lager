# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0
"""Deterministic bootstrap for the box unit suite.

pytest imports a directory's conftest before it imports any test module in that
directory -- and, when the directory is named on the command line, during
``Config._preparse``, i.e. before collection starts at all. That is the only
hook that runs early enough to settle ``sys.modules`` for all 55 box test
modules at once, because each module does its own import surgery at module
level (collection time).

Without this file the suite is order-dependent. Twelve modules register a
placeholder ``lager`` so they can load individual box files by path without
executing the heavy package ``__init__``; twenty-two module-level
``from lager ... import`` statements need that ``__init__`` to have run. A
module whose ``__init__`` never executed has no top-level names, and CPython's
IMPORT_FROM reports that as::

    ImportError: cannot import name 'DeviceNotFoundError' from 'lager'
                 (unknown location)

The only thing reconciling the two groups today is alphabetical collection
order plus ``if <name> in sys.modules`` guards -- a contract three modules
document as load-bearing (test_binaries_store.py, test_acroname_driver.py,
test_debug_net_self_heal.py). Any perturbation flips it: an explicit file list,
-k, --ignore, xdist sharding, or simply a new test file that sorts earlier.

The same accident governs ``pygdbmi``. Modules that stub it generically give
``pygdbmi.constants.GdbTimeoutError`` a MagicMock, but box/lager/debug/gdb.py
catches that name -- so whichever module sorts first decides whether
``except GdbTimeoutError`` works or dies with "catching classes that do not
inherit from BaseException".

This file removes both orderings from the equation: box/ goes to the FRONT of
sys.path, the two genuinely-absent third-party modules get stubs with the
shapes the code under test requires, the real ``lager`` is imported exactly
once, and we assert it is a real on-disk package. Every placeholder site in the
test modules is guarded by ``if ... in sys.modules``, so they all degrade to
no-ops once this has run.
"""

from __future__ import annotations

import os
import sys
import types
from unittest.mock import MagicMock

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
BOX_DIR = os.path.join(REPO_ROOT, "box")

# FRONT of sys.path, not the back. A directory literally named `lager` that
# precedes box/ wins as a genuine namespace package (__file__ is None), and
# every `from lager import X` then fails with the "(unknown location)" error
# above. Remove-then-insert so a pre-existing later entry cannot beat us.
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

    MUST be a real exception class, not a MagicMock attribute:
    box/lager/debug/gdb.py catches ``(GdbTimeoutError,
    DebuggerNotConnectedError)``, and catching a non-exception raises
    ``TypeError: catching classes that do not inherit from BaseException``.
    """


# The only two third-party imports reachable from box/lager that are neither
# guarded by try/except nor provided by test/requirements-unit.txt:
#     flask_socketio -> box_http_server.py, http_handlers/{uart,supply,battery}.py
#     pygdbmi        -> debug/gdb.py, hence all of lager.debug
# Everything else is either a declared unit-test dep (numpy, simplejson, pyvisa,
# pyserial, pyusb, flask, cryptography, pexpect), a lager-cli dep (requests,
# PyYAML, click), or already behind a guarded import (bleak, joulescope,
# yoctopuce, ppk2_api, labjack, phidget22, brainstem, pyftdi, uldaq, aardvark).
# Deliberately NOT added to requirements-unit.txt: that file documents the
# mock-don't-install policy, and bleak in particular drags in a dbus stack.
_stub_module("flask_socketio")
_stub_module("pygdbmi")
_gdbcontroller = _stub_module("pygdbmi.gdbcontroller")
_gdbcontroller.GdbController = object  # type: ignore[attr-defined]  # patched per-test
_constants = _stub_module("pygdbmi.constants")
_constants.GdbTimeoutError = GdbTimeoutError  # type: ignore[attr-defined]

import lager  # noqa: E402

# A namespace package has __file__ is None. Fail loudly, once, here -- rather
# than letting twenty modules degrade into "(unknown location)" collection
# errors that read like a missing dependency.
_lager_file = getattr(lager, "__file__", None)
assert _lager_file is not None, (
    "`lager` resolved to a placeholder or namespace package with __path__="
    f"{list(getattr(lager, '__path__', []))!r} instead of the real package "
    f"under {BOX_DIR}.\n"
    "Either something named `lager` shadows box/lager on sys.path, or another "
    "suite's conftest registered a stub `lager` first -- test/unit/box/ cannot "
    "share a pytest process with test/unit/measurement/ (see this file's "
    "docstring). Run it as its own pytest invocation."
)
assert os.path.realpath(_lager_file).startswith(os.path.realpath(BOX_DIR)), (
    f"`lager` resolved to {_lager_file}, which is not the in-repo box "
    f"package under {BOX_DIR}."
)
