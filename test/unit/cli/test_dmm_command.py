# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0
"""Unit tests for `lager dmm` (cli/commands/measurement/dmm.py).

The box is mocked at the ``post_net_command`` boundary: these tests check the
action and params the CLI sends and how it formats the reply.
"""

from __future__ import annotations

import importlib
import json
import os
import sys
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(__file__)))))

dmm_mod = importlib.import_module("cli.commands.measurement.dmm")
cli = importlib.import_module("cli.main").cli


def _run(args, reply=None, default_net=None, saved_nets=()):
    """Invoke `lager dmm` with the box mocked; returns (result, calls, display_mock)."""
    calls: list[dict] = []

    def fake_post(ctx, box_ip, netname, action, role=None, quiet=False,
                  http_timeout=None, **params):
        calls.append({"netname": netname, "action": action, "role": role,
                      "http_timeout": http_timeout, "params": params})
        return reply or {"success": True, "value": 3.3, "unit": "V",
                         "overload": False, "message": "3.3 V"}

    display_mock = MagicMock()
    with patch.object(dmm_mod, "post_net_command", fake_post), \
         patch.object(dmm_mod, "resolve_box_locked", lambda *a, **k: "1.2.3.4"), \
         patch.object(dmm_mod, "validate_net_exists",
                      lambda ctx, ip, name, role: {"name": name}), \
         patch.object(dmm_mod, "display_nets", display_mock), \
         patch.object(dmm_mod, "get_default_net", lambda ctx, t: default_net), \
         patch.object(dmm_mod, "fetch_nets", lambda ip: list(saved_nets)):
        result = CliRunner().invoke(dmm_mod.dmm, args, catch_exceptions=False)
    return result, calls, display_mock


def test_default_reads_dc_volts():
    result, calls, _ = _run(["dmm1"])
    assert result.exit_code == 0, result.output
    assert calls[0]["action"] == "measure"
    assert calls[0]["role"] == "dmm"
    assert calls[0]["params"] == {"function": "dcv", "range": None, "nplc": None, "sensor": None}
    assert "3.3 V" in result.output


def test_options_are_forwarded():
    result, calls, _ = _run(["dmm1", "RES", "--range", "2e3", "--nplc", "100"])
    assert result.exit_code == 0, result.output
    assert calls[0]["params"] == {"function": "res", "range": 2000.0, "nplc": 100.0,
                                  "sensor": None}
    # Must outlast the box's 60 s budget for a slow reading.
    assert calls[0]["http_timeout"] > 60


def test_auto_range_and_sensor():
    _, calls, _ = _run(["dmm1", "temp", "--range", "auto", "--sensor", "rtd:pt100"])
    assert calls[0]["params"]["range"] is None
    assert calls[0]["params"]["sensor"] == "rtd:pt100"


def test_bad_range_is_rejected_locally():
    result, calls, _ = _run(["dmm1", "--range", "lots"])
    assert result.exit_code != 0
    assert "not a number or 'auto'" in result.output
    assert calls == []


def test_unknown_function_is_rejected_locally():
    result, calls, _ = _run(["dmm1", "ohms"])
    assert result.exit_code != 0
    assert calls == []


def test_json():
    result, _, _ = _run(["dmm1", "freq", "--json"],
                        reply={"success": True, "value": 1000.0, "unit": "Hz",
                               "overload": False, "message": "1000.0 Hz"})
    assert json.loads(result.output) == {"netname": "dmm1", "function": "freq",
                                         "value": 1000.0, "unit": "Hz", "overload": False}


@pytest.mark.parametrize("as_json", [False, True])
def test_overload(as_json):
    reply = {"success": True, "unit": "ohm", "overload": True, "message": "OPEN"}
    result, _, _ = _run(["dmm1", "cont", *(["--json"] if as_json else [])], reply=reply)
    if as_json:
        assert json.loads(result.output)["value"] is None
    else:
        assert "OPEN" in result.output


@pytest.mark.parametrize("flag, action, key", [("--errors", "errors", "errors"),
                                              ("--idn", "identify", "idn")])
def test_errors_and_idn(flag, action, key):
    reply = {"success": True, "value": ["-113, Undefined header"], "message": "-113"}
    result, calls, _ = _run(["dmm1", flag, "--json"], reply=reply)
    assert calls[0]["action"] == action
    assert json.loads(result.output)[key] == ["-113, Undefined header"]


def test_errors_and_idn_are_exclusive():
    result, calls, _ = _run(["dmm1", "--errors", "--idn"])
    assert result.exit_code != 0
    assert calls == []


def test_no_net_lists_dmm_nets():
    result, calls, display = _run([])
    assert result.exit_code == 0
    assert calls == []
    assert display.call_args.args[3] == "dmm"


def test_lone_function_uses_the_default_net():
    result, calls, _ = _run(["RES"], default_net="VBAT")
    assert result.exit_code == 0, result.output
    assert calls[0]["netname"] == "VBAT"
    assert calls[0]["params"]["function"] == "res"


def test_net_named_like_a_function_wins_over_the_default():
    _, calls, _ = _run(["res"], default_net="VBAT",
                       saved_nets=[{"name": "res", "role": "dmm"}])
    assert calls[0]["netname"] == "res"
    assert calls[0]["params"]["function"] == "dcv"


def test_lone_function_without_a_default_is_a_net_name():
    _, calls, _ = _run(["res"])
    assert calls[0]["netname"] == "res"
    assert calls[0]["params"]["function"] == "dcv"


def test_registered_on_the_root_cli():
    assert cli.commands["dmm"] is dmm_mod.dmm


def test_functions_match_the_box():
    # The box's list lives in box/lager/measurement/dmm/dmm_net.py, which the
    # CLI cannot import; keep the two in step.
    path = os.path.join(os.path.dirname(__file__), "..", "..", "..",
                        "box", "lager", "measurement", "dmm", "dmm_net.py")
    with open(path, encoding="utf-8") as f:
        source = f.read()
    for function in dmm_mod.FUNCTIONS:
        assert f'"{function}"' in source
