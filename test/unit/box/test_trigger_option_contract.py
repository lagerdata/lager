# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0
"""
Every value `lager scope` and `lager logic` offer for a trigger must reach the
MSO5000 mapper as something the mapper accepts.

A trigger command crosses three layers that were written apart: the click
options in `cli/commands/measurement/{scope,logic}.py`, the shared handler in
`cli/impl/measurement/scope.py` (uploaded with the CLI and run on the box), and
the mapper in `box/lager/nets/mappers/rigol_mso5000.py`. Nothing checked that
they agree, and they did not (#498, #499, #500):

  * `lager scope trigger i2c` failed with every default, because its default
    `--direction read_write` was a value the handler refused;
  * `ack_miss`, the scope `--data-width` default, and every `--trigger-on`
    value of `lager logic trigger pulse` were refused the same way;
  * `--data` and `--address` reached an integer comparison as strings;
  * `error` called `set_trigger_on_error`, which the mapper does not define.
    The mapper forwards an unknown name to the instrument driver, so the call
    failed only on a box, as "Function not found";
  * `--clk-slope rising` was silently ignored.

These tests drive the real click commands with the box boundary mocked, pass
what the command would send into the real handler, and let the handler call
the real mapper classes over a fake device. The mapper is wrapped so that only
methods its own class defines can be called: a name it would forward to the
driver fails here instead.

`lager dac` had the same kind of mismatch: it accepted 0-10 V, which matches no
supported DAC, so a value above 5 V failed deeper in the stack. The `lager dac`
section below pins it.

What this does not show is that the MSO5000 driver implements the protocol
calls the mapper makes. Most of the pulse, UART and I2C ones are still missing
(#418); that is tracked by `mapper_undefined_baseline.txt`.
"""

import ast
import importlib
import json
import pathlib
import types
from unittest import mock

import click
import pytest
from click.testing import CliRunner

from lager.nets.defines import TriggerI2CDirection
from lager.nets.mappers import rigol_mso5000 as mapper

impl = importlib.import_module("cli.impl.measurement.scope")
scope_cli = importlib.import_module("cli.commands.measurement.scope")
logic_cli = importlib.import_module("cli.commands.measurement.logic")

REPO_ROOT = pathlib.Path(__file__).resolve().parents[3]
IMPL_PATH = REPO_ROOT / "cli" / "impl" / "measurement" / "scope.py"

HANDLERS = {
    "trigger_edge": impl.trigger_edge,
    "trigger_uart": impl.trigger_uart,
    "trigger_pulse": impl.trigger_pulse,
    "trigger_i2c": impl.trigger_i2c,
    "trigger_spi": impl.trigger_spi,
}

#: The mapper class behind each `trigger_settings.<name>` the handler uses.
SUB_MAPPERS = {
    "edge": mapper.TriggerSettingsEdge_RigolMSO5000FunctionMapper,
    "pulse": mapper.TriggerSettingsPulse_RigolMSO5000FunctionMapper,
    "uart": mapper.TriggerSettingsUART_RigolMSO5000FunctionMapper,
    "i2c": mapper.TriggerSettingsI2C_RigolMSO5000FunctionMapper,
    "spi": mapper.TriggerSettingsSPI_RigolMSO5000FunctionMapper,
}


class FakeDevice:
    """Records what the mapper forwards to the driver; answers queries with 8."""

    def __init__(self):
        self.forwarded = []

    def __getattr__(self, name):
        def record(*args, **kwargs):
            self.forwarded.append((name, args, kwargs))
            return 8
        return record


class MapperOnly:
    """Lets through only the methods the wrapped mapper's class defines."""

    def __init__(self, inner, label, calls):
        self.__dict__.update(_inner=inner, _label=label, _calls=calls)

    def __getattr__(self, name):
        if not callable(getattr(type(self._inner), name, None)):
            raise AssertionError(
                f"{type(self._inner).__name__} defines no {name}(). The mapper "
                f"would forward it to the instrument driver, which fails on a "
                f"box with 'Function not found'."
            )
        method = getattr(self._inner, name)

        def call(*args, **kwargs):
            self._calls.append((f"{self._label}.{name}", args, kwargs))
            return method(*args, **kwargs)
        return call


