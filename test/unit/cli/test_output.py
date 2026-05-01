# Copyright 2024-2026 Lager Data LLC
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for cli/output.py.

Covers the resolve-config policy, the JSON envelope contract, the text
state-block alignment, and the success/error/action emitters.
"""
from __future__ import annotations

import io
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(__file__)))))

from cli.output import (  # noqa: E402
    ColorPolicy,
    ExitCode,
    Field,
    Format,
    OutputConfig,
    SEVERITY_ERROR,
    SEVERITY_OK,
    SEVERITY_WARN,
    action,
    default_config,
    error,
    list_table,
    reading,
    resolve_color,
    resolve_config,
    resolve_unicode,
    state,
    success,
    tree_table,
    warn,
    TreeGroup,
)


class _Stream(io.StringIO):
    """StringIO that lets us pretend to be (or not be) a TTY."""
    def __init__(self, *, tty: bool = False, encoding: str = "utf-8") -> None:
        super().__init__()
        self._tty = tty
        self._encoding = encoding

    def isatty(self) -> bool:
        return self._tty

    @property
    def encoding(self) -> str:  # type: ignore[override]
        return self._encoding


# --------------------------------------------------------------------------- #
# resolve_color / resolve_config
# --------------------------------------------------------------------------- #

class TestResolveColor:
    def test_no_color_env_overrides_always(self):
        assert resolve_color("always", _Stream(tty=True), env={"NO_COLOR": "1"}) is False

    def test_always_with_no_tty(self):
        assert resolve_color("always", _Stream(tty=False), env={}) is True

    def test_never_with_tty(self):
        assert resolve_color("never", _Stream(tty=True), env={}) is False

    def test_auto_tty(self):
        assert resolve_color("auto", _Stream(tty=True), env={}) is True

    def test_auto_no_tty(self):
        assert resolve_color("auto", _Stream(tty=False), env={}) is False

    def test_auto_dumb_term(self):
        assert resolve_color("auto", _Stream(tty=True), env={"TERM": "dumb"}) is False

    def test_force_color_overrides_dumb_only_in_auto(self):
        # FORCE_COLOR only applies in auto mode after TERM=dumb early-exits.
        assert resolve_color("auto", _Stream(tty=True),
                             env={"TERM": "dumb", "FORCE_COLOR": "1"}) is False

    def test_force_color_in_auto_no_tty(self):
        # No TTY → still False. FORCE_COLOR doesn't override the no-tty gate.
        assert resolve_color("auto", _Stream(tty=False),
                             env={"FORCE_COLOR": "1"}) is False


class TestResolveUnicode:
    def test_utf8(self):
        assert resolve_unicode(_Stream(encoding="utf-8")) is True

    def test_ascii(self):
        assert resolve_unicode(_Stream(encoding="ascii")) is False

    def test_no_encoding(self):
        s = _Stream()
        s._encoding = ""  # type: ignore[attr-defined]
        assert resolve_unicode(s) is False


class TestResolveConfig:
    def test_text_always_color_on(self):
        cfg = resolve_config("text", "always", stream=_Stream(tty=True), env={})
        assert cfg.format is Format.TEXT
        assert cfg.color is True
        assert cfg.unicode is True

    def test_json_always_color_off(self):
        # JSON mode forces color off regardless of policy.
        cfg = resolve_config("json", "always", stream=_Stream(tty=True), env={})
        assert cfg.format is Format.JSON
        assert cfg.color is False

    def test_no_color_env_wins_in_text(self):
        cfg = resolve_config("text", "always",
                             stream=_Stream(tty=True), env={"NO_COLOR": "1"})
        assert cfg.color is False

    def test_default_config_returns_textmode(self):
        cfg = default_config()
        assert cfg.format is Format.TEXT


# --------------------------------------------------------------------------- #
# state() — the headline contract
# --------------------------------------------------------------------------- #

class TestStateText:
    def _cfg(self) -> OutputConfig:
        return OutputConfig(
            format=Format.TEXT,
            color=False,         # turn off ANSI so we can assert on plain text
            unicode=True,
            stream=_Stream(tty=True),
            err_stream=_Stream(tty=True),
        )

    def _emit(self, cfg: OutputConfig, *args, **kwargs) -> str:
        cfg = cfg.with_overrides(stream=_Stream(tty=True))
        state(*args, cfg=cfg, **kwargs)
        return cfg.stream.getvalue()

    def test_basic_alignment(self):
        out = self._emit(
            self._cfg(),
            "supply2",
            [
                Field("Output", "OFF"),
                Field("OCP", 1.1, unit="A"),
                Field("OVP", 66.0, unit="V"),
            ],
            subject={"instrument": "Rigol DP821", "channel": 1},
        )
        # Title line
        assert out.startswith("✓ supply2 (Rigol DP821, ch 1)\n")
        # Labels right-padded to the longest ("Output:" = 7 chars + 2 spaces gap)
        assert "  Output:  OFF" in out
        assert "  OCP:     1.1 A" in out
        assert "  OVP:     66.0 V" in out

    def test_composite_value_split_in_text(self):
        out = self._emit(
            self._cfg(),
            "supply2",
            [Field("Set",
                   value=(0.0, 1.0),
                   unit=("V", "A"),
                   json_subkeys=("voltage", "current"))],
        )
        assert "  Set:  0.0 V  /  1.0 A" in out

    def test_ascii_fallback_when_no_unicode(self):
        cfg = self._cfg().with_overrides(unicode=False)
        out = self._emit(cfg, "supply2", [Field("Output", "OFF")])
        assert out.startswith("[OK] supply2")

    def test_error_severity_title(self):
        out = self._emit(self._cfg(), "supply2",
                         [Field("Output", "OFF")],
                         title_severity=SEVERITY_ERROR)
        assert out.startswith("✗ supply2")


class TestStateJson:
    def _cfg(self) -> OutputConfig:
        return OutputConfig(
            format=Format.JSON,
            color=False,
            unicode=True,
            stream=_Stream(tty=False),
            err_stream=_Stream(tty=False),
        )

    def test_envelope_shape(self):
        cfg = self._cfg().with_overrides(stream=_Stream())
        state(
            "supply2",
            [Field("Output", "OFF"), Field("OCP", 1.1, unit="A")],
            cfg=cfg,
            command="supply.state",
            subject={"net": "supply2", "instrument": "Rigol DP821", "channel": 1},
        )
        env = json.loads(cfg.stream.getvalue())
        assert env["lager"] == 1
        assert env["kind"] == "state"
        assert env["status"] == "ok"
        assert env["command"] == "supply.state"
        assert env["subject"] == {"net": "supply2", "instrument": "Rigol DP821", "channel": 1}
        assert env["data"] == {"output": "OFF", "ocp": 1.1, "ocp_unit": "A"}

    def test_composite_split_in_json(self):
        cfg = self._cfg().with_overrides(stream=_Stream())
        state(
            "supply2",
            [Field("Set",
                   value=(0.0, 1.0),
                   unit=("V", "A"),
                   json_subkeys=("voltage", "current"))],
            cfg=cfg,
        )
        data = json.loads(cfg.stream.getvalue())["data"]
        assert data == {
            "set_voltage": 0.0,
            "set_voltage_unit": "V",
            "set_current": 1.0,
            "set_current_unit": "A",
        }

    def test_one_object_per_line(self):
        cfg = self._cfg().with_overrides(stream=_Stream())
        state("a", [Field("X", 1)], cfg=cfg)
        state("b", [Field("Y", 2)], cfg=cfg)
        lines = [ln for ln in cfg.stream.getvalue().splitlines() if ln]
        assert len(lines) == 2
        assert all(json.loads(ln)["lager"] == 1 for ln in lines)


# --------------------------------------------------------------------------- #
# success / warn / error / action / reading / list_table
# --------------------------------------------------------------------------- #

class TestActionAndSuccess:
    def test_text(self):
        cfg = OutputConfig(format=Format.TEXT, color=False, unicode=True,
                           stream=_Stream(), err_stream=_Stream())
        action("Enabled supply output", cfg=cfg)
        assert cfg.stream.getvalue() == "✓ Enabled supply output\n"

    def test_ascii_fallback(self):
        cfg = OutputConfig(format=Format.TEXT, color=False, unicode=False,
                           stream=_Stream(), err_stream=_Stream())
        success("Done", cfg=cfg)
        assert cfg.stream.getvalue() == "[OK] Done\n"

    def test_json(self):
        cfg = OutputConfig(format=Format.JSON, color=False, unicode=True,
                           stream=_Stream(), err_stream=_Stream())
        action("Enabled supply output", cfg=cfg, command="supply.enable",
               subject={"net": "supply2"})
        env = json.loads(cfg.stream.getvalue())
        assert env == {
            "lager": 1, "kind": "ack", "status": "ok",
            "command": "supply.enable",
            "subject": {"net": "supply2"},
            "message": "Enabled supply output",
        }


class TestError:
    def test_text_to_stderr_and_exits(self):
        cfg = OutputConfig(format=Format.TEXT, color=False, unicode=True,
                           stream=_Stream(), err_stream=_Stream())
        with pytest.raises(SystemExit) as exc:
            error("boom", cfg=cfg, exit_code=ExitCode.USER_ERROR)
        assert exc.value.code == 65
        assert cfg.err_stream.getvalue() == "✗ boom\n"
        assert cfg.stream.getvalue() == ""  # nothing on stdout in text mode

    def test_json_to_stdout_and_exits(self):
        cfg = OutputConfig(format=Format.JSON, color=False, unicode=True,
                           stream=_Stream(), err_stream=_Stream())
        with pytest.raises(SystemExit) as exc:
            error("wrong role", cfg=cfg, exit_code=ExitCode.USER_ERROR,
                  command="battery.state",
                  subject={"net": "supply2", "actual_role": "power-supply",
                           "expected_role": "battery"})
        assert exc.value.code == 65
        env = json.loads(cfg.stream.getvalue())
        assert env["kind"] == "error"
        assert env["status"] == "error"
        assert env["exit_code"] == 65
        assert env["message"] == "wrong role"
        assert env["subject"]["actual_role"] == "power-supply"

    def test_no_raise(self):
        cfg = OutputConfig(format=Format.TEXT, color=False, unicode=True,
                           stream=_Stream(), err_stream=_Stream())
        # raise_exit=False → message is emitted but no SystemExit.
        error("no-raise", cfg=cfg, exit_code=ExitCode.USER_ERROR, raise_exit=False)
        assert "no-raise" in cfg.err_stream.getvalue()


class TestColorSuppressedInJson:
    def test_no_ansi_in_json_even_with_color_always(self):
        # resolve_config should force color off for JSON mode.
        cfg = resolve_config("json", "always", stream=_Stream(tty=True), env={})
        assert cfg.color is False
        success("hi", cfg=cfg)
        # No ANSI escape sequences:
        assert "\x1b[" not in cfg.stream.getvalue()


class TestReading:
    def test_text(self):
        cfg = OutputConfig(format=Format.TEXT, color=False, unicode=True,
                           stream=_Stream(), err_stream=_Stream())
        reading("voltage", 3.3, "V", cfg=cfg)
        assert cfg.stream.getvalue() == "3.3 V\n"

    def test_json(self):
        cfg = OutputConfig(format=Format.JSON, color=False, unicode=True,
                           stream=_Stream(), err_stream=_Stream())
        reading("voltage", 3.3, "V", cfg=cfg, command="supply.voltage",
                subject={"net": "supply2"})
        env = json.loads(cfg.stream.getvalue())
        assert env["kind"] == "reading"
        assert env["data"] == {"voltage": 3.3, "voltage_unit": "V"}


class TestListTable:
    def test_text(self):
        cfg = OutputConfig(format=Format.TEXT, color=False, unicode=True,
                           stream=_Stream(), err_stream=_Stream())
        list_table(["Name", "Role"], [("supply2", "power-supply"), ("battery1", "battery")], cfg=cfg)
        out = cfg.stream.getvalue()
        assert "Name" in out and "Role" in out
        assert "supply2" in out
        assert "battery1" in out

    def test_json(self):
        cfg = OutputConfig(format=Format.JSON, color=False, unicode=True,
                           stream=_Stream(), err_stream=_Stream())
        list_table(["Name", "Role"], [("supply2", "power-supply")], cfg=cfg)
        env = json.loads(cfg.stream.getvalue())
        assert env["kind"] == "list"
        assert env["data"] == [{"name": "supply2", "role": "power-supply"}]


class TestTreeTable:
    def test_text(self):
        cfg = OutputConfig(format=Format.TEXT, color=False, unicode=True,
                           stream=_Stream(), err_stream=_Stream())
        tree_table(
            ["Name", "Role", "Channel"],
            [TreeGroup("Rigol DP821", [("supply2", "power-supply", 1),
                                        ("supply3", "power-supply", 2)])],
            cfg=cfg,
        )
        out = cfg.stream.getvalue()
        assert "Rigol DP821" in out
        assert "├── " in out and "└── " in out

    def test_json(self):
        cfg = OutputConfig(format=Format.JSON, color=False, unicode=True,
                           stream=_Stream(), err_stream=_Stream())
        tree_table(
            ["Name", "Role"],
            [TreeGroup("Rigol DP821", [("supply2", "power-supply")])],
            cfg=cfg,
        )
        env = json.loads(cfg.stream.getvalue())
        assert env["kind"] == "list"
        assert env["data"] == [{
            "title": "Rigol DP821",
            "rows": [{"name": "supply2", "role": "power-supply"}],
        }]


# --------------------------------------------------------------------------- #
# Sanity: ExitCode constants
# --------------------------------------------------------------------------- #

def test_exit_codes():
    assert ExitCode.OK == 0
    assert ExitCode.USAGE == 64
    assert ExitCode.USER_ERROR == 65
    assert ExitCode.DEVICE_NOT_FOUND == 68
    assert ExitCode.LIBRARY_MISSING == 70
    assert ExitCode.BACKEND_ERROR == 75
