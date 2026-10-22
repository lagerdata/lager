# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

"""
lager.drivers.manifest - Parse and validate a custom driver's manifest.

Every driver bundle carries ``lager_driver.json`` beside its ``driver.py``.
The manifest is the whole contract the CLI sees: it builds the ``lager <type>``
command, its subcommands and their options from it, and never needs the
driver code. The box validates each call's arguments against it before the
driver runs.

    {
      "type": "stepper",
      "version": "1.0.0",
      "lager_api": 1,
      "description": "Stepper rig controlled over a serial link",
      "requirements": "requirements.txt",
      "params": {
        "uart_net": {"type": "net", "role": "uart", "required": true,
                     "help": "UART net wired to the controller"}
      },
      "actions": {
        "position": {"help": "Query the current position"},
        "jog": {"help": "Move forward for N seconds, then return home",
                "args": {"seconds": {"type": "float", "min": 0.1,
                                     "max": 30, "required": true}}}
      }
    }

``params`` are per-net settings stored in the net record. ``args`` are
per-call values. ``requirements`` is optional: a hashed lock file at the top
of the bundle whose packages are installed with the driver (see deps.py).
A box that predates it rejects the key, so an old box refuses such a bundle
instead of installing a driver that cannot import its packages. Pure module: no Flask, no filesystem beyond load_manifest,
so the same rules can be mirrored by the control plane.
"""

import json
import math
import os
import re

MANIFEST_FILE = 'lager_driver.json'
DRIVER_FILE = 'driver.py'

# Versions of the CustomDriver contract this box can run.
SUPPORTED_LAGER_API = (1,)

DEFAULT_TIMEOUT_S = 60
MAX_TIMEOUT_S = 600

TYPE_RE = re.compile(r'^[a-z][a-z0-9_-]{1,31}$')
# Action, param and arg names become Python keyword arguments, so they must
# be identifiers. The CLI shows an underscore as a dash (--hold-time).
NAME_RE = re.compile(r'^[a-z][a-z0-9_]{0,31}$')
# A file at the top of the bundle, not in a folder.
REQUIREMENTS_RE = re.compile(r'^[A-Za-z0-9][A-Za-z0-9._-]{0,63}\.txt$')

# Arg names the CLI already uses on every action command (--box, --json,
# --help) or passes alongside the args (post_net_command's own parameters).
RESERVED_ARG_NAMES = frozenset({
    'box', 'json', 'as_json', 'help', 'netname', 'action', 'role', 'quiet',
    'http_timeout', 'ctx', 'box_addr', 'params',
})

SCALAR_TYPES = ('str', 'int', 'float', 'bool', 'choice')
PARAM_TYPES = SCALAR_TYPES + ('net',)

# A driver type becomes a net role and a top-level `lager <type>` command, so
# it may not take the name of a built-in role or command. test_driver_reserved
# _names_sync.py fails if a CLI command or NetType role is missing here.
RESERVED_TYPES = frozenset({
    # Built-in net roles (NetType.from_role, ROLE_ACTIONS, CLI NET_ROLES)
    'actuate', 'adc', 'analog', 'arm', 'battery', 'dac', 'debug', 'eload',
    'energy-analyzer', 'gpi', 'gpio', 'gpo', 'hsm', 'i2c', 'logic', 'mikrotik',
    'power-supply', 'power-supply-2q', 'rotation', 'router', 'scope',
    'scope-channel', 'solar', 'spi', 'thermocouple', 'uart', 'usb',
    'watt-meter', 'waveform', 'webcam', 'wifi',
    # Top-level CLI commands
    'authorize', 'bench', 'binaries', 'ble', 'blufi', 'box', 'box-config',
    'boxes', 'custom', 'defaults', 'devenv', 'diagnose', 'drivers', 'dut',
    'energy', 'exec', 'hello', 'install', 'install-wheel', 'instruments',
    'login', 'logout', 'logs', 'nets', 'python', 'ssh', 'ssh-setup', 'supply',
    'terminal', 'uninstall', 'update', 'watt', 'whoami',
    # Words a user could reasonably type that must stay unambiguous
    'custom-net', 'driver', 'help', 'net', 'version',
})


class ManifestError(ValueError):
    """The manifest, or a value checked against it, is invalid.

    A ValueError so /net/command answers 400 without a special case.
    """


def _require(cond, message):
    if not cond:
        raise ManifestError(message)


