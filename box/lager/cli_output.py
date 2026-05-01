# Copyright 2024-2026 Lager Data LLC
# SPDX-License-Identifier: Apache-2.0

"""
    lager.cli_output

    Box-side mirror of cli/output.py. Used by impl scripts in cli/impl/ that
    are uploaded into the box container, and by the drivers under box/lager/
    that emit human-readable state/readings.

    Wire-format compatibility: the JSON envelope here MUST match the shape
    described in cli/output.py — both modules document the contract.

    Presentation is driven by these env vars (set by the CLI when invoking
    the impl script):
        LAGER_OUTPUT_FORMAT  text | json     (default text)
        LAGER_OUTPUT_COLOR   1 | 0           (default: TTY-detected)
        NO_COLOR             any non-empty   (overrides everything)
"""
from __future__ import annotations

import dataclasses
import enum
import json
import os
import sys
from typing import Any, Optional, Sequence, Tuple, Union


# Exit codes — kept in lockstep with cli.output.ExitCode.
class ExitCode(enum.IntEnum):
    OK = 0
    UNEXPECTED = 1
    USAGE = 64
    USER_ERROR = 65
    DEVICE_NOT_FOUND = 68
    LIBRARY_MISSING = 70
    BACKEND_ERROR = 75


SEVERITY_OK = "ok"
SEVERITY_WARN = "warn"
SEVERITY_ERROR = "error"
SEVERITY_INFO = "info"

_SYMBOL_UNICODE = {
    SEVERITY_OK: "✓",
    SEVERITY_WARN: "⚠",
    SEVERITY_ERROR: "✗",
    SEVERITY_INFO: "•",
}
_SYMBOL_ASCII = {
    SEVERITY_OK: "[OK]",
    SEVERITY_WARN: "[WARN]",
    SEVERITY_ERROR: "[ERROR]",
    SEVERITY_INFO: "[INFO]",
}


class ANSI:
    """Single source of ANSI escapes for the box container.

    Replaces the per-module GREEN/RED/YELLOW/RESET redefinitions previously
    scattered across box/lager/power/.../*.py and box/lager/automation/usb_hub/.
    """
    RESET = "\033[0m"
    BOLD = "\033[1m"
    GREEN = "\033[32m"
    RED = "\033[31m"
    YELLOW = "\033[33m"
    CYAN = "\033[36m"


_ANSI_BY_SEVERITY = {
    SEVERITY_OK: ANSI.GREEN,
    SEVERITY_WARN: ANSI.YELLOW,
    SEVERITY_ERROR: ANSI.RED,
    SEVERITY_INFO: ANSI.CYAN,
}


# --------------------------------------------------------------------------- #
# Mode detection
# --------------------------------------------------------------------------- #

def _json_mode() -> bool:
    return os.environ.get("LAGER_OUTPUT_FORMAT", "text").lower() == "json"


def _color_enabled(stream=None) -> bool:
    if os.environ.get("NO_COLOR"):
        return False
    explicit = os.environ.get("LAGER_OUTPUT_COLOR")
    if explicit == "1":
        return True
    if explicit == "0":
        return False
    s = stream if stream is not None else sys.stdout
    isatty = getattr(s, "isatty", None)
    try:
        return bool(isatty()) if isatty else False
    except Exception:
        return False


def _unicode_enabled(stream=None) -> bool:
    s = stream if stream is not None else sys.stdout
    encoding = (getattr(s, "encoding", None) or "").lower()
    if not encoding:
        return False
    if encoding in {"ascii", "us-ascii", "ansi_x3.4-1968"}:
        return False
    return "utf" in encoding or encoding in {"latin1", "latin-1", "iso-8859-1"}


def _symbol(severity: str, *, stream=None) -> str:
    table = _SYMBOL_UNICODE if _unicode_enabled(stream) else _SYMBOL_ASCII
    return table.get(severity, table[SEVERITY_INFO])


def _color_wrap(text: str, severity: Optional[str], *, stream=None, bold: bool = False) -> str:
    if not _color_enabled(stream):
        return text
    code = _ANSI_BY_SEVERITY.get(severity, "") if severity else ""
    if bold:
        code = ANSI.BOLD + code
    if not code:
        return text
    return f"{code}{text}{ANSI.RESET}"


# --------------------------------------------------------------------------- #
# Field model
# --------------------------------------------------------------------------- #

