# Copyright 2024-2026 Lager Data LLC
# SPDX-License-Identifier: Apache-2.0

"""
    cli.output

    Centralized CLI output for the lager command-line tool.

    Two presentation modes:
        - text:  human-friendly. Aligned key/value blocks, symbol prefixes
                 (✓/⚠/✗ on UTF-8 + color terminals, [OK]/[WARN]/[ERROR] otherwise).
        - json:  one envelope object per line (NDJSON). The envelope is the
                 stable contract for machine consumers.

    Envelope shape (the contract):
        {
            "lager":   1,
            "kind":    "ack" | "state" | "reading" | "list" | "error",
            "status":  "ok" | "warn" | "error",
            "command": "<dotted-path>"   (optional),
            "subject": {...}             (optional),
            "data":    {...} | [...]     (optional),
            "message": "<human string>"  (required for ack/warn/error),
            "exit_code": <int>           (only on error envelopes)
        }

    Color policy:
        --format=json always disables color (color in JSON strings breaks parsers).
        --color=auto follows isatty + NO_COLOR + FORCE_COLOR + TERM=dumb.
        --color=always forces on (still suppressed in JSON mode).
        --color=never forces off.

    Glyph fallback (separate from color): when stream encoding cannot encode
    UTF-8 symbols, fall back to [OK]/[WARN]/[ERROR] text prefixes.
"""
from __future__ import annotations

import dataclasses
import enum
import io
import json
import os
import sys
from typing import Any, Mapping, Optional, Sequence, Tuple, Union


# --------------------------------------------------------------------------- #
# Exit codes
# --------------------------------------------------------------------------- #

class ExitCode(enum.IntEnum):
    """Canonical exit codes used across lager commands.

    Numeric values follow BSD sysexits where applicable. Names are the
    public contract; numbers are documented but should be referenced via
    these constants.
    """
    OK = 0
    UNEXPECTED = 1
    USAGE = 64
    USER_ERROR = 65            # wrong-role, out-of-range, missing net
    DEVICE_NOT_FOUND = 68      # instrument not on the bus
    LIBRARY_MISSING = 70       # pyvisa / driver dep not installed
    BACKEND_ERROR = 75         # driver/SCPI error (formerly 4/5)


# --------------------------------------------------------------------------- #
# Format and color policy
# --------------------------------------------------------------------------- #

class Format(enum.Enum):
    TEXT = "text"
    JSON = "json"

    @classmethod
    def parse(cls, value: str) -> "Format":
        try:
            return cls(value.lower())
        except ValueError as exc:
            raise ValueError(f"unknown format {value!r} (expected text or json)") from exc


class ColorPolicy(enum.Enum):
    AUTO = "auto"
    ALWAYS = "always"
    NEVER = "never"

    @classmethod
    def parse(cls, value: str) -> "ColorPolicy":
        try:
            return cls(value.lower())
        except ValueError as exc:
            raise ValueError(f"unknown color policy {value!r} (expected auto, always, or never)") from exc


# Severity labels are stable identifiers used in JSON envelopes and to look up
# symbols/colors. Keep these strings aligned with box/lager/cli_output.py.
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

# ANSI color codes — kept tiny and stdlib-only on purpose.
_ANSI_RESET = "\033[0m"
_ANSI_BOLD = "\033[1m"
_ANSI_BY_SEVERITY = {
    SEVERITY_OK: "\033[32m",       # green
    SEVERITY_WARN: "\033[33m",     # yellow
    SEVERITY_ERROR: "\033[31m",    # red
    SEVERITY_INFO: "\033[36m",     # cyan
}


# --------------------------------------------------------------------------- #
# Resolution helpers
# --------------------------------------------------------------------------- #

def _stream_isatty(stream: Any) -> bool:
    fn = getattr(stream, "isatty", None)
    if fn is None:
        return False
    try:
        return bool(fn())
    except (ValueError, io.UnsupportedOperation):
        return False


def _stream_can_unicode(stream: Any) -> bool:
    """True if the stream's encoding can encode our symbol set."""
    encoding = getattr(stream, "encoding", None) or ""
    encoding = encoding.lower()
    if not encoding:
        return False
    if encoding in {"ascii", "us-ascii", "ansi_x3.4-1968"}:
        return False
    return "utf" in encoding or encoding in {"latin1", "latin-1", "iso-8859-1"}