def _check_timeout(value, where):
    _require(isinstance(value, int) and not isinstance(value, bool)
             and 1 <= value <= MAX_TIMEOUT_S,
             '%s: timeout_s must be an integer from 1 to %d' % (where, MAX_TIMEOUT_S))
    return value


def _number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) \
        and math.isfinite(value)


def _parse_field(name, spec, where, allowed_types):
    """Validate one param or arg spec; return it normalised."""
    _require(isinstance(name, str) and NAME_RE.match(name),
             "%s: name '%s' must be lower case letters, digits and _ "
             "(start with a letter, at most 32)" % (where, name))
    _require(isinstance(spec, dict), '%s.%s must be an object' % (where, name))
    unknown = set(spec) - {'type', 'help', 'required', 'default', 'min',
                           'max', 'choices', 'role'}
    _require(not unknown, '%s.%s: unknown keys %s' % (where, name, sorted(unknown)))

    kind = spec.get('type', 'str')
    _require(kind in allowed_types, "%s.%s: type must be one of %s"
             % (where, name, ', '.join(allowed_types)))
    out = {
        'type': kind,
        'help': str(spec.get('help', '')),
        'required': bool(spec.get('required', False)),
    }

    if kind == 'net':
        role = spec.get('role')
        _require(isinstance(role, str) and role,
                 "%s.%s: a 'net' param must name the role it needs" % (where, name))
        out['role'] = role
        return out

    if kind == 'choice':
        choices = spec.get('choices')
        _require(isinstance(choices, list) and choices
                 and all(isinstance(c, str) for c in choices),
                 "%s.%s: a 'choice' needs a non-empty list of strings" % (where, name))
        out['choices'] = list(choices)

    for bound in ('min', 'max'):
        if bound in spec:
            _require(kind in ('int', 'float') and _number(spec[bound]),
                     '%s.%s: %s is only for int and float, and must be a number'
                     % (where, name, bound))
            out[bound] = spec[bound]
    if 'min' in out and 'max' in out:
        _require(out['min'] <= out['max'], '%s.%s: min is above max' % (where, name))

    if 'default' in spec:
        out['default'] = coerce_value(spec['default'], out, '%s.%s' % (where, name))
    return out


def _parse_arg(name, spec, where):
    _require(name not in RESERVED_ARG_NAMES,
             "%s.args: '%s' is reserved by the lager CLI" % (where, name))
    return _parse_field(name, spec, where + '.args', SCALAR_TYPES)


def parse_manifest(data):
    """Validate a decoded manifest; return a normalised copy.

    Raises ManifestError naming the first problem found.
    """
    _require(isinstance(data, dict), 'manifest must be a JSON object')
    unknown = set(data) - {'type', 'version', 'lager_api', 'description',
                           'timeout_s', 'params', 'actions', 'requirements'}
    _require(not unknown, 'manifest: unknown keys %s' % sorted(unknown))

    dtype = data.get('type')
    _require(isinstance(dtype, str) and TYPE_RE.match(dtype),
             "type must be 2-32 lower case letters, digits, '-' or '_', "
             'starting with a letter')
    _require(dtype not in RESERVED_TYPES,
             "type '%s' is the name of a built-in net role or lager command" % dtype)

    version = data.get('version')
    _require(isinstance(version, str) and version.strip(), 'version is required')

    api = data.get('lager_api')
    _require(api in SUPPORTED_LAGER_API,
             'lager_api %r is not supported by this box (supported: %s)'
             % (api, ', '.join(str(v) for v in SUPPORTED_LAGER_API)))

    timeout = _check_timeout(data.get('timeout_s', DEFAULT_TIMEOUT_S), 'manifest')

    requirements = data.get('requirements')
    _require(requirements is None
             or (isinstance(requirements, str) and REQUIREMENTS_RE.match(requirements)),
             'requirements must name a .txt file at the top of the bundle, '
             'such as "requirements.txt"')

    params_in = data.get('params', {})
    _require(isinstance(params_in, dict), 'params must be an object')
    params = {name: _parse_field(name, spec, 'params', PARAM_TYPES)
              for name, spec in params_in.items()}

    actions_in = data.get('actions')
    _require(isinstance(actions_in, dict) and actions_in,
             'actions must be a non-empty object')
    actions = {}
    for name, spec in actions_in.items():
        where = 'actions.%s' % name
        _require(isinstance(name, str) and NAME_RE.match(name),
                 "action name '%s' must be lower case letters, digits and _" % name)
        _require(not name.startswith('_') and name not in ('setup', 'teardown'),
                 "action name '%s' is reserved" % name)
        _require(isinstance(spec, dict), '%s must be an object' % where)
        unknown = set(spec) - {'help', 'args', 'timeout_s'}
        _require(not unknown, '%s: unknown keys %s' % (where, sorted(unknown)))
        args_in = spec.get('args', {})
        _require(isinstance(args_in, dict), '%s.args must be an object' % where)
        actions[name] = {
            'help': str(spec.get('help', '')),
            'timeout_s': _check_timeout(spec.get('timeout_s', timeout), where),
            'args': {arg: _parse_arg(arg, aspec, where) for arg, aspec in args_in.items()},
        }

    return {
        'type': dtype,
        'version': version.strip(),
        'lager_api': api,
        'description': str(data.get('description', '')),
        'timeout_s': timeout,
        'requirements': requirements,
        'params': params,
        'actions': actions,
    }


