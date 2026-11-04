# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

"""The engine views golden (test/mcp/fixtures/engine-views-golden.json) is current.

A host checks its own views, or a deployed engine service, against this
file, so a change to ``engine.fleet`` that is not regenerated here would
let the two drift without anyone noticing. Regenerate with
``python tools/gen_engine_golden.py``.
"""

import importlib.util
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
GEN = ROOT / "tools" / "gen_engine_golden.py"


def _generator():
    spec = importlib.util.spec_from_file_location("gen_engine_golden", GEN)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_golden_is_current():
    gen = _generator()
    on_disk = Path(gen.GOLDEN).read_text(encoding="utf-8")
    assert on_disk == gen.render(gen.build()), "stale: run python tools/gen_engine_golden.py"


def test_golden_uses_placeholders_for_every_replaceable_sentence():
    doc = json.loads(Path(_generator().GOLDEN).read_text(encoding="utf-8"))
    text = json.dumps(doc)
    for key in doc["message_keys"]:
        assert "{{message:%s}}" % key in text, key
