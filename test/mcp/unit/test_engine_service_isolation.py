# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

"""The engine service imports nothing box-side, and its dependencies agree with the box's.

The engine image ships ``lager/mcp`` under an EMPTY ``lager/__init__.py``,
because the real one imports every driver. So the service must import in a
fresh interpreter that has only that stub package, and it must not load the
MCP SDK, the MCP server, its tools and resources, the live bench state or a
driver. And the image's requirements must stay inside the bounds the box's
MCP SDK accepts, or the two would run different pydantic majors.
"""

import re
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
MCP_SRC = ROOT / "box" / "lager" / "mcp"
ENGINE_REQS = ROOT / "box" / "lager" / "docker" / "requirements-engine.txt"
DOCKERFILE = ROOT / "box" / "lager" / "docker" / "engine.Dockerfile"

PROBE = r"""
import sys
sys.path.insert(0, sys.argv[1])
import lager.mcp.engine_service.app
forbidden = [
    m for m in sys.modules
    if m == "mcp" or m.startswith("mcp.")
    or m.startswith(("lager.mcp.server", "lager.mcp.tools", "lager.mcp.resources", "lager.mcp.data"))
    or m in ("lager.mcp.engine.instruments", "lager.mcp.engine.bench_loader")
    or m.startswith(("lager.nets", "lager.power", "lager.automation", "lager.debug", "lager.http_handlers"))
]
print("FORBIDDEN:" + ",".join(sorted(forbidden)))
"""


def test_service_imports_under_a_stub_package(tmp_path):
    pkg = tmp_path / "lager"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("")
    shutil.copytree(MCP_SRC, pkg / "mcp", ignore=shutil.ignore_patterns("__pycache__"))
    result = subprocess.run(
        [sys.executable, "-I", "-c", PROBE, str(tmp_path)],
        capture_output=True, text=True, timeout=60, check=False,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "FORBIDDEN:", result.stdout


def _bounds(path):
    out = {}
    for line in path.read_text().splitlines():
        line = line.split("#", 1)[0].strip()
        if not line:
            continue
        name = re.split(r"[<>=!~;\s\[]", line, 1)[0].lower()
        out[name] = line[len(name):].strip()
    return out


def test_engine_requirements_are_pinned_both_ways():
    bounds = _bounds(ENGINE_REQS)
    assert set(bounds) == {"pydantic", "starlette", "uvicorn"}
    for name, spec in bounds.items():
        assert ">=" in spec and "<" in spec.replace("<=", ""), f"{name} needs a floor and a ceiling: {spec}"


def test_engine_floors_sit_inside_the_mcp_sdk_floors():
    """mcp 2.x requires pydantic>=2.12, starlette>=0.48 (3.14+) and uvicorn>=0.31.1; never ask for less."""
    def floor(spec):
        m = re.search(r">=\s*([0-9][0-9.]*)", spec)
        return tuple(int(p) for p in m.group(1).split("."))

    bounds = _bounds(ENGINE_REQS)
    assert floor(bounds["pydantic"]) >= (2, 12)
    assert floor(bounds["starlette"]) >= (0, 48)
    assert floor(bounds["uvicorn"]) >= (0, 31, 1)


def test_dockerfile_installs_the_engine_requirements_and_probes_the_import():
    text = DOCKERFILE.read_text()
    assert "requirements-engine.txt" in text
    assert "import lager.mcp.engine_service" in text
    assert "lager.mcp.engine_service" in text.split("CMD", 1)[1]
