# Copyright 2024-2026 Lager Data LLC
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for box/lager/cli_output.py.

The box-side helper mirrors cli/output.py — these tests assert the wire
format matches and that env-driven mode selection (LAGER_OUTPUT_FORMAT,
LAGER_OUTPUT_COLOR, NO_COLOR) behaves correctly.
"""
from __future__ import annotations

import importlib.util
import io
import json
import os
import sys
from pathlib import Path

import pytest


# Load box/lager/cli_output.py without importing the full lager box package
# (which has hardware dependencies like pyvisa not present in the unit-test env).
_HERE = Path(__file__).resolve().parent
_REPO = _HERE.parent.parent.parent
_CLI_OUTPUT_PATH = _REPO / "box" / "lager" / "cli_output.py"

_spec = importlib.util.spec_from_file_location("_box_cli_output", _CLI_OUTPUT_PATH)
cli_output = importlib.util.module_from_spec(_spec)  # type: ignore[arg-type]
sys.modules["_box_cli_output"] = cli_output  # required for dataclasses to resolve types
_spec.loader.exec_module(cli_output)  # type: ignore[union-attr]


class _Stream(io.StringIO):
    def __init__(self, *, tty: bool = False, encoding: str = "utf-8") -> None:
        super().__init__()
        self._tty = tty
        self._encoding = encoding

    def isatty(self) -> bool:
        return self._tty

    @property
    def encoding(self) -> str:  # type: ignore[override]
        return self._encoding


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    """Clear output-related env vars before each test."""
    for var in ("LAGER_OUTPUT_FORMAT", "LAGER_OUTPUT_COLOR", "NO_COLOR",
                "FORCE_COLOR", "TERM"):
        monkeypatch.delenv(var, raising=False)


# --------------------------------------------------------------------------- #
# Mode detection
# --------------------------------------------------------------------------- #

class TestJsonMode:
    def test_default_is_text(self):
        assert cli_output._json_mode() is False

    def test_env_set_json(self, monkeypatch):
        monkeypatch.setenv("LAGER_OUTPUT_FORMAT", "json")
        assert cli_output._json_mode() is True

    def test_case_insensitive(self, monkeypatch):
        monkeypatch.setenv("LAGER_OUTPUT_FORMAT", "JSON")
        assert cli_output._json_mode() is True


class TestColorEnabled:
    def test_no_color_overrides(self, monkeypatch):
        monkeypatch.setenv("NO_COLOR", "1")
        monkeypatch.setenv("LAGER_OUTPUT_COLOR", "1")
        assert cli_output._color_enabled(_Stream(tty=True)) is False

    def test_explicit_on(self, monkeypatch):
        monkeypatch.setenv("LAGER_OUTPUT_COLOR", "1")
        assert cli_output._color_enabled(_Stream(tty=False)) is True

    def test_explicit_off(self, monkeypatch):
        monkeypatch.setenv("LAGER_OUTPUT_COLOR", "0")
        assert cli_output._color_enabled(_Stream(tty=True)) is False

    def test_default_uses_isatty(self):
        assert cli_output._color_enabled(_Stream(tty=True)) is True
        assert cli_output._color_enabled(_Stream(tty=False)) is False


# --------------------------------------------------------------------------- #
# print_state
# --------------------------------------------------------------------------- #

class TestPrintStateText:
    def test_alignment_no_color(self, monkeypatch):
        monkeypatch.setenv("LAGER_OUTPUT_COLOR", "0")
        buf = _Stream()
        monkeypatch.setattr(sys, "stdout", buf)
        cli_output.print_state(
            "supply2",
            [
                cli_output.Field("Output", "OFF"),
                cli_output.Field("OCP", 1.1, unit="A"),
                cli_output.Field("OVP", 66.0, unit="V"),
            ],
            subject={"instrument": "Rigol DP821", "channel": 1},
        )
        out = buf.getvalue()
        assert out.startswith("✓ supply2 (Rigol DP821, ch 1)\n")
        assert "  Output:  OFF" in out
        assert "  OCP:     1.1 A" in out

    def test_ascii_fallback(self, monkeypatch):
        monkeypatch.setenv("LAGER_OUTPUT_COLOR", "0")
        buf = _Stream(encoding="ascii")
        monkeypatch.setattr(sys, "stdout", buf)
        cli_output.print_state("supply2", [cli_output.Field("Output", "OFF")])
        assert buf.getvalue().startswith("[OK] supply2")


class TestPrintStateJson:
    def test_envelope_matches_cli_side_contract(self, monkeypatch):
        monkeypatch.setenv("LAGER_OUTPUT_FORMAT", "json")
        buf = _Stream()
        monkeypatch.setattr(sys, "stdout", buf)
        cli_output.print_state(
            "supply2",
            [
                cli_output.Field("Output", "OFF"),
                cli_output.Field("Set", value=(0.0, 1.0), unit=("V", "A"),
                                 json_subkeys=("voltage", "current")),
                cli_output.Field("OCP", 1.1, unit="A"),
            ],
            command="supply.state",
            subject={"net": "supply2", "instrument": "Rigol DP821", "channel": 1},
        )
        env = json.loads(buf.getvalue())
        assert env["lager"] == 1
        assert env["kind"] == "state"
        assert env["status"] == "ok"
        assert env["command"] == "supply.state"
        assert env["subject"]["net"] == "supply2"
        assert env["data"] == {
            "output": "OFF",
            "set_voltage": 0.0, "set_voltage_unit": "V",
            "set_current": 1.0, "set_current_unit": "A",
            "ocp": 1.1, "ocp_unit": "A",
        }

    def test_one_object_per_line(self, monkeypatch):
        monkeypatch.setenv("LAGER_OUTPUT_FORMAT", "json")
        buf = _Stream()
        monkeypatch.setattr(sys, "stdout", buf)
        cli_output.print_state("a", [cli_output.Field("X", 1)])
        cli_output.print_state("b", [cli_output.Field("Y", 2)])
        lines = [ln for ln in buf.getvalue().splitlines() if ln]
        assert len(lines) == 2
        assert all(json.loads(ln)["lager"] == 1 for ln in lines)


# --------------------------------------------------------------------------- #
# print_action / print_reading
# --------------------------------------------------------------------------- #

class TestPrintAction:
    def test_text(self, monkeypatch):
        monkeypatch.setenv("LAGER_OUTPUT_COLOR", "0")
        buf = _Stream()
        monkeypatch.setattr(sys, "stdout", buf)
        cli_output.print_action("Enabled supply output")
        assert buf.getvalue() == "✓ Enabled supply output\n"

    def test_ascii_fallback(self, monkeypatch):
        monkeypatch.setenv("LAGER_OUTPUT_COLOR", "0")
        buf = _Stream(encoding="ascii")
        monkeypatch.setattr(sys, "stdout", buf)
        cli_output.print_action("Done")
        assert buf.getvalue() == "[OK] Done\n"

    def test_json(self, monkeypatch):
        monkeypatch.setenv("LAGER_OUTPUT_FORMAT", "json")
        buf = _Stream()
        monkeypatch.setattr(sys, "stdout", buf)
        cli_output.print_action("Enabled", command="supply.enable",
                                subject={"net": "supply2"})
        env = json.loads(buf.getvalue())
        assert env == {
            "lager": 1, "kind": "ack", "status": "ok",
            "command": "supply.enable",
            "subject": {"net": "supply2"},
            "message": "Enabled",
        }


class TestPrintReading:
    def test_text(self, monkeypatch):
        monkeypatch.setenv("LAGER_OUTPUT_COLOR", "0")
        buf = _Stream()
        monkeypatch.setattr(sys, "stdout", buf)
        cli_output.print_reading("voltage", 3.3, "V")
        assert buf.getvalue() == "3.3 V\n"

    def test_json(self, monkeypatch):
        monkeypatch.setenv("LAGER_OUTPUT_FORMAT", "json")
        buf = _Stream()
        monkeypatch.setattr(sys, "stdout", buf)
        cli_output.print_reading("voltage", 3.3, "V",
                                  command="supply.voltage",
                                  subject={"net": "supply2"})
        env = json.loads(buf.getvalue())
        assert env["kind"] == "reading"
        assert env["data"] == {"voltage": 3.3, "voltage_unit": "V"}


# --------------------------------------------------------------------------- #
# die()
# --------------------------------------------------------------------------- #

class TestDie:
    def test_text_to_stderr_and_exits(self, monkeypatch):
        monkeypatch.setenv("LAGER_OUTPUT_COLOR", "0")
        out_buf, err_buf = _Stream(), _Stream()
        monkeypatch.setattr(sys, "stdout", out_buf)
        monkeypatch.setattr(sys, "stderr", err_buf)
        with pytest.raises(SystemExit) as exc:
            cli_output.die("boom", code=cli_output.ExitCode.USER_ERROR)
        assert exc.value.code == 65
        assert err_buf.getvalue() == "✗ boom\n"
        assert out_buf.getvalue() == ""

    def test_json_to_stdout_and_exits(self, monkeypatch):
        monkeypatch.setenv("LAGER_OUTPUT_FORMAT", "json")
        out_buf, err_buf = _Stream(), _Stream()
        monkeypatch.setattr(sys, "stdout", out_buf)
        monkeypatch.setattr(sys, "stderr", err_buf)
        with pytest.raises(SystemExit) as exc:
            cli_output.die(
                "wrong role",
                code=cli_output.ExitCode.USER_ERROR,
                command="battery.state",
                subject={"net": "supply2",
                         "actual_role": "power-supply",
                         "expected_role": "battery"},
            )
        assert exc.value.code == 65
        env = json.loads(out_buf.getvalue())
        assert env["kind"] == "error"
        assert env["status"] == "error"
        assert env["exit_code"] == 65
        assert env["message"] == "wrong role"
        assert env["data"]["category"] == "backend"
        assert env["subject"]["actual_role"] == "power-supply"
        assert err_buf.getvalue() == ""

    def test_default_code(self, monkeypatch):
        monkeypatch.setenv("LAGER_OUTPUT_COLOR", "0")
        monkeypatch.setattr(sys, "stderr", _Stream())
        with pytest.raises(SystemExit) as exc:
            cli_output.die("oops")
        assert exc.value.code == 1


# --------------------------------------------------------------------------- #
# Exit codes match cli.output.ExitCode
# --------------------------------------------------------------------------- #

def test_exit_code_values_match_cli_side():
    """The two ExitCode enums must stay in lockstep — they're a wire contract."""
    sys.path.insert(0, str(_REPO))
    from cli.output import ExitCode as CliExit
    for name in ("OK", "UNEXPECTED", "USAGE", "USER_ERROR",
                 "DEVICE_NOT_FOUND", "LIBRARY_MISSING", "BACKEND_ERROR"):
        assert int(getattr(cli_output.ExitCode, name)) == int(getattr(CliExit, name)), name