def resolve_color(color: Union[str, ColorPolicy], stream: Any, *,
                  env: Optional[Mapping[str, str]] = None) -> bool:
    """Compute whether ANSI should be emitted to ``stream``.

    Order of precedence:
        1. NO_COLOR env (any non-empty value) → False
        2. explicit policy ``always`` → True; ``never`` → False
        3. stream is not a TTY → False
        4. TERM=dumb → False
        5. FORCE_COLOR=1/true/yes → True
        6. otherwise → True
    """
    if isinstance(color, str):
        color = ColorPolicy.parse(color)
    env = env if env is not None else os.environ
    if env.get("NO_COLOR"):
        return False
    if color is ColorPolicy.ALWAYS:
        return True
    if color is ColorPolicy.NEVER:
        return False
    if not _stream_isatty(stream):
        return False
    if env.get("TERM", "") == "dumb":
        return False
    if env.get("FORCE_COLOR", "").lower() in {"1", "true", "yes"}:
        return True
    return True


def resolve_unicode(stream: Any) -> bool:
    """True if we should use ✓/⚠/✗; False if we should fall back to [OK]/[WARN]/[ERROR]."""
    return _stream_can_unicode(stream)


@dataclasses.dataclass(frozen=True)
class OutputConfig:
    """Resolved presentation settings; built once at root-command startup
    and threaded through every command via LagerContext."""
    format: Format = Format.TEXT
    color: bool = False
    unicode: bool = True
    stream: Any = dataclasses.field(default=None)
    err_stream: Any = dataclasses.field(default=None)

    def with_overrides(self, **kwargs) -> "OutputConfig":
        return dataclasses.replace(self, **kwargs)


def resolve_config(format_value: Union[str, Format] = Format.TEXT,
                   color_value: Union[str, ColorPolicy] = ColorPolicy.AUTO,
                   *,
                   stream: Any = None,
                   err_stream: Any = None,
                   env: Optional[Mapping[str, str]] = None) -> OutputConfig:
    """Apply the §C policy and return an OutputConfig."""
    if isinstance(format_value, str):
        format_value = Format.parse(format_value)
    if isinstance(color_value, str):
        color_value = ColorPolicy.parse(color_value)

    out_stream = stream if stream is not None else sys.stdout
    err = err_stream if err_stream is not None else sys.stderr

    if format_value is Format.JSON:
        # Color is never useful inside JSON strings; force off regardless of policy.
        color_enabled = False
    else:
        color_enabled = resolve_color(color_value, out_stream, env=env)

    unicode_ok = resolve_unicode(out_stream)

    return OutputConfig(
        format=format_value,
        color=color_enabled,
        unicode=unicode_ok,
        stream=out_stream,
        err_stream=err,
    )


def default_config() -> OutputConfig:
    """Module-default config used when no LagerContext has been built yet
    (e.g. early startup error paths). Conservative: text mode, auto color,
    auto unicode."""
    return resolve_config(Format.TEXT, ColorPolicy.AUTO)


# --------------------------------------------------------------------------- #
# Field model for state/list rendering
# --------------------------------------------------------------------------- #

Scalar = Union[str, int, float, bool, None]
ValueType = Union[Scalar, Tuple[Scalar, ...]]
UnitType = Union[Optional[str], Tuple[Optional[str], ...]]


@dataclasses.dataclass(frozen=True)
class Field:
    """One row of a state block.

    For composite values (e.g. a "Set: 0.0 V / 1.000 A" line that combines
    a setpoint voltage and a setpoint current), pass tuples for ``value``,
    ``unit``, and ``json_subkeys``:

        Field("Set", value=(0.0, 1.000), unit=("V", "A"),
              json_subkeys=("voltage", "current"))

    Text mode renders this as a single line; JSON mode emits separate keys
    (e.g. ``set_voltage``, ``set_voltage_unit``, ``set_current``,
    ``set_current_unit``).
    """
    label: str
    value: ValueType
    unit: UnitType = None
    severity: Optional[str] = None
    json_key: Optional[str] = None
    json_subkeys: Optional[Tuple[str, ...]] = None
    text: Optional[str] = None           # opt-out: pre-formatted text override

    def derived_json_key(self) -> str:
        if self.json_key:
            return self.json_key
        return self.label.strip().lower().replace(" ", "_").replace("-", "_")


