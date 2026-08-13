# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

"""Every cli/impl module must import without the box tree present.

These modules are uploaded to the box and executed there, but they also ship
inside the lager-cli wheel -- get_impl_path() resolves them from the installed
package on disk, so they are importable modules on any host that pip-installed
the CLI. The `lager` package lives under box/ and is NOT in the wheel, so a
module-level `import lager` makes them fail on every pip install.

The packaging gate catches this against a built wheel, but only there, and only
once the wheel is built. This pins the same property in the unit suite, where
it is cheap and where the failure names the offending module directly.

The suites normally run with box/ on PYTHONPATH (see the PYTHONPATH convention
in unit-tests.yml), which would hide the defect -- so each import here runs in a
subprocess with box/ stripped from the path and `lager` poisoned.
"""
import pathlib
import subprocess
import sys

import pytest

REPO_ROOT = pathlib.Path(__file__).resolve().parents[3]
IMPL_ROOT = REPO_ROOT / "cli" / "impl"


def _impl_modules():
    """Every cli.impl.* module, derived from disk rather than hardcoded."""
    for path in sorted(IMPL_ROOT.rglob("*.py")):
        rel = path.relative_to(REPO_ROOT).with_suffix("")
        yield ".".join(rel.parts)


# A guard against this test silently covering nothing if the tree moves.
def test_impl_tree_is_discoverable():
    modules = list(_impl_modules())
    assert "cli.impl.box_config" in modules
    assert "cli.impl.power.enable_disable" in modules
    assert len(modules) >= 5


@pytest.mark.parametrize("module", sorted(_impl_modules()))
def test_imports_without_box_tree(module):
    """Import the module with box/ off sys.path and `lager` unimportable."""
    box_dir = str(REPO_ROOT / "box")
    program = (
        "import sys\n"
        # Drop box/ however it arrived -- PYTHONPATH, conftest, or .pth file.
        f"sys.path = [p for p in sys.path if p not in ({box_dir!r}, '')]\n"
        # Poison the name too, so a stray install of a package called `lager`
        # cannot make a module-level import look fine.
        "import importlib.abc, importlib.machinery\n"
        "class _Block(importlib.abc.MetaPathFinder):\n"
        "    def find_spec(self, fullname, path=None, target=None):\n"
        "        if fullname == 'lager' or fullname.startswith('lager.'):\n"
        "            raise ImportError(f'blocked box-tree import: {fullname}')\n"
        "        return None\n"
        "sys.meta_path.insert(0, _Block())\n"
        f"import {module}\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", program],
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, (
        f"{module} does not import without the box tree.\n"
        f"Move the `lager` import inside the function that needs it "
        f"(see cli/impl/measurement/scope.py).\n\n{result.stderr}"
    )