def run_handler(action, params):
    """Run the handler against real mappers. Returns (mapper calls, device)."""
    device = FakeDevice()
    settings = mapper.TriggerSettings_RigolMSO5000FunctionMapper(None, None, device)
    calls = []
    top = MapperOnly(settings, "trigger_settings", calls)
    for name in SUB_MAPPERS:
        top.__dict__[name] = MapperOnly(getattr(settings, name), name, calls)
    net = types.SimpleNamespace(enable=lambda: None, trigger_settings=top)
    with mock.patch.object(impl, "get_rigol_net", return_value=net), \
            mock.patch.object(impl, "get_source_net", return_value=None):
        HANDLERS[action](**params)
    return calls, device


class _Obj:
    """Settable stand-in for the LagerContext; the group stores `netname` on it."""


def cli_params(group, argv):
    """Invoke `lager <group> net1 trigger <argv...>`; return (action, params)."""
    sent = []

    def fake_backend(ctx, box, script, action, **params):
        assert script == "scope.py"
        params.pop("mcu", None)
        params.pop("role", None)
        sent.append((action, params))

    def fake_run_python(ctx, path, box, env, **kwargs):
        data = json.loads(env[0].split("=", 1)[1])
        sent.append((data["action"], dict(data["params"])))

    module = scope_cli if group == "scope" else logic_cli
    validator = "_validate_scope_net" if group == "scope" else "_validate_logic_net"
    with mock.patch.object(module, "_resolve_box", return_value="192.0.2.10"), \
            mock.patch.object(module, "_require_netname", return_value="net1"), \
            mock.patch.object(module, validator, return_value={"name": "net1"}), \
            mock.patch.object(module, "run_backend", fake_backend), \
            mock.patch.object(scope_cli, "run_python_internal", fake_run_python):
        result = CliRunner().invoke(getattr(module, group), ["net1", "trigger", *argv],
                                    obj=_Obj(), catch_exceptions=False)
    assert result.exit_code == 0, result.output
    assert len(sent) == 1, sent
    return sent[0]


def call_names(calls):
    return [name for name, _, _ in calls]


# ---------------------------------------------------------------------------
# Every offered value, generated from the click options themselves
# ---------------------------------------------------------------------------

def _trigger_commands(group):
    module = scope_cli if group == "scope" else logic_cli
    trigger_group = getattr(module, group).commands["trigger"]
    return trigger_group.commands


def _companions(sub, option, value):
    """Arguments a value needs before the handler has anything to act on."""
    extra = []
    if sub == "pulse":
        extra += ["--upper", "0.002", "--lower", "0.001"]
    if sub == "i2c":
        condition = value if option == "trigger_on" else "addr_data"
        if option != "trigger_on":
            extra += ["--trigger-on", "addr_data"]
        if condition in ("address", "addr_data"):
            extra += ["--address", "10"]
        if condition in ("data", "addr_data"):
            extra += ["--data", "10"]
    if sub == "uart" and option == "trigger_on" and value == "data":
        extra += ["--data", "10"]
    if sub == "spi":
        extra += ["--data", "10", "--timeout", "0.5"]
    return extra


def _cases():
    cases = []
    for group in ("scope", "logic"):
        for sub, command in sorted(_trigger_commands(group).items()):
            cases.append(pytest.param(group, sub, [], id=f"{group}-{sub}-defaults"))
            for param in command.params:
                if not isinstance(param.type, click.Choice):
                    continue
                flag = param.opts[0]
                for value in param.type.choices:
                    argv = [flag, value, *_companions(sub, param.name, value)]
                    cases.append(pytest.param(
                        group, sub, argv, id=f"{group}-{sub}-{flag[2:]}={value}"))
    return cases


CASES = _cases()


def test_the_generator_found_the_whole_surface():
    """Guard the guard: a missed group would leave the test below vacuous."""
    for group in ("scope", "logic"):
        assert set(_trigger_commands(group)) == {"edge", "uart", "pulse", "i2c", "spi"}, group
    assert len(CASES) >= 80, len(CASES)


@pytest.mark.parametrize("group, sub, argv", CASES)
def test_every_offered_value_reaches_the_mapper(group, sub, argv):
    action, params = cli_params(group, [sub, *argv])
    run_handler(action, params)