# --------------------------------------------------------------------------- #
# Low-level emitters
# --------------------------------------------------------------------------- #

def _symbol(severity: str, cfg: OutputConfig) -> str:
    if cfg.unicode:
        return _SYMBOL_UNICODE.get(severity, _SYMBOL_UNICODE[SEVERITY_INFO])
    return _SYMBOL_ASCII.get(severity, _SYMBOL_ASCII[SEVERITY_INFO])


def _color_wrap(text: str, severity: Optional[str], cfg: OutputConfig, *, bold: bool = False) -> str:
    if not cfg.color or severity not in _ANSI_BY_SEVERITY:
        return text if not bold or not cfg.color else f"{_ANSI_BOLD}{text}{_ANSI_RESET}"
    code = _ANSI_BY_SEVERITY[severity]
    if bold:
        code = _ANSI_BOLD + code
    return f"{code}{text}{_ANSI_RESET}"


def _emit_json(envelope: dict, cfg: OutputConfig, *, err: bool = False) -> None:
    """Write a single JSON object as one line to stdout (or stderr)."""
    stream = cfg.err_stream if err else cfg.stream
    line = json.dumps(envelope, separators=(",", ":"), ensure_ascii=False)
    stream.write(line + "\n")
    try:
        stream.flush()
    except (AttributeError, ValueError):
        pass


def _emit_text(line: str, cfg: OutputConfig, *, err: bool = False) -> None:
    stream = cfg.err_stream if err else cfg.stream
    stream.write(line + "\n")
    try:
        stream.flush()
    except (AttributeError, ValueError):
        pass


# --------------------------------------------------------------------------- #
# Title / subject formatting
# --------------------------------------------------------------------------- #

def _format_subject_line(title: str, subject: Optional[Mapping[str, Any]]) -> str:
    """Compose a state-block title:
        ``supply2``                                      (no subject extras)
        ``supply2 (Rigol DP821, ch 1)``                  (instrument + channel)
    """
    if not subject:
        return title
    extras = []
    instrument = subject.get("instrument") if isinstance(subject, Mapping) else None
    channel = subject.get("channel") if isinstance(subject, Mapping) else None
    if instrument:
        extras.append(str(instrument))
    if channel is not None:
        extras.append(f"ch {channel}")
    if extras:
        return f"{title} ({', '.join(extras)})"
    return title


# --------------------------------------------------------------------------- #
# Field rendering (text + JSON)
# --------------------------------------------------------------------------- #

def _format_scalar_value(value: Scalar, unit: Optional[str]) -> str:
    if value is None:
        return "—"
    if isinstance(value, bool):
        return "ON" if value else "OFF"
    if isinstance(value, float):
        # Strip trailing zeros but keep at least one decimal for non-integer floats.
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


def _field_to_json(field: Field) -> dict:
    """Return a flat dict of {key: value} pairs for one Field."""
    out: dict[str, Any] = {}
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


def _coerce_json_value(value: Scalar) -> Any:
    """Pass through JSON-native types; convert bools and None as-is."""
    if isinstance(value, (bool, int, float, str)) or value is None:
        return value
    return str(value)


def _format_state_block(title: str, fields: Sequence[Field], cfg: OutputConfig,
                        subject: Optional[Mapping[str, Any]],
                        title_severity: str = SEVERITY_OK) -> str:
    """Render a state block in text mode. The first line is

        ``<symbol> <title> [(subject extras)]``

    followed by one indented ``Label:  Value`` line per field, with labels
    column-aligned. The label width is computed from the longest visible label.
    """
    sym = _symbol(title_severity, cfg)
    sym_colored = _color_wrap(sym, title_severity, cfg, bold=True)
    title_line = f"{sym_colored} {_format_subject_line(title, subject)}"

    if not fields:
        return title_line

    label_width = max(len(field.label) for field in fields)
    body_lines: list[str] = []
    for field in fields:
        value_text = _format_field_text(field)
        if field.severity and cfg.color:
            value_text = _color_wrap(value_text, field.severity, cfg)
        # Label colon padding: "Output:    " — pad to label_width + 1 (for colon) + 2 spaces.
        label_with_colon = f"{field.label}:".ljust(label_width + 3)
        body_lines.append(f"  {label_with_colon}{value_text}")

    return "\n".join([title_line, *body_lines])