Scalar = Union[str, int, float, bool, None]
ValueType = Union[Scalar, Tuple[Scalar, ...]]
UnitType = Union[Optional[str], Tuple[Optional[str], ...]]


@dataclasses.dataclass(frozen=True)
class Field:
    """One row of a state block. See cli/output.py:Field for full docs."""
    label: str
    value: ValueType
    unit: UnitType = None
    severity: Optional[str] = None
    json_key: Optional[str] = None
    json_subkeys: Optional[Tuple[str, ...]] = None
    text: Optional[str] = None

    def derived_json_key(self) -> str:
        if self.json_key:
            return self.json_key
        return self.label.strip().lower().replace(" ", "_").replace("-", "_")


# --------------------------------------------------------------------------- #
# Formatting helpers (mirror cli/output.py exactly so the wire shape matches)
# --------------------------------------------------------------------------- #

def _format_scalar_value(value: Scalar, unit: Optional[str]) -> str:
    if value is None:
        return "—"
    if isinstance(value, bool):
        return "ON" if value else "OFF"
    if isinstance(value, float):
        if value == int(value):
            text = f"{int(value)}.0"
        else:
            text = f"{value:g}"
        return f"{text} {unit}" if unit else text
    if isinstance(value, int):
        return f"{value} {unit}" if unit else str(value)
    text = str(value)
    return f"{text} {unit}" if unit else text


def _format_field_text(field: Field) -> str:
    if field.text is not None:
        return field.text
    if isinstance(field.value, tuple):
        units = field.unit if isinstance(field.unit, tuple) else (None,) * len(field.value)
        if len(units) != len(field.value):
            units = tuple(list(units) + [None] * (len(field.value) - len(units)))
        return "  /  ".join(_format_scalar_value(v, u) for v, u in zip(field.value, units))
    return _format_scalar_value(field.value, field.unit if isinstance(field.unit, str) else None)


def _coerce_json_value(value: Scalar) -> Any:
    if isinstance(value, (bool, int, float, str)) or value is None:
        return value
    return str(value)


def _field_to_json(field: Field) -> dict:
    out: dict = {}
    base_key = field.derived_json_key()
    if isinstance(field.value, tuple):
        sub = field.json_subkeys or tuple(f"part{i}" for i in range(len(field.value)))
        units = field.unit if isinstance(field.unit, tuple) else (None,) * len(field.value)
        if len(units) != len(field.value):
            units = tuple(list(units) + [None] * (len(field.value) - len(units)))
        for sub_key, value, unit in zip(sub, field.value, units):
            full_key = f"{base_key}_{sub_key}"
            out[full_key] = _coerce_json_value(value)
            if unit:
                out[f"{full_key}_unit"] = unit
    else:
        out[base_key] = _coerce_json_value(field.value)
        if isinstance(field.unit, str) and field.unit:
            out[f"{base_key}_unit"] = field.unit
    return out


def _format_subject_line(title: str, subject: Optional[dict]) -> str:
    if not subject:
        return title
    extras = []
    instrument = subject.get("instrument")
    channel = subject.get("channel")
    if instrument:
        extras.append(str(instrument))
    if channel is not None:
        extras.append(f"ch {channel}")
    if extras:
        return f"{title} ({', '.join(extras)})"
    return title


# --------------------------------------------------------------------------- #
# Emit primitives
# --------------------------------------------------------------------------- #

def _emit_json(envelope: dict, *, stream=None) -> None:
    s = stream if stream is not None else sys.stdout
    s.write(json.dumps(envelope, separators=(",", ":"), ensure_ascii=False) + "\n")
    try:
        s.flush()
    except Exception:
        pass


def _emit_text(line: str, *, stream=None) -> None:
    s = stream if stream is not None else sys.stdout
    s.write(line + "\n")
    try:
        s.flush()
    except Exception:
        pass


def _build_envelope(*, kind: str, status: str, message: Optional[str] = None,
                    command: Optional[str] = None, subject: Optional[dict] = None,
                    data: Any = None, exit_code: Optional[int] = None) -> dict:
    env: dict = {"lager": 1, "kind": kind, "status": status}
    if command is not None:
        env["command"] = command
    if subject is not None:
        env["subject"] = dict(subject)
    if message is not None:
        env["message"] = message
    if data is not None:
        env["data"] = data
    if exit_code is not None:
        env["exit_code"] = int(exit_code)
    return env