# ---------------------------------------------------------------------------
# The specific mismatches, pinned by what reaches the mapper
# ---------------------------------------------------------------------------

def test_scope_i2c_with_its_defaults_triggers_on_start():
    calls, _ = run_handler(*cli_params("scope", ["i2c"]))
    assert "i2c.set_trigger_on_start" in call_names(calls)


def test_scope_i2c_read_write_and_ack_miss_mean_rw_and_nack():
    calls, _ = run_handler(*cli_params("scope", ["i2c", "--trigger-on", "ack_miss"]))
    assert "i2c.set_trigger_on_nack" in call_names(calls)

    calls, _ = run_handler(*cli_params(
        "scope", ["i2c", "--trigger-on", "address", "--address", "0x50"]))
    (_, _, kwargs), = [c for c in calls if c[0] == "i2c.set_trigger_on_address"]
    assert kwargs == {"bits": 7, "direction": TriggerI2CDirection.RW, "address": 0x50}


def test_scope_hex_values_reach_the_mapper_as_integers():
    _, device = run_handler(*cli_params(
        "scope", ["i2c", "--trigger-on", "addr_data", "--address", "0x3ff",
                  "--addr-width", "10", "--data", "beef", "--data-width", "2"]))
    assert ("set_trigger_i2c_address", (0x3FF,), {}) in device.forwarded
    assert ("set_trigger_i2c_data", (0xBEEF,), {}) in device.forwarded

    _, device = run_handler(*cli_params("scope", ["spi", "--data", "0xA5"]))
    assert ("set_trigger_spi_data", (0xA5,), {}) in device.forwarded


def test_scope_i2c_data_width_is_bytes():
    _, params = cli_params("scope", ["i2c"])
    assert params["data_width"] == 1
    result = CliRunner().invoke(scope_cli.scope, ["net1", "trigger", "i2c", "--data-width", "8"],
                                obj=_Obj())
    assert result.exit_code == 2
    assert "1<=x<=5" in result.output


@pytest.mark.parametrize("value, method", [
    ("start", "set_trigger_on_start"),
    ("error", "set_trigger_on_frame_error"),
    ("cerror", "set_trigger_on_check_error"),
])
@pytest.mark.parametrize("group", ["scope", "logic"])
def test_uart_conditions_use_the_mapper_names(group, value, method):
    calls, _ = run_handler(*cli_params(group, ["uart", "--trigger-on", value]))
    assert f"uart.{method}" in call_names(calls)


def test_scope_uart_no_longer_offers_stop():
    """The MSO5000 has no stop condition for a UART trigger."""
    options = {p.name: p for p in _trigger_commands("scope")["uart"].params}
    assert "stop" not in options["trigger_on"].type.choices


@pytest.mark.parametrize("value, method", [
    ("rising", "set_clk_edge_positive"),
    ("falling", "set_clk_edge_negative"),
])
def test_scope_spi_clock_edge_is_applied(value, method):
    calls, _ = run_handler(*cli_params("scope", ["spi", "--clk-slope", value]))
    assert f"spi.{method}" in call_names(calls)


def test_an_unknown_spi_clock_edge_is_refused():
    with pytest.raises(Exception, match="not a valid option"):
        run_handler("trigger_spi", dict(
            netname="net1", mode="normal", coupling="dc", source_mosi_miso=None,
            source_sck=None, source_cs=None, level_mosi_miso=None, level_sck=None,
            level_cs=None, data=None, data_width=8, clk_slope="sideways",
            trigger_on="cs", cs_idle="high", timeout=None))


@pytest.mark.parametrize("value, method, kwargs", [
    ("gt", "set_trigger_on_pulse_greater_than_width", None),
    ("lt", "set_trigger_on_pulse_less_than_width", None),
    ("gtlt", "set_trigger_on_pulse_less_than_greater_than",
     {"max_pulse_width": 0.002, "min_pulse_width": 0.001}),
])
def test_logic_pulse_conditions_reach_the_mapper(value, method, kwargs):
    calls, _ = run_handler(*cli_params(
        "logic", ["pulse", "--trigger-on", value, "--upper", "0.002", "--lower", "0.001"]))
    matching = [c for c in calls if c[0] == f"pulse.{method}"]
    assert len(matching) == 1, call_names(calls)
    if kwargs is not None:
        assert matching[0][2] == kwargs