# --------------------------------------------------------------------------- #
# Public API
# --------------------------------------------------------------------------- #

def _build_envelope(*, kind: str, status: str,
                    message: Optional[str] = None,
                    command: Optional[str] = None,
                    subject: Optional[Mapping[str, Any]] = None,
                    data: Any = None,
                    exit_code: Optional[int] = None) -> dict:
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


def success(message: str, *, cfg: Optional[OutputConfig] = None,
            command: Optional[str] = None,
            subject: Optional[Mapping[str, Any]] = None,
            data: Any = None) -> None:
    cfg = cfg or default_config()
    if cfg.format is Format.JSON:
        _emit_json(_build_envelope(kind="ack", status="ok", message=message,
                                    command=command, subject=subject, data=data), cfg)
        return
    sym = _color_wrap(_symbol(SEVERITY_OK, cfg), SEVERITY_OK, cfg, bold=True)
    _emit_text(f"{sym} {message}", cfg)


def warn(message: str, *, cfg: Optional[OutputConfig] = None,
         command: Optional[str] = None,
         subject: Optional[Mapping[str, Any]] = None,
         data: Any = None) -> None:
    cfg = cfg or default_config()
    if cfg.format is Format.JSON:
        _emit_json(_build_envelope(kind="ack", status="warn", message=message,
                                    command=command, subject=subject, data=data), cfg, err=True)
        return
    sym = _color_wrap(_symbol(SEVERITY_WARN, cfg), SEVERITY_WARN, cfg, bold=True)
    _emit_text(f"{sym} {message}", cfg, err=True)


def error(message: str, *, cfg: Optional[OutputConfig] = None,
          exit_code: int = ExitCode.UNEXPECTED,
          command: Optional[str] = None,
          subject: Optional[Mapping[str, Any]] = None,
          data: Any = None,
          raise_exit: bool = True) -> None:
    """Emit an error envelope and (by default) raise SystemExit(exit_code).

    In JSON mode the envelope goes to stdout (machines parse one stream).
    In text mode the message goes to stderr.
    """
    cfg = cfg or default_config()
    code = int(exit_code)
    if cfg.format is Format.JSON:
        _emit_json(_build_envelope(kind="error", status="error", message=message,
                                    command=command, subject=subject, data=data,
                                    exit_code=code), cfg)
    else:
        sym = _color_wrap(_symbol(SEVERITY_ERROR, cfg), SEVERITY_ERROR, cfg, bold=True)
        _emit_text(f"{sym} {message}", cfg, err=True)
    if raise_exit:
        raise SystemExit(code)


def state(title: str, fields: Sequence[Field], *,
          cfg: Optional[OutputConfig] = None,
          command: Optional[str] = None,
          subject: Optional[Mapping[str, Any]] = None,
          title_severity: str = SEVERITY_OK) -> None:
    cfg = cfg or default_config()
    if cfg.format is Format.JSON:
        data: dict[str, Any] = {}
        for field in fields:
            data.update(_field_to_json(field))
        _emit_json(_build_envelope(kind="state",
                                    status="ok" if title_severity != SEVERITY_ERROR else "error",
                                    command=command, subject=subject, data=data), cfg)
        return
    block = _format_state_block(title, fields, cfg, subject, title_severity=title_severity)
    _emit_text(block, cfg)


def action(message: str, *, cfg: Optional[OutputConfig] = None,
           command: Optional[str] = None,
           subject: Optional[Mapping[str, Any]] = None,
           data: Any = None) -> None:
    """One-line success message for state-changing operations
    (`enable`, `disable`, `clear-ovp`, etc.). Same shape as success()."""
    success(message, cfg=cfg, command=command, subject=subject, data=data)


def reading(label: str, value: Scalar, unit: Optional[str] = None, *,
            cfg: Optional[OutputConfig] = None,
            command: Optional[str] = None,
            subject: Optional[Mapping[str, Any]] = None) -> None:
    """Render a single scalar reading (e.g. ``lager supply ... voltage``).

    Text:  ``3.300 V``
    JSON:  envelope with kind="reading", data={label: value, label_unit: unit}.
    """
    cfg = cfg or default_config()
    if cfg.format is Format.JSON:
        json_key = label.strip().lower().replace(" ", "_")
        data: dict[str, Any] = {json_key: _coerce_json_value(value)}
        if unit:
            data[f"{json_key}_unit"] = unit
        _emit_json(_build_envelope(kind="reading", status="ok",
                                    command=command, subject=subject, data=data), cfg)
        return
    _emit_text(_format_scalar_value(value, unit), cfg)


