# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

"""
conftest.py — bootstrap the lager package for unit tests without
installing the full dependency tree (simplejson, pyvisa, etc.).

We import lager.exceptions and the dispatcher/measurement subpackages
*without* triggering lager/__init__.py (which pulls in nets → requests →
simplejson and many other heavy deps).
"""

import importlib
import importlib.util
import os
import sys
import types
from unittest.mock import MagicMock

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
BOX_DIR = os.path.join(REPO_ROOT, "box")

if BOX_DIR not in sys.path:
    sys.path.insert(0, BOX_DIR)


def _ensure_package(dotted: str) -> types.ModuleType:
    """Register a bare package (directory with __init__) in sys.modules."""
    if dotted in sys.modules:
        return sys.modules[dotted]
    mod = types.ModuleType(dotted)
    parts = dotted.split(".")
    mod.__path__ = [os.path.join(BOX_DIR, *parts)]
    mod.__package__ = dotted
    sys.modules[dotted] = mod
    return mod


def _load_module(dotted: str, filepath: str) -> types.ModuleType:
    """Load a single .py file and register it in sys.modules."""
    if dotted in sys.modules:
        return sys.modules[dotted]
    spec = importlib.util.spec_from_file_location(dotted, filepath)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[dotted] = mod
    spec.loader.exec_module(mod)
    return mod


# 1. Stub the top-level lager package so its __init__.py is NOT executed.
_ensure_package("lager")

# 2. Load lager.exceptions (standalone, no transitive deps).
_load_module(
    "lager.exceptions",
    os.path.join(BOX_DIR, "lager", "exceptions.py"),
)

# 3. Register subpackages as bare namespaces.
for pkg in [
    "lager.dispatchers",
    "lager.measurement",
    "lager.measurement.watt",
    "lager.measurement.energy_analyzer",
    "lager.measurement.thermocouple",
]:
    _ensure_package(pkg)

# 4. Load dispatcher helpers and base (needed by watt/energy dispatchers).
_load_module(
    "lager.dispatchers.helpers",
    os.path.join(BOX_DIR, "lager", "dispatchers", "helpers.py"),
)
_load_module(
    "lager.dispatchers.base",
    os.path.join(BOX_DIR, "lager", "dispatchers", "base.py"),
)
# Re-export BaseDispatcher from the dispatchers package so
# `from lager.dispatchers import BaseDispatcher` works.
sys.modules["lager.dispatchers"].BaseDispatcher = sys.modules[
    "lager.dispatchers.base"
].BaseDispatcher

# 5. Load the watt meter base and the yocto stub (imported by dispatcher).
_load_module(
    "lager.measurement.watt.watt_net",
    os.path.join(BOX_DIR, "lager", "measurement", "watt", "watt_net.py"),
)

# YoctoWatt and JoulescopeJS220 are imported by the watt dispatcher.
# Mock them so we don't need yoctopuce/joulescope libs.
for mod_name in [
    "lager.measurement.watt.yocto_watt",
    "lager.measurement.watt.joulescope_js220",
]:
    if mod_name not in sys.modules:
        mock_mod = MagicMock()
        # Give them a class with the right name
        cls_name = mod_name.rsplit(".", 1)[-1]
        setattr(mock_mod, {
            "yocto_watt": "YoctoWatt",
            "joulescope_js220": "JoulescopeJS220",
        }.get(cls_name, cls_name), type(cls_name, (), {}))
        sys.modules[mod_name] = mock_mod

# Re-create YoctoWatt/JoulescopeJS220 as proper classes for identity checks.
class _YoctoWatt:
    pass

class _JoulescopeJS220:
    pass

sys.modules["lager.measurement.watt.yocto_watt"].YoctoWatt = _YoctoWatt
sys.modules["lager.measurement.watt.joulescope_js220"].JoulescopeJS220 = _JoulescopeJS220

# 6. Mock ppk2_api (hardware library).
sys.modules.setdefault("ppk2_api", MagicMock())
sys.modules.setdefault("ppk2_api.ppk2_api", MagicMock())

# 7. Load PPK2 watt driver.
_load_module(
    "lager.measurement.watt.ppk2_watt",
    os.path.join(BOX_DIR, "lager", "measurement", "watt", "ppk2_watt.py"),
)

# 8. Load energy analyzer base.
_load_module(
    "lager.measurement.energy_analyzer.energy_analyzer_net",
    os.path.join(BOX_DIR, "lager", "measurement", "energy_analyzer", "energy_analyzer_net.py"),
)

# Mock Joulescope energy analyzer (imported by energy dispatcher).
class _JoulescopeEnergyAnalyzer:
    pass

_mock_je = MagicMock()
_mock_je.JoulescopeEnergyAnalyzer = _JoulescopeEnergyAnalyzer
sys.modules["lager.measurement.energy_analyzer.joulescope_energy"] = _mock_je

# 9. Load PPK2 energy analyzer.
_load_module(
    "lager.measurement.energy_analyzer.ppk2_energy",
    os.path.join(BOX_DIR, "lager", "measurement", "energy_analyzer", "ppk2_energy.py"),
)

# 10. Load dispatchers (watt, energy).
_load_module(
    "lager.measurement.watt.dispatcher",
    os.path.join(BOX_DIR, "lager", "measurement", "watt", "dispatcher.py"),
)
_load_module(
    "lager.measurement.energy_analyzer.dispatcher",
    os.path.join(BOX_DIR, "lager", "measurement", "energy_analyzer", "dispatcher.py"),
)