def test_a_pulse_condition_without_its_width_changes_nothing_and_says_so(capsys):
    calls, _ = run_handler(*cli_params("scope", ["pulse"]))
    assert not [n for n in call_names(calls) if "pulse_" in n]
    assert "No --upper given" in capsys.readouterr().out


def test_logic_offers_only_address_widths_the_mapper_accepts():
    options = {p.name: p for p in _trigger_commands("logic")["i2c"].params}
    assert "9" not in options["addr_width"].type.choices


def test_logic_uart_refuses_a_nine_bit_data_width():
    with mock.patch.object(logic_cli, "_resolve_box", return_value="192.0.2.10"), \
            mock.patch.object(logic_cli, "run_backend") as backend:
        result = CliRunner().invoke(
            logic_cli.logic, ["net1", "trigger", "uart", "--data-width", "9"], obj=_Obj())
    assert result.exit_code == 1
    assert "between 5 and 8 bits" in result.output
    backend.assert_not_called()


# ---------------------------------------------------------------------------
# `lager dac`: the same rule for a range check (#500)
# ---------------------------------------------------------------------------

dac_cli = importlib.import_module("cli.commands.measurement.dac")


def _dac(argv):
    """Invoke `lager dac` with the box mocked; return (result, values sent)."""
    sent = []

    def fake_post(ctx, box_ip, netname, action, role=None, quiet=False, **params):
        sent.append(params.get("value"))
        return {"value": params.get("value", 0.0)}

    with mock.patch.object(dac_cli, "resolve_box_locked", return_value="192.0.2.10"), \
            mock.patch.object(dac_cli, "validate_net_exists", return_value={"name": "dac1"}), \
            mock.patch.object(dac_cli, "post_net_command", fake_post):
        result = CliRunner().invoke(dac_cli.dac, argv, obj=_Obj())
    return result, sent


def test_dac_sends_the_top_of_the_range_to_the_box():
    """5 V is the most any supported DAC outputs (LabJack T7, MCC USB-202)."""
    result, sent = _dac(["dac1", "5.0"])
    assert result.exit_code == 0, result.output
    assert sent == [5.0]


def test_dac_refuses_a_voltage_no_supported_dac_can_output():
    """The old 0-10 V check matched no instrument, so 6 V failed deeper down."""
    result, sent = _dac(["dac1", "6.0"])
    assert result.exit_code == 1
    assert "between 0.0 and 5.0 V" in result.output
    assert sent == []


# ---------------------------------------------------------------------------
# Every mapper method the handler names exists, including untested branches
# ---------------------------------------------------------------------------

def _handler_mapper_calls():
    """(sub-mapper or None, method) for every `.trigger_settings[.sub].method(`."""
    found = []
    for node in ast.walk(ast.parse(IMPL_PATH.read_text())):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)):
            continue
        owner = node.func.value
        if isinstance(owner, ast.Attribute) and owner.attr == "trigger_settings":
            found.append((None, node.func.attr))
        elif (isinstance(owner, ast.Attribute)
              and isinstance(owner.value, ast.Attribute)
              and owner.value.attr == "trigger_settings"):
            found.append((owner.attr, node.func.attr))
    return found


def test_the_walker_found_the_handler_calls():
    calls = _handler_mapper_calls()
    assert len(calls) >= 40, len(calls)
    assert {sub for sub, _ in calls if sub} == set(SUB_MAPPERS)


@pytest.mark.parametrize("sub, method", sorted(set(_handler_mapper_calls()), key=str))
def test_every_mapper_method_the_handler_calls_is_defined(sub, method):
    cls = mapper.TriggerSettings_RigolMSO5000FunctionMapper if sub is None else SUB_MAPPERS[sub]
    assert callable(getattr(cls, method, None)), (
        f"cli/impl/measurement/scope.py calls trigger_settings"
        f"{'.' + sub if sub else ''}.{method}(), which {cls.__name__} does not "
        f"define. The mapper would forward it to the driver, and it would fail "
        f"on a box with 'Function not found'."
    )