def load_manifest(bundle_dir):
    """Read and validate ``bundle_dir/lager_driver.json``."""
    path = os.path.join(bundle_dir, MANIFEST_FILE)
    try:
        with open(path, 'r', encoding='utf-8') as f:
            data = json.load(f)
    except FileNotFoundError:
        raise ManifestError('%s is missing' % MANIFEST_FILE)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as e:
        raise ManifestError('%s is not valid JSON: %s' % (MANIFEST_FILE, e))
    return parse_manifest(data)


_TRUE = ('1', 'true', 'yes', 'on')
_FALSE = ('0', 'false', 'no', 'off')


def coerce_value(raw, spec, label):
    """Convert one raw value (JSON or CLI string) to the spec's type.

    Range-checks numbers and choices. Raises ManifestError.
    """
    kind = spec['type']
    try:
        if kind == 'bool':
            if isinstance(raw, bool):
                value = raw
            elif isinstance(raw, str) and raw.strip().lower() in _TRUE + _FALSE:
                value = raw.strip().lower() in _TRUE
            else:
                raise ValueError
        elif kind == 'int':
            if isinstance(raw, bool) or (isinstance(raw, float) and not raw.is_integer()):
                raise ValueError
            value = int(raw)
        elif kind == 'float':
            if isinstance(raw, bool):
                raise ValueError
            value = float(raw)
            if not math.isfinite(value):
                raise ValueError
        elif kind in ('str', 'choice', 'net'):
            if not isinstance(raw, (str, int, float)) or isinstance(raw, bool):
                raise ValueError
            value = str(raw)
        else:
            raise ValueError
    except (TypeError, ValueError):
        raise ManifestError('%s must be %s, got %r' % (label, _describe(kind), raw))

    if kind == 'choice' and value not in spec['choices']:
        raise ManifestError('%s must be one of %s, got %r'
                            % (label, ', '.join(spec['choices']), value))
    if 'min' in spec and value < spec['min']:
        raise ManifestError('%s must be at least %s, got %s' % (label, spec['min'], value))
    if 'max' in spec and value > spec['max']:
        raise ManifestError('%s must be at most %s, got %s' % (label, spec['max'], value))
    return value


def _describe(kind):
    return {'bool': 'true or false', 'int': 'an integer', 'float': 'a number',
            'choice': 'one of the listed choices', 'net': 'a net name'}.get(kind, 'text')


def _coerce_fields(fields, raw, what):
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        raise ManifestError('%s must be an object' % what)
    unknown = sorted(set(raw) - set(fields))
    if unknown:
        raise ManifestError('unknown %s: %s (expected: %s)'
                            % (what, ', '.join(unknown), ', '.join(sorted(fields)) or 'none'))
    out = {}
    for name, spec in fields.items():
        if name in raw and raw[name] is not None:
            out[name] = coerce_value(raw[name], spec, "%s '%s'" % (what[:-1], name))
        elif 'default' in spec:
            out[name] = spec['default']
        elif spec['required']:
            raise ManifestError("missing required %s '%s'" % (what[:-1], name))
    return out


def coerce_args(manifest, action, raw):
    """Validate one call's args for ``action``; return them typed.

    Raises KeyError for an unknown action, ManifestError for bad args.
    """
    spec = manifest['actions'][action]
    return _coerce_fields(spec['args'], raw, 'args')


def coerce_params(manifest, raw):
    """Validate a net record's params against the manifest; return them typed."""
    return _coerce_fields(manifest['params'], raw, 'params')
