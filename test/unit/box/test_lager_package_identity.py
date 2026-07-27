# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0
"""Guards the invariant that conftest.py establishes for this whole suite.

Twelve modules here register a placeholder `lager` in sys.modules so they can
load individual box files by path without executing the heavy package
__init__. Twenty-two module-level `from lager ... import` statements need that
__init__ to have run. conftest.py reconciles them by importing the real package
once, before any test module loads.

Every placeholder site is guarded by `if <name> in sys.modules`, so they are
inert -- but invisibly so. If conftest.py is edited, or a new module installs a
placeholder unguarded, the suite reverts to depending on alphabetical
collection order and starts failing somewhere far from the cause with:

    ImportError: cannot import name 'DeviceNotFoundError' from 'lager'
                 (unknown location)

These tests turn that into one named failure that says what happened.
"""

import os
import sys

import pytest


def test_lager_is_a_real_on_disk_package():
    """`lager` must be the real package, not a placeholder ModuleType."""
    import lager

    assert getattr(lager, "__file__", None) is not None, (
        "`lager` is a placeholder module (no __file__). Something registered a "
        "stub before test/unit/box/conftest.py could import the real package."
    )

    box_dir = os.path.join(
        os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(
            os.path.abspath(__file__))))),
        "box",
    )
    assert os.path.realpath(lager.__file__).startswith(os.path.realpath(box_dir)), (
        f"`lager` resolved to {lager.__file__}, outside the in-repo {box_dir}. "
        "A directory named `lager` is shadowing box/lager on sys.path."
    )


def test_lager_exposes_its_top_level_names():
    """The package __init__ must actually have executed.

    A placeholder with a correct __path__ still imports and still resolves
    submodules -- what it lacks is the top-level names bound by __init__. That
    is the exact shape that produces "(unknown location)", so check for the
    names rather than just for importability.
    """
    import lager

    for name in ("Net", "NetType", "DeviceNotFoundError"):
        assert hasattr(lager, name), (
            f"`lager.{name}` is missing, so lager/__init__.py never executed. "
            "See this file's docstring."
        )


@pytest.mark.parametrize("dotted", ["lager.constants", "lager.nets.net"])
def test_lager_submodules_resolve(dotted):
    """Submodule imports must reach real modules, not empty placeholders.

    An empty `__path__ = []` on a placeholder makes every submodule
    unfindable -- a bug that shipped in this suite once already.
    """
    __import__(dotted)
    assert getattr(sys.modules[dotted], "__file__", None) is not None, (
        f"{dotted} resolved to a placeholder with no __file__."
    )