def list_table(headers: Sequence[str], rows: Sequence[Sequence[Any]], *,
               cfg: Optional[OutputConfig] = None,
               command: Optional[str] = None,
               subject: Optional[Mapping[str, Any]] = None) -> None:
    """Render a tabular listing.

    Text mode: simple aligned columns (no third-party deps).
    JSON mode: ``data`` is a list of {header: value, ...} objects.
    """
    cfg = cfg or default_config()
    if cfg.format is Format.JSON:
        records = [
            {h.strip().lower().replace(" ", "_"): _coerce_json_value(v) for h, v in zip(headers, row)}
            for row in rows
        ]
        _emit_json(_build_envelope(kind="list", status="ok",
                                    command=command, subject=subject, data=records), cfg)
        return
    if not rows:
        _emit_text("(no entries)", cfg)
        return
    widths = [len(str(h)) for h in headers]
    str_rows = [[str(c) for c in row] for row in rows]
    for row in str_rows:
        for i, cell in enumerate(row):
            if i < len(widths) and len(cell) > widths[i]:
                widths[i] = len(cell)
    sep = "  "
    header_line = sep.join(str(h).ljust(w) for h, w in zip(headers, widths))
    underline = sep.join("-" * w for w in widths)
    body = [sep.join(cell.ljust(w) for cell, w in zip(row, widths)) for row in str_rows]
    _emit_text("\n".join([header_line, underline, *body]), cfg)


# --------------------------------------------------------------------------- #
# Tree-table sketch (not consumed by pilot — placeholder for nets migration)
# --------------------------------------------------------------------------- #

@dataclasses.dataclass(frozen=True)
class TreeGroup:
    """One section of a tree_table: a header line plus a list of indented rows."""
    title: str
    rows: Sequence[Sequence[Any]]


def tree_table(headers: Sequence[str], groups: Sequence[TreeGroup], *,
               cfg: Optional[OutputConfig] = None,
               command: Optional[str] = None) -> None:
    """Render grouped rows as a tree (├──/└── prefixes).

    Foundation API only; not yet consumed. The migration of
    ``cli/commands/box/nets.py`` will be its first user.
    """
    cfg = cfg or default_config()
    if cfg.format is Format.JSON:
        data = [
            {
                "title": g.title,
                "rows": [
                    {h.strip().lower().replace(" ", "_"): _coerce_json_value(v) for h, v in zip(headers, row)}
                    for row in g.rows
                ],
            }
            for g in groups
        ]
        _emit_json(_build_envelope(kind="list", status="ok", command=command, data=data), cfg)
        return

    # Compute global column widths across every row in every group.
    widths = [len(str(h)) for h in headers]
    for g in groups:
        for row in g.rows:
            for i, cell in enumerate(row):
                if i < len(widths) and len(str(cell)) > widths[i]:
                    widths[i] = len(str(cell))

    sep = "  "
    out: list[str] = []
    out.append(sep.join(str(h).ljust(w) for h, w in zip(headers, widths)))
    out.append(sep.join("=" * w for w in widths))
    for g in groups:
        out.append("")
        out.append(g.title)
        for i, row in enumerate(g.rows):
            is_last = i == len(g.rows) - 1
            prefix = "└── " if is_last else "├── "
            cells = [str(c).ljust(w) for c, w in zip(row, widths)]
            out.append(prefix + sep.join(cells))
    _emit_text("\n".join(out), cfg)


__all__ = [
    "ColorPolicy",
    "ExitCode",
    "Field",
    "Format",
    "OutputConfig",
    "TreeGroup",
    "action",
    "default_config",
    "error",
    "list_table",
    "reading",
    "resolve_color",
    "resolve_config",
    "resolve_unicode",
    "state",
    "success",
    "tree_table",
    "warn",
    "SEVERITY_OK",
    "SEVERITY_WARN",
    "SEVERITY_ERROR",
    "SEVERITY_INFO",
]
