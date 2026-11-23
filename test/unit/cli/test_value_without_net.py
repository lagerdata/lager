# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0
"""
`lager dac VALUE` and `lager gpo LEVEL` with a default net set.

NET_NAME and the value are both optional positionals, so `lager dac 3.3` binds
`3.3` to NET_NAME. With a default DAC net set, that used to fail net validation
(#841); `lager gpo high` failed the same way with a default GPIO net. Both now
read a lone value as the value when a default net is set and no net of the
command's role has that name, the rule `lager dmm` applies to its function.
"""

import importlib
from unittest.mock import patch

import pytest
from click.testing import CliRunner

net_helpers = importlib.import_module("cli.core.net_helpers")


class _Obj:
    pass


def _run(module_name, command, argv, default, box_nets=()):
    mod = importlib.import_module(module_name)
    calls = []

    def fake_post(ctx, box_ip, netname, action, role=None, quiet=False, **params):
        calls.append({"netname": netname, "action": action, "params": params})
        return {"success": True, "value": 1, "message": "ok"}

    validated = []

    def fake_validate(ctx, ip, name, role):
        validated.append(name)
        return {"name": name}

    with patch.object(mod, "post_net_command", fake_post), \
         patch.object(mod, "resolve_box_locked", lambda *a, **k: "1.2.3.4"), \
         patch.object(mod, "validate_net_exists", fake_validate), \
         patch.object(mod, "get_default_net", lambda ctx, t: default), \
         patch.object(net_helpers, "fetch_nets", lambda ip: list(box_nets)):
        result = CliRunner().invoke(getattr(mod, command), argv, obj=_Obj(),
                                    catch_exceptions=False)
    return result, calls, validated


DAC = "cli.commands.measurement.dac"
GPO = "cli.commands.measurement.gpo"


class TestDac:

    def test_lone_number_is_the_voltage_with_a_default_net(self):
        result, calls, validated = _run(DAC, "dac", ["3.3"], default="OUT")
        assert result.exit_code == 0, result.output
        assert validated == ["OUT"]
        assert calls[0]["netname"] == "OUT"
        assert calls[0]["params"] == {"value": 3.3}

    def test_lone_number_without_a_default_is_still_a_net_name(self):
        result, calls, validated = _run(DAC, "dac", ["3.3"], default=None)
        assert validated == ["3.3"]
        assert calls[0]["netname"] == "3.3"
        assert calls[0]["action"] != "set"

    def test_a_dac_net_named_like_a_number_still_wins(self):
        result, calls, validated = _run(DAC, "dac", ["5"], default="OUT",
                                        box_nets=[{"name": "5", "role": "dac"}])
        assert validated == ["5"]
        assert calls[0]["action"] != "set"

    def test_a_lone_net_name_reads_that_net(self):
        result, calls, validated = _run(DAC, "dac", ["OTHER"], default="OUT")
        assert validated == ["OTHER"]
        assert calls[0]["netname"] == "OTHER"

    def test_net_and_voltage_are_unchanged(self):
        result, calls, validated = _run(DAC, "dac", ["OTHER", "1.5"], default="OUT")
        assert validated == ["OTHER"]
        assert calls[0]["params"] == {"value": 1.5}


class TestGpo:

    @pytest.mark.parametrize("level", ["high", "HIGH", "0", "toggle"])
    def test_lone_level_is_the_level_with_a_default_net(self, level):
        result, calls, validated = _run(GPO, "gpo", [level], default="LED")
        assert result.exit_code == 0, result.output
        assert validated == ["LED"]
        assert calls[0]["netname"] == "LED"
        assert calls[0]["params"] == {"level": level.lower()}

    def test_lone_level_without_a_default_is_still_a_net_name(self):
        result, calls, validated = _run(GPO, "gpo", ["high"], default=None)
        assert validated == ["high"]
        assert "LEVEL argument required" in result.output
        assert calls == []

    def test_a_gpio_net_named_like_a_level_still_wins(self):
        result, calls, validated = _run(GPO, "gpo", ["on"], default="LED",
                                        box_nets=[{"name": "on", "role": "gpio"}])
        assert validated == ["on"]
        assert calls == []