# --------------------------------------------------------------------------- #
# Public API
# --------------------------------------------------------------------------- #

def print_state(title: str, fields: Sequence[Field], *,
                command: Optional[str] = None,
                subject: Optional[dict] = None,
                title_severity: str = SEVERITY_OK) -> None:
    """Render a state block. Mirrors cli.output.state()."""
    if _json_mode():
        data: dict = {}
        for field in fields:
            data.update(_field_to_json(field))
        _emit_json(_build_envelope(
            kind="state",
            status="ok" if title_severity != SEVERITY_ERROR else "error",
            command=command, subject=subject, data=data))
        return

    sym = _color_wrap(_symbol(title_severity), title_severity, bold=True)
    title_line = f"{sym} {_format_subject_line(title, subject)}"
    if not fields:
        _emit_text(title_line)
        return
    label_width = max(len(f.label) for f in fields)
    body = []
    for f in fields:
        text = _format_field_text(f)
        if f.severity:
            text = _color_wrap(text, f.severity)
        label_part = f"{f.label}:".ljust(label_width + 3)
        body.append(f"  {label_part}{text}")
    _emit_text("\n".join([title_line, *body]))


def print_action(message: str, *,
                 command: Optional[str] = None,
                 subject: Optional[dict] = None,
                 data: Any = None) -> None:
    """One-line success ack — mirrors cli.output.success()/action()."""
    if _json_mode():
        _emit_json(_build_envelope(kind="ack", status="ok", message=message,
                                    command=command, subject=subject, data=data))
        return
    sym = _color_wrap(_symbol(SEVERITY_OK), SEVERITY_OK, bold=True)
    _emit_text(f"{sym} {message}")


def print_warn(message: str, *,
               command: Optional[str] = None,
               subject: Optional[dict] = None,
               data: Any = None) -> None:
    if _json_mode():
        _emit_json(_build_envelope(kind="ack", status="warn", message=message,
                                    command=command, subject=subject, data=data),
                   stream=sys.stderr)
        return
    sym = _color_wrap(_symbol(SEVERITY_WARN, stream=sys.stderr), SEVERITY_WARN, bold=True)
    _emit_text(f"{sym} {message}", stream=sys.stderr)


def print_reading(label: str, value: Scalar, unit: Optional[str] = None, *,
                  command: Optional[str] = None,
                  subject: Optional[dict] = None) -> None:
    """One scalar reading — mirrors cli.output.reading()."""
    if _json_mode():
        json_key = label.strip().lower().replace(" ", "_")
        data: dict = {json_key: _coerce_json_value(value)}
        if unit:
            data[f"{json_key}_unit"] = unit
        _emit_json(_build_envelope(kind="reading", status="ok",
                                    command=command, subject=subject, data=data))
        return
    _emit_text(_format_scalar_value(value, unit))


def die(message: str, *, code: int = ExitCode.UNEXPECTED,
        category: str = "backend",
        command: Optional[str] = None,
        subject: Optional[dict] = None,
        data: Optional[dict] = None) -> "NoReturn":  # type: ignore[name-defined]
    """Replaces the per-impl die() helpers in cli/impl/power/{supply,battery}.py.

    JSON mode: emits an error envelope to stdout with exit_code, then exits.
    Text mode: emits ``[ERROR] <message>`` to stderr (color where possible).
    """
    code_int = int(code)
    if _json_mode():
        env_data = dict(data) if data else {}
        if category and "category" not in env_data:
            env_data["category"] = category
        _emit_json(_build_envelope(kind="error", status="error", message=message,
                                    command=command, subject=subject,
                                    data=env_data or None,
                                    exit_code=code_int))
    else:
        sym = _color_wrap(_symbol(SEVERITY_ERROR, stream=sys.stderr),
                          SEVERITY_ERROR, bold=True)
        _emit_text(f"{sym} {message}", stream=sys.stderr)
    sys.exit(code_int)


__all__ = [
    "ANSI",
    "ExitCode",
    "Field",
    "die",
    "print_action",
    "print_reading",
    "print_state",
    "print_warn",
    "SEVERITY_OK",
    "SEVERITY_WARN",
    "SEVERITY_ERROR",
    "SEVERITY_INFO",
]
