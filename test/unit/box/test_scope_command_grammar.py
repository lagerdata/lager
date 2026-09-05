# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

"""
The in-browser scope CLI and the box handler must share one command vocabulary.

The point of these tests is to make drift impossible to merge. The web UI's
grammar lives in JavaScript (``static/scope/commands.js``) and the handler
lives in Python (``http_handlers/net_command.py``), so nothing in either
language forces them to agree -- a renamed action would leave the UI sending a
command the box rejects, and no unit test of either half would notice.

So these tests run the real JavaScript grammar under node, take the actions it
produces, and drive the real Python handler with them.
"""
from __future__ import annotations

import json
import pathlib
import shutil
import subprocess

import pytest

REPO_ROOT = pathlib.Path(__file__).resolve().parents[3]
GRAMMAR_JS = REPO_ROOT / "box" / "lager" / "static" / "scope" / "commands.js"

# One line per verb the UI offers, in the spelling a user would type. Every
# one of these must parse in JS and be accepted by the Python handler.
COMMAND_LINES = [
    "enable",
    "disable",
    "start",
    "start single",
    "stop",
    "force",
    "scale 0.5",
    "scale",
    "timebase 1e-3",
    "timebase",
    "coupling dc",
    "coupling",
    "probe 10",
    "probe",
    "offset 0.1",
    "offset",
    "measure vpp",
    "measure vmax",
    "measure vmin",
    "measure vrms",
    "measure vavg",
    "measure freq",
    "measure period",
    "measure duty-pos",
    "measure duty-neg",
    "measure width-pos",
    "measure width-neg",
    "measure rise",
    "measure fall",
    "trigger level 1.2 slope rising",
    "trigger edge level 0 source A",
    "capabilities",
    "autoscale",
]

pytestmark = pytest.mark.skipif(
    shutil.which("node") is None,
    reason="node is required to run the browser command grammar")


def _parse_with_node(lines):
    """Parse each line with the real JS grammar, returning {line: {...}}."""
    # Input arrives through the environment rather than argv: under
    # `node -e` the argv layout differs from a script invocation, and an
    # env var sidesteps quoting entirely.
    script = """
    import { parse } from %s;
    const lines = JSON.parse(process.env.SCOPE_TEST_LINES);
    const out = {};
    for (const line of lines) {
      try {
        const parsed = parse(line);
        out[line] = { action: parsed.action, params: parsed.params };
      } catch (e) {
        out[line] = { error: String(e.message) };
      }
    }
    process.stdout.write(JSON.stringify(out));
    """ % json.dumps(str(GRAMMAR_JS))

    import os
    env = dict(os.environ, SCOPE_TEST_LINES=json.dumps(lines))
    result = subprocess.run(
        ["node", "--input-type=module", "-e", script],
        capture_output=True, text=True, timeout=60, check=False, env=env)
    if result.returncode != 0:
        pytest.fail("node failed to run the grammar: %s" % result.stderr.strip())
    return json.loads(result.stdout)


@pytest.fixture(scope="module")
def parsed():
    return _parse_with_node(COMMAND_LINES)


def test_every_documented_command_parses(parsed):
    failures = {line: r["error"] for line, r in parsed.items() if "error" in r}
    assert not failures, "grammar rejected its own commands: %s" % failures


def test_actions_are_accepted_by_the_box_handler(parsed):
    """Drive the Python handler with the actions the browser produces.

    A mock device stands in for the hardware; what is under test is whether
    the handler recognizes the action and reads the parameters the UI sent,
    not what the scope does with them.
    """
    from lager.http_handlers import net_command

    unknown = []
    missing_params = []

    for line, result in parsed.items():
        device = _MockScope()
        try:
            _invoke_scope(net_command, device, result["action"], result["params"])
        except net_command.UnknownAction:
            unknown.append((line, result["action"]))
        except KeyError as e:
            # The handler raises KeyError naming a parameter it required but
            # did not receive -- exactly the drift this test exists to catch.
            missing_params.append((line, result["action"], str(e)))

    assert not unknown, "handler does not implement actions the UI sends: %s" % unknown
    assert not missing_params, (
        "UI sends different parameter names than the handler reads: %s" % missing_params)


def test_measure_actions_cover_the_daemon_measurements(parsed):
    """Every measurement the UI offers must be one the handler maps."""
    from lager.http_handlers import net_command

    ui_measurements = {
        r["action"] for r in parsed.values()
        if "action" in r and r["action"].startswith("measure_")
    }
    handler_measurements = set(net_command._SCOPE_MEASUREMENTS)

    assert ui_measurements <= handler_measurements, (
        "UI offers measurements the handler cannot perform: %s"
        % (ui_measurements - handler_measurements))


def test_unknown_command_is_rejected_with_a_helpful_message():
    result = _parse_with_node(["frobnicate", "measure nonsense", "scale abc"])

    assert "unknown command" in result["frobnicate"]["error"]
    # The message should list the alternatives rather than just refusing.
    assert "vpp" in result["measure nonsense"]["error"]
    assert "number" in result["scale abc"]["error"]


def _invoke_scope(net_command, device, action, params):
    """Call the scope handler with a mock device in place of the hardware."""
    original = net_command._proxy
    net_command._proxy = lambda *a, **k: device
    try:
        return net_command._scope("scope1", "scope", action, params)
    finally:
        net_command._proxy = original


class _MockScope:
    """Accepts any driver call and returns a plausible value.

    Returns 0.0 for reads so the handler's float() conversions succeed; the
    values are irrelevant to what these tests check.
    """

    def __getattr__(self, name):
        def call(*_args, **_kwargs):
            if name == "capabilities":
                return {"model": "MOCK-2204A", "analog_channels": 2}
            if name.startswith("get_") or name.startswith("measure"):
                return 0.0
            return {"status": "ok"}
        return call
